"""Causal reporting regression; synthetic telemetry only, no new runtime recording."""

import sys
from pathlib import Path

from test_clock_proof_selection import MONO, WALL, clock_with, proof, quote

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from analyze_clock_selection_observation import request_metrics, selection_metrics  # noqa: E402


def row(at, clock, age_ms):
    stamp = quote(age_ms)
    result = clock.assess(stamp, now_ns=MONO, wall_ns=WALL)
    return {
        "elapsed_s": at,
        "status": {
            "connected": True,
            "diagnostic_observation": {
                "source_freshness": result,
                "blockers": [],
                "quotes": {
                    "binance": {
                        "receipt_ns": stamp.receipt_ns,
                        "monotonic_ns": stamp.monotonic_ns,
                        "exchange_ms": stamp.exchange_ms,
                    }
                },
            },
            "source_clock": clock.status(),
            "decision_inputs": {
                "data_valid": result["passed"],
                "source_freshness": result,
                "data_guard": {"passed": result["passed"], "source_freshness": result},
            },
        },
    }


def test_actual_selection_and_latest_counterfactual_are_distinct():
    clock = clock_with(proof(60, -10, 10), proof(0, -200, 200))
    rows = [row(0, clock, 100), row(0.5, clock, 400)]
    result = selection_metrics(rows, 1)
    assert result["older_selected_samples"] == 2
    assert result["runtime_data_latest_only_counterfactual_gain_seconds"] == 0.5
    assert result["same_input_latest_pass_selected_reject_samples"] == 0
    assert not result["decision_or_selection_violations"]


def test_invalid_latest_and_missing_quote_never_report_selection_gain():
    clock = clock_with(proof(0), proof(121))
    result = selection_metrics([row(0, clock, 100)], 1)
    assert result["expiry_intervals"] == [{"start_s": 0, "end_s": 1}]
    assert result["runtime_data_latest_only_counterfactual_gain_seconds"] == 0
    assert result["older_selected_samples"] == 0


def test_bootstrap_has_clock_evidence_but_no_book_counterfactual_is_unknown():
    clock = clock_with(proof())
    r = row(0, clock, 100)
    r["status"]["diagnostic_observation"]["quotes"]["binance"] = None
    result = selection_metrics([r], 1)
    assert result["counterfactual_quote_unavailable_samples"] == 1
    assert result["runtime_data_latest_only_counterfactual_gain_seconds"] == 0
    assert result["verified_proof_assessment_samples"] == 1


def test_connection_create_is_not_relabelled_tls_and_absent_dns_unknown():
    record = {
        "kind": "source_clock",
        "monotonic_ns": 40,
        "payload": {
            "attempt": {
                "id": 1,
                "started_ns": WALL,
                "scheduled_monotonic_ns": 0,
                "woke_monotonic_ns": 1,
                "started_monotonic_ns": 2,
                "send_monotonic_ns": 3,
                "outcome": "clock_unverified",
                "status": 200,
                "returned_server_ms": WALL // 1_000_000,
                "duration_ms": 500,
                "transport": {
                    "events": [
                        {"phase": "connection_create_start", "monotonic_ns": 4},
                        {"phase": "connection_create_end", "monotonic_ns": 20},
                        {"phase": "request_headers_sent", "monotonic_ns": 21},
                        {"phase": "request_end", "monotonic_ns": 30},
                    ],
                    "events_dropped": 0,
                    "callback_cpu_ns": 10,
                    "connection_reused": False,
                },
            }
        },
    }
    result = request_metrics([record], 0, 50)
    attempt = result["routine_attempts_startup_and_window"][0]
    assert attempt["connection_create_ms"] == 16 / 1e6
    assert attempt["dns_ms"] is None and attempt["pool_queue_ms"] is None
    assert attempt["returned_server_ms"] == WALL // 1_000_000
    assert not any("tls_ms" in key for key in attempt)
