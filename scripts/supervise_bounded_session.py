"""Supervise an explicitly authorized UUID through the existing local command API.

Uses the installed engine's database/config models; no exchange client, order,
signer, signal override, rearm, or retry of START_LIVE exists in this script.
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
from datetime import UTC, datetime
from pathlib import Path

from lldesk.config import load_config, repo_root
from lldesk.db import Database

BASE = "http://127.0.0.1:8765"


def get_status() -> dict:
    with urllib.request.urlopen(BASE + "/api/status", timeout=2) as response:
        return json.load(response)


def command(kind: str) -> int:
    assert kind in {"DIAGNOSE_PREFLIGHT", "START_LIVE", "STOP_ENTRIES"}
    with urllib.request.urlopen(BASE + "/api/csrf", timeout=2) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        BASE + "/api/command/" + kind,
        data=b"",
        method="POST",
        headers={"X-CSRF-Token": token},
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
    root = repo_root()
    config = load_config().model_copy(
        update={"allow_live_orders": False, "session_accept_reported_fees": False}
    )
    temp = root / "config.json.pending"
    temp.write_text(config.model_dump_json(indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(root / "config.json")
    db = Database(root / "state/desk.db")
    try:
        raw = db.flag("strategy_session")
        record = json.loads(raw) if raw else {}
        if record.get("id") == session_id and record.get("status") == "authorized":
            record.update(status="closed", stop_reason=reason, ended_at=time.time())
            db.set_flag("strategy_session", json.dumps(record))
            db.set_flag(f"session_used:{session_id}", "1")
            db.event("STRATEGY_SESSION_UNUSED_AUTHORIZATION_CLOSED", record)
    finally:
        db.close()
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
            "preparation_deadline_epoch": preparation_started + 1200,
            "collector_started_utc": datetime.now(UTC).isoformat(),
            "engine_pid": pid,
            "source_hashes": hashes,
            "config": frozen_config,
            "authorization": record,
            "only_commands": ["DIAGNOSE_PREFLIGHT", "START_LIVE", "STOP_ENTRIES"],
        },
    )
    diag_requested = False
    start_requested = False
    stop_requested = False
    active_seen = False
    abort_reason = None
    last_notice = 0.0
    previous = initial
    samples = 0
    errors = 0
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
                    if abort_reason:
                        revoke_unused(session_id, abort_reason)
                    elif not diag_requested and not status["live_blockers"]:
                        diag_requested = True
                        print(
                            "DIAGNOSE_PREFLIGHT command=" + str(command("DIAGNOSE_PREFLIGHT")),
                            flush=True,
                        )
                    elif diag_requested and not start_requested:
                        diagnostic = status["preflight"]["diagnostic"]
                        if diagnostic and diagnostic != previous["preflight"]["diagnostic"]:
                            if diagnostic["outcome"] != "complete_no_send":
                                abort_reason = "activation preflight failed: " + str(
                                    diagnostic["blockers"]
                                )
                                revoke_unused(session_id, abort_reason)
                        if (
                            diagnostic.get("outcome") == "complete_no_send"
                            and now - diagnostic["finished_at"] < 4
                            and not status["live_blockers"]
                            and status["diagnostic_observation"]["technical_ready"]
                        ):
                            # Latch before POST: an uncertain command is never retried.
                            start_requested = True
                            print("START_LIVE command=" + str(command("START_LIVE")), flush=True)
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
                previous = status
                time.sleep(0.25)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--preparation-started", required=True, type=float)
    args = parser.parse_args()
    supervise(args.output, args.session_id, args.preparation_started)
