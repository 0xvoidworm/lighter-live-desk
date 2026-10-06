"""Evidence-window regression; synthetic files only, no transport/account calls."""

import gzip
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "transport_analysis", SCRIPTS / "analyze_transport_observation.py"
)
assert spec and spec.loader
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_open_gzip_prefix_preserves_complete_records_and_limitation(tmp_path):
    path = tmp_path / "transport-test.jsonl.gz"
    rows = [{"monotonic_ns": 1, "kind": "eof"}, {"monotonic_ns": 2, "kind": "cleanup"}]
    path.write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode())[:-8])
    actual, limits = analysis.transport_records(tmp_path)
    assert actual == rows
    assert limits == [{"file": path.name, "reason": "open_gzip_member_no_trailer"}]


def test_metrics_keep_startup_separate_and_guard_actual_assessment():
    rows = [
        {
            "monotonic_ns": 9_000_000_000,
            "kind": "handshake_started",
            "connection_id": "a",
            "channel": "public",
            "url": "wss://example.invalid/stream",
        },
        {"monotonic_ns": 9_100_000_000, "kind": "handshake_completed", "connection_id": "a"},
        {"monotonic_ns": 11_000_000_000, "kind": "pong_observed", "matched": True},
        {
            "monotonic_ns": 11_500_000_000,
            "kind": "loop_timer_lag",
            "lag_ms": 30,
            "context": {
                "market_queue": 2,
                "account_queue": 0,
                "active_rest": [
                    {"host": "example.invalid", "path": "/account", "last_phase": "waiting_headers"}
                ],
            },
        },
        {
            "monotonic_ns": 12_000_000_000,
            "kind": "close_initiated",
            "connection_id": "a",
            "cause": "transport_eof",
        },
        {
            "monotonic_ns": 12_100_000_000,
            "kind": "close_followup",
            "connection_id": "a",
            "cause": "cleanup_writer_exception",
        },
        {"monotonic_ns": 16_000_000_000, "kind": "coverage_observed", "ready": True},
    ]
    first = {
        k: 0
        for k in [
            "events_emitted",
            "events_written",
            "events_dropped",
            "write_errors",
            "bytes_written",
            "hook_total_ms",
        ]
    }
    first["connections"] = []
    snapshots = [
        {
            "status": {
                "websocket_telemetry": first,
                "decision_inputs": {
                    "data_valid": False,
                    "source_freshness": None,
                    "data_guard": {"passed": False, "source_freshness": None},
                },
            }
        }
    ]
    result = analysis.transport_metrics(
        rows, snapshots, {"start_monotonic": 10, "duration_seconds": 5}, []
    )
    assert result["window_event_counts"] == {
        "pong_observed": 1,
        "loop_timer_lag": 1,
        "close_initiated": 1,
        "close_followup": 1,
    }
    assert (
        result["recovery_stage_timeline"] == []
    )  # Future coverage cannot prove recovery in-window.
    assert result["library_pong_match_observations"] == {"True": 1}
    connection = result["startup_and_window_connections"][0]
    assert connection["handshake_ms"] == 100
    assert connection["closure_timeline"][0]["cause"] == "transport_eof"
    assert connection["closure_timeline"][1]["cause"] == "cleanup_writer_exception"
    assert result["decision_guard_recording_violations"] == 0
    assert result["local_context_at_lag_events"]["concurrent_rest_phases"] == [
        {
            "host": "example.invalid",
            "path": "/account",
            "phase": "waiting_headers",
            "observations": 1,
        }
    ]
    assert result["capture_extends_beyond_window"]
    snapshots[0]["status"]["decision_inputs"]["source_freshness"] = {"passed": True}
    assert (
        analysis.transport_metrics(
            rows, snapshots, {"start_monotonic": 10, "duration_seconds": 5}, []
        )["decision_guard_recording_violations"]
        == 1
    )
