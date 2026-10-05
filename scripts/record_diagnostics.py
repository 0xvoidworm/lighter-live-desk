"""Exactly one 20-minute passive local-status capture; no exchange client/order calls.

Uses the existing Python project toolchain. A durable start marker prevents replay.
"""

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from lldesk.config import load_config

DURATION_SECONDS = 1200


def capture(output: Path, expected_pid: int):
    root = Path(__file__).resolve().parents[1]
    config = load_config()
    assert not config.allow_live_orders
    assert not config.probe_accept_reported_fees and not config.session_accept_reported_fees
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    marker = output / "started.json"
    assert not marker.exists(), "one recording only; existing start marker forbids repeating"
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "src/lldesk").glob("*.py")
    }
    params = {
        key: getattr(config, key)
        for key in [
            "mode",
            "account_index",
            "account_type",
            "binance_dns_mode",
            "expected_account_type_code",
            "expected_account_trading_mode",
            "expected_margin_mode",
            "expected_initial_margin_fraction",
            "max_entry_notional_usd",
            "max_trade_risk_usd",
            "max_effective_leverage",
            "daily_loss_limit_usd",
            "max_funding_usd",
            "source_funding_spent_usd",
            "max_data_age_ms",
            "warmup_seconds",
            "max_hold_seconds",
            "allow_live_orders",
            "probe_accept_reported_fees",
            "session_accept_reported_fees",
        ]
    }
    # Startup wait is bounded and precedes recording; it never queries the exchange.
    wait_until = time.monotonic() + 10
    while True:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=2) as r:
            initial = json.load(r)
        if initial.get("engine_pid") == expected_pid and "diagnostic_observation" in initial:
            break
        assert time.monotonic() < wait_until, "new instrumented process did not appear"
        time.sleep(0.1)
    assert not initial["allow_live_orders"] and initial["strategy_session"]["status"] == "closed"
    start_mono, start_wall = time.monotonic(), time.time()
    manifest = {
        "base_commit": "ca8c2b6",
        "code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "started_at_utc": datetime.fromtimestamp(start_wall, UTC).isoformat(),
        "start_epoch": start_wall,
        "start_monotonic": start_mono,
        "duration_seconds": DURATION_SECONDS,
        "deadline_epoch": start_wall + DURATION_SECONDS,
        "expected_engine_pid": expected_pid,
        "source_hashes": hashes,
        "parameters": params,
        "configuration_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
        "sdk_distributions": {
            name: importlib.metadata.version(name)
            for name in importlib.metadata.packages_distributions().get("lighter", [])
        },
        "source": "existing engine status/trace only; no additional exchange polling",
        "authorization": "order false, prior trading session closed",
    }
    with marker.open("x") as stream:
        json.dump(manifest, stream, indent=2, default=str)
    marker.chmod(0o600)
    records = errors = 0
    deadline = start_mono + DURATION_SECONDS
    snapshots = output / "snapshots.jsonl.gz"
    with gzip.open(snapshots, "xt") as stream:
        snapshots.chmod(0o600)
        while time.monotonic() < deadline:
            tick = time.monotonic()
            record = {
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "elapsed_s": tick - start_mono,
            }
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8765/api/status", timeout=min(2, deadline - tick)
                ) as response:
                    status = json.load(response)
                assert (
                    not status["allow_live_orders"]
                    and status["strategy_session"]["status"] == "closed"
                )
                assert not status["strategy_session"]["attempts"]
                if status.get("account"):
                    assert not Decimal(status["account"]["position_qty"])
                    assert not status["account"]["exchange_active_order_count"]
                    assert not status["account"]["other_exchange_position_count"]
                assert not status["pending_orders"]
                record["status"] = {k: v for k, v in status.items() if k != "events"}
                record["local_capture_delay_ms"] = (time.monotonic() - tick) * 1000
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                record["collector_error"] = type(error).__name__
                errors += 1
            if time.monotonic() >= deadline:
                break
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            records += 1
            # Fixed deadline includes downtime/warm-up; no compensating extension.
            time.sleep(max(0, min(0.25 - (time.monotonic() - tick), deadline - time.monotonic())))
    finish = {
        "ended_at_utc": datetime.now(UTC).isoformat(),
        "analysis_window_seconds": DURATION_SECONDS,
        "elapsed_wall_monotonic_seconds": time.monotonic() - start_mono,
        "records": records,
        "collector_errors": errors,
        "configuration_unchanged": config == load_config(),
        "source_hashes_unchanged": all(
            hashlib.sha256((root / p).read_bytes()).hexdigest() == h for p, h in hashes.items()
        ),
        "snapshot_sha256": hashlib.sha256(snapshots.read_bytes()).hexdigest(),
        "order_authorization": False,
    }
    (output / "finished.json").write_text(json.dumps(finish, indent=2) + "\n")
    (output / "finished.json").chmod(0o600)
    print(json.dumps(finish))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--engine-pid", required=True, type=int)
    args = parser.parse_args()
    capture(args.output, args.engine_pid)
