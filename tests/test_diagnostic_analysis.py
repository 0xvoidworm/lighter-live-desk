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


def test_clock_replay_never_uses_proof_first_recorded_after_evaluation():
    scripts = str(Path(__file__).parents[1] / "scripts")
    sys.path.insert(0, scripts)
    try:
        from analyze_control_feed_observation import clock_replay
    finally:
        sys.path.remove(scripts)
    wall, mono = 1_791_213_410_000_000_000, 99_000_000_000_000
    proof = {
        "received_ns": wall,
        "received_monotonic_ns": mono,
        "offset_low_ms": -50,
        "offset_high_ms": 50,
        "server_ms": wall // 1_000_000,
        "rtt_ms": 80,
        "host": {"synchronized": True},
    }
    source = {
        "evaluated_monotonic_ns": mono,
        "reason": "SOURCE_CLOCK_NOT_VERIFIED",
        "passed": False,
    }
    row = {
        "elapsed_s": 1,
        "status": {
            "decision_inputs": {
                "source_freshness": source,
                "monotonic_ns": mono,
                "evaluated_at": wall / 1e9,
                "data_valid": False,
                "quotes": {
                    "binance": {
                        "receipt_ns": wall,
                        "monotonic_ns": mono,
                        "exchange_ms": wall // 1_000_000 - 200,
                        "exchange_event_ms": None,
                    }
                },
            },
            "diagnostic_observation": {
                "source_freshness": source,
                "checks": {"basis_scale": False},
            },
        },
    }
    later = [{"recorded_monotonic_ns": mono + 1, "payload": {"clock": proof}}]
    result = clock_replay([row], later)
    assert result["proof_unavailable"] == 1
    assert result["matching_recorded_second_assessment_reasons"] == 0
    earlier = [{"recorded_monotonic_ns": mono - 1, "payload": {"clock": proof}}]
    result = clock_replay([row], earlier)
    assert result["second_assessment_reasons"] == {"SOURCE_FRESH_WITH_CLOCK_BOUND": 1}
    assert result["matching_recorded_second_assessment_reasons"] == 0  # No historical rewrite.


def test_existing_account_diagnostics_export_excludes_secrets_and_preserves_timeout(tmp_path):
    scripts = str(Path(__file__).parents[1] / "scripts")
    sys.path.insert(0, scripts)
    try:
        from analyze_control_feed_observation import persistent_diagnostics
    finally:
        sys.path.remove(scripts)
    path = tmp_path / "audit.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE events(at REAL,kind TEXT,body TEXT)")
        body = {
            "outcome": "error",
            "lock_wait_ms": 0.004,
            "request_used_60s": 9,
            "requests": [
                {
                    "id": 1,
                    "path": "/api/v1/accountActiveOrders",
                    "outcome": "error",
                    "last_phase": "waiting_headers",
                    "error": "CancelledError",
                    "token": "fixture-secret",
                }
            ],
        }
        db.execute("INSERT INTO events VALUES(1,'ACCOUNT_STATE_DIAGNOSTIC',?)", (json.dumps(body),))
        db.execute("INSERT INTO events VALUES(3,'ACCOUNT_STATE_DIAGNOSTIC',?)", (json.dumps(body),))
    exported = persistent_diagnostics(path, 0, 2)
    assert len(exported["records"]) == 1
    assert "fixture-secret" not in json.dumps(exported)
    assert exported["request_failure_phases"] == {"waiting_headers": 1}


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
