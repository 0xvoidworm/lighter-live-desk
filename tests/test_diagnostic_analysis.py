"""Offline economic estimates must not invent fees or double-count spread."""

import gzip
import importlib.util
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "diagnostic_analysis", Path(__file__).parents[1] / "scripts/analyze_diagnostic_recording.py"
)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


@pytest.mark.parametrize("residual", [D(-1), D(1)])
def test_no_reversion_loses_one_full_spread_and_keeps_fee_unknown(residual):
    result = analysis.book_reversion_estimate(D(101), D(99), D(100), D(100), residual, D(0), D(0))
    entry = D(101) if residual < 0 else D(99)
    assert result["edge_after_two_local_book_legs_bps"] == -D(2) / entry * 10000
    assert result["roundtrip_fee_assumption_bps"] == 0
    assert result["fee_exact"] is None


def test_depth_fee_and_buffer_can_eliminate_positive_mid_reversion():
    # Mid 100 -> 100.1 looks positive, but same-size VWAP/depth consumes it.
    result = analysis.book_reversion_estimate(
        D("100.08"), D("99.92"), D(100), D("100.1"), D(-10), D("0.3"), D(5)
    )
    assert result["edge_after_two_local_book_legs_bps"] < 0
    assert result["edge_after_fee_assumption_and_5bps_buffer"] < D("-5.6")
    assert result["roundtrip_fee_assumption_bps"] == D("0.6")
    assert result["fee_exact"] is None


def test_reconnect_analysis_uses_interval_delta_and_keeps_unknown_ownership(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "bounded_analysis", Path(__file__).parents[1] / "scripts/analyze_bounded_preparation.py"
    )
    bounded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bounded)
    (tmp_path / "started.json").write_text(
        json.dumps({"engine_pid": 7, "preparation_started_epoch": 10})
    )
    (tmp_path / "finished.json").write_text(json.dumps({"session": {"ended_at": 30}}))
    handshakes = [
        {"id": 1, "path": "/public/stream", "started_at": 10, "finished_at": 10.1},
        {"id": 2, "path": "/public/stream", "started_at": 22, "finished_at": 22.1},
    ]
    rows = []
    for epoch, counter in [(11, 54), (22.2, 55)]:
        rows.append(
            {
                "epoch": epoch,
                "status": {
                    "feed_channels": {
                        "binance_public": {
                            "reconnects": counter,
                            "last_disconnect_at": 20,
                            "connected": True,
                            "last_frame_age_ms": 100,
                            "last_error": "ConnectionError",
                            "last_transport_detail": {
                                "cause": "ClientConnectionResetError",
                                "close_code": 1006,
                            },
                            "retry_delay_s": 2,
                        }
                    },
                    "binance_transport_trace": {"recent": handshakes[: counter - 53]},
                    "binance_streams": {"public": True},
                    "queue_backlog": 0,
                    "event_loop_lag_ms": 0,
                },
            }
        )
    with gzip.open(tmp_path / "snapshots.jsonl.gz", "wt") as target:
        for row in rows:
            target.write(json.dumps(row) + "\n")
    (tmp_path / "state").mkdir()
    with sqlite3.connect(tmp_path / "state/desk.db") as db:
        db.execute("CREATE TABLE events(at REAL,kind TEXT,body TEXT)")
        db.execute("INSERT INTO events VALUES(21,'BOOK_GAP','sequence recovery')")
    result = bounded.reconnect_timeline(tmp_path, tmp_path)
    assert result["observed_reconnects"] == 1  # counter 54 is pre-existing, not this interval
    assert result["timeline"][0]["connection_id"] == "pid7:http1"
    assert result["timeline"][0]["actual_disconnect_to_next_handshake_start_s"] == 2
    assert result["sequence_recovery_events"][0][0] == 21
    assert result["timeline"][0]["close_owner"].startswith("not recorded")
