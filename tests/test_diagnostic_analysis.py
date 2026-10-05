"""Offline economic estimates must not invent fees or double-count spread."""

import gzip
import importlib.util
import json
import sqlite3
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "diagnostic_analysis", Path(__file__).parents[1] / "scripts/analyze_diagnostic_recording.py"
)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_production_metrics_keep_source_unknown_and_deduplicate_requests():
    scripts = str(Path(__file__).parents[1] / "scripts")
    sys.path.insert(0, scripts)
    try:
        from analyze_control_feed_observation import production_metrics
    finally:
        sys.path.remove(scripts)
    request = {
        "id": 1,
        "started_at": 100.2,
        "path": "/api/v1/account",
        "local_quota_weight": 1,
        "http_attempts": 1,
        "reason": "account_state_refresh",
    }
    rows = []
    for tick in range(3):
        known = tick != 0
        rows.append(
            {
                "elapsed_s": tick,
                "status": {
                    "connected": True,
                    "diagnostic_observation": {
                        "source_freshness": {
                            "passed": known,
                            "reason": "fresh" if known else "unknown",
                            "source_age_high_ms": 200 if known else None,
                        },
                        "checks": {
                            "source_freshness": known,
                            "market_data": known,
                            "basis_scale": known,
                        },
                    },
                    "history_audit": {"complete": True},
                    "request_budget": {
                        "used_last_60s": 12,
                        "reserved_unconsumed": 2,
                        "transaction_slots_remaining": 41,
                    },
                    "transport_trace": {
                        "active": [request] if not known else [],
                        "recent": [request | {"outcome": "ok"}] if known else [],
                    },
                },
            }
        )
    report = production_metrics(
        rows, {"duration_seconds": 3, "start_epoch": 100, "deadline_epoch": 103}
    )
    assert report["source_reason_seconds"] == {"unknown": 1, "fresh": 2}
    assert report["source_assessment_statistics"]["source_age_high_ms"]["n"] == 2
    assert report["source_assessment_statistics"]["clock_uncertainty_ms"] is None
    assert report["source_consistency_violations"] == 0
    assert report["exit_capacity_at_least_15_seconds"] == 3
    assert report["request_budget_statistics"]["used_plus_reserved"]["max"] == 14
    endpoint = report["lighter_read_endpoint_summary"]["/api/v1/account"]
    assert endpoint["request_count"] == endpoint["quota_weight"] == endpoint["http_attempts"] == 1
    assert endpoint["outcomes"] == {"ok": 1}


def test_depth_ab_analysis_keeps_disconnect_and_missing_clock_without_zero_latency(tmp_path):
    """Synthetic offline fixture, never starts a transport or a measurement."""
    spec = importlib.util.spec_from_file_location(
        "bounded_ab", Path(__file__).parents[1] / "scripts/analyze_bounded_preparation.py"
    )
    bounded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bounded)
    (tmp_path / "started.json").write_text(json.dumps({"at": 100, "deadline_epoch": 110}))
    (tmp_path / "finished.json").write_text(json.dumps({"at": 110}))
    status = {
        "diagnostic_observation": {"blockers": ["source_freshness", "coverage"]},
        "source_clock": {"evidence": None, "recent": []},
        "source_freshness": {"reason": "SOURCE_CLOCK_NOT_VERIFIED", "receipt_age_ms": 20},
    }
    (tmp_path / "app.jsonl").write_text(
        json.dumps({"at": 100, "kind": "app_snapshot", "body": {"status": status}}) + "\n"
    )
    for arm, received in [("depth", 101), ("combined", 101.2)]:
        rows = [
            {
                "at": received,
                "elapsed": 1,
                "kind": "message",
                "body": {
                    "connection": 1,
                    "event": "depthUpdate",
                    "received_at": received,
                    "bytes": 99,
                    "T": 97000,
                    "E": 97000,
                    "u": 123,
                    "U": 121,
                    "pu": 120,
                    "bid": [],
                    "ask": [],
                    "sequence_gap": False,
                },
            },
            {
                "at": 102,
                "elapsed": 2,
                "kind": "receive_error",
                "body": {"connection": 1, "error": "ResetWithoutClosingHandshake"},
            },
        ]
        (tmp_path / f"{arm}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = bounded.analyze_depth_ab(tmp_path, tmp_path)
    assert result["arms"]["combined"]["counts"]["receive_error"] == 1
    assert result["arms"]["depth"]["source_clock_bound_ms"] == {}
    assert result["arms"]["depth"]["source_classifications"]["depthUpdate:T:clock_unknown"] == 1
    assert result["paired_depth"]["combined_minus_depth_receipt_ms"]["p50"] == pytest.approx(200)
    assert result["engine"]["technical_ready_s"] == 0
    assert result["engine"]["blocker_s"]["coverage"] == 10
    assert sum(v["seconds"] for v in result["engine"]["overlap_s"]) == 10


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
