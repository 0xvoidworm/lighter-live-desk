"""Supervise an explicitly authorized UUID through the existing local command API.

Uses the installed engine's database/config models; no exchange client, order,
signer, signal override, rearm, or retry of activation exists in this script.
Private snapshots are analysis inputs, never public audit artifacts.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from lldesk.config import load_config, repo_root
from lldesk.db import Database

BASE = "http://127.0.0.1:8765"


def get_status() -> dict:
    with urllib.request.urlopen(BASE + "/api/status", timeout=2) as response:
        return json.load(response)


def command(kind: str, binding: dict | None = None) -> int:
    assert kind in {
        "LOAD_SESSION_PERMISSION",
        "DIAGNOSE_PREFLIGHT",
        "START_LIVE",
        "START_WHEN_READY",
        "STOP_ENTRIES",
    }
    assert (kind == "START_WHEN_READY") == (binding is not None)
    with urllib.request.urlopen(BASE + "/api/csrf", timeout=2) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        BASE + "/api/command/" + kind,
        data=json.dumps(binding).encode() if binding else b"",
        method="POST",
        headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return int(json.load(response)["command_id"])


def persist(path: Path, value: dict) -> None:
    with path.open("x") as target:
        os.fchmod(target.fileno(), 0o600)
        json.dump(value, target, indent=2, default=str)
        target.write("\n")


def clean(status: dict) -> bool:
    account = status.get("account") or {}
    db = Database(repo_root() / "state/desk.db")
    try:
        intents = db.unresolved()
    finally:
        db.close()
    return bool(
        status.get("connected")
        and account.get("components_complete")
        and max(account.get("component_ages_s", {"unknown": 99}).values()) < 15
        and float(account.get("position_qty", 1)) == 0
        and account.get("exchange_active_order_count") == 0
        and account.get("other_exchange_position_count") == 0
        and account.get("exchange_pending_count") == 0
        and not status.get("pending_orders")
        and not intents
    )


def revoke_unused(session_id: str, reason: str) -> None:
    record = get_status()["strategy_session"]
    assert record["id"] == session_id and record["status"] == "authorized"
    print("Unused authorization revocation requested: " + reason, flush=True)
    # The engine owns permission/config/session transitions; the supervisor only queues.
    command("STOP_ENTRIES")


def supervise(output: Path, session_id: str, preparation_started: float) -> None:
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    root = repo_root()
    initial = get_status()
    record = initial["strategy_session"]
    assert record["id"] == session_id and record["status"] == "authorized"
    assert record["started_at"] is None and not record["attempts"]
    assert record["max_seconds"] <= 1800 and record["max_attempts"] <= 3
    assert preparation_started == record["authorized_at"], "original preparation clock required"
    pid = initial["engine_pid"]
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "src/lldesk").glob("*.py")
    }
    frozen_config = load_config().model_dump(mode="json")
    persist(
        output / "started.json",
        {
            "session_id": session_id,
            "preparation_started_epoch": preparation_started,
            "preparation_deadline_epoch": record["authorization_expires_epoch"],
            "collector_started_utc": datetime.now(UTC).isoformat(),
            "engine_pid": pid,
            "source_hashes": hashes,
            "config": frozen_config,
            "authorization": record,
            "only_commands": [
                "LOAD_SESSION_PERMISSION",
                "START_WHEN_READY",
                "STOP_ENTRIES",
            ],
        },
    )
    load_requested = False
    start_requested = False
    stop_requested = False
    active_seen = False
    abort_reason = None
    last_notice = 0.0
    samples = 0
    errors = 0
    activation_binding = {
        "request_id": str(uuid.uuid4()),
        "session_id": record["id"],
        "account_index": record["account_index"],
        "parameter_hash": record["parameter_hash"],
        "authorization_expires_epoch": record["authorization_expires_epoch"],
    }
    persist(output / "activation-binding.json", activation_binding)
    with (output / "snapshots.jsonl.gz").open("xb") as raw:
        os.fchmod(raw.fileno(), 0o600)
        with gzip.GzipFile(fileobj=raw, mode="wb") as capture:
            while True:
                now = time.time()
                try:
                    status = get_status()
                except Exception as exc:
                    errors += 1
                    capture.write(
                        (
                            json.dumps({"epoch": now, "local_status_error": type(exc).__name__})
                            + "\n"
                        ).encode()
                    )
                    time.sleep(0.25)
                    continue
                record = status["strategy_session"]
                assert record["id"] == session_id, "authorization changed unexpectedly"
                capture.write(
                    (
                        json.dumps(
                            {"epoch": now, "monotonic_ns": time.monotonic_ns(), "status": status}
                        )
                        + "\n"
                    ).encode()
                )
                samples += 1
                if now - last_notice >= 45:
                    last_notice = now
                    print(
                        json.dumps(
                            {
                                "utc": datetime.now(UTC).isoformat(),
                                "session": record["status"],
                                "activation": (status.get("activation_request") or {}).get(
                                    "status"
                                ),
                                "state": status["state"],
                                "attempts": len(record["attempts"]),
                                "remaining": status.get("session_remaining_s"),
                                "coverage_remaining": status["coverage"]["duration_remaining_s"],
                                "technical_ready": status["diagnostic_observation"][
                                    "technical_ready"
                                ],
                                "position": status.get("account", {}).get("position_qty"),
                                "raw_deviation_bps": status["decision"].get("deviation_bps"),
                            }
                        ),
                        flush=True,
                    )
                if record["status"] == "authorized":
                    if now >= preparation_started + 1200:
                        abort_reason = "20-minute preparation deadline reached"
                    elif status["engine_pid"] != pid:
                        abort_reason = "engine restarted during preparation"
                    if abort_reason and not stop_requested:
                        stop_requested = True
                        revoke_unused(session_id, abort_reason)
                    elif not load_requested and all(
                        status["diagnostic_observation"]["checks"][k]
                        for k in (
                            "account_fresh",
                            "flat_clean",
                            "lifecycle_idle",
                            "reconciliation_idle",
                            "reconciliation_fresh",
                            "journal",
                        )
                    ):
                        load_requested = True
                        print(
                            "LOAD_SESSION_PERMISSION command="
                            + str(command("LOAD_SESSION_PERMISSION")),
                            flush=True,
                        )
                    elif (
                        not start_requested
                        and status.get("loaded_session_permission_id") == session_id
                    ):
                        # Persisted client ID + latch before POST. Engine owns readiness
                        # and the fresh audit, not repeated supervisor START_LIVE calls.
                        start_requested = True
                        try:
                            print(
                                "START_WHEN_READY command="
                                + str(command("START_WHEN_READY", activation_binding)),
                                flush=True,
                            )
                        except Exception as exc:
                            print(
                                "activation response ambiguous: "
                                + type(exc).__name__
                                + "; monitor same persisted request ID and journal; no POST retry",
                                flush=True,
                            )
                elif record["status"] in {"active", "ending"}:
                    active_seen = True
                    if record["status"] == "active":
                        changed = status["engine_pid"] != pid or any(
                            hashlib.sha256((root / name).read_bytes()).hexdigest() != digest
                            for name, digest in hashes.items()
                        )
                        if now >= record["deadline_epoch"] or changed:
                            if not stop_requested:
                                stop_requested = True
                                print(
                                    "STOP_ENTRIES command=" + str(command("STOP_ENTRIES")),
                                    flush=True,
                                )
                elif (
                    record["status"] == "closed"
                    and clean(status)
                    and not status["allow_live_orders"]
                ):
                    if status["state"] == "IDLE":
                        persist(
                            output / "finished.json",
                            {
                                "finished_at_utc": datetime.now(UTC).isoformat(),
                                "session": record,
                                "final_status": status,
                                "samples": samples,
                                "local_status_errors": errors,
                                "active_seen": active_seen,
                                "abort_reason": abort_reason,
                                "source_hashes_unchanged": all(
                                    hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                                    for name, digest in hashes.items()
                                ),
                            },
                        )
                        print(
                            "FINISHED flat/order/intent clean, IDLE, orders OFF, "
                            "authorization closed",
                            flush=True,
                        )
                        return
                time.sleep(0.25)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--preparation-started", required=True, type=float)
    args = parser.parse_args()
    supervise(args.output, args.session_id, args.preparation_started)
