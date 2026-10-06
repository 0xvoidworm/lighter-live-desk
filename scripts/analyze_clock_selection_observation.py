"""One completed passive recording: actual selection, timing and causal latest replay.

Raw input remains private. No account/client/signing imports or network requests.
"""

import argparse
import collections
import gzip
import json
from pathlib import Path

from analyze_control_feed_observation import analyze
from analyze_transport_observation import distribution, transport_metrics, transport_records
from replay_clock_selection import baseline

from lldesk.models import Stamp


def request_metrics(records, start_ns, end_ns):
    attempts = []
    for row in records:
        if row["kind"] != "source_clock" or row["monotonic_ns"] >= end_ns:
            continue
        a = row["payload"]["attempt"]
        events = {e["phase"]: e["monotonic_ns"] for e in a["transport"]["events"]}

        def interval(begin, end, events=events, a=a):
            lo, hi = events.get(begin, a.get(begin)), events.get(end, a.get(end))
            return (hi - lo) / 1e6 if lo is not None and hi is not None else None

        attempts.append(
            {
                "id": a["id"],
                "started_wall_ns": a["started_ns"],
                "scheduled_monotonic_ns": a["scheduled_monotonic_ns"],
                "send_monotonic_ns": a["send_monotonic_ns"],
                "received_monotonic_ns": a.get("received_monotonic_ns"),
                "inside_window": a["started_monotonic_ns"] >= start_ns,
                "outcome": a["outcome"],
                "http_status": a.get("status"),
                "returned_server_ms": a.get("returned_server_ms"),
                "validation": a.get("validation"),
                "rtt_ms": a.get("duration_ms"),
                "wake_lag_ms": (a["woke_monotonic_ns"] - a["scheduled_monotonic_ns"]) / 1e6,
                "host_read_ms": (a["started_monotonic_ns"] - a["woke_monotonic_ns"]) / 1e6,
                "connection_reused": a["transport"]["connection_reused"],
                "pool_queue_ms": interval("connection_queued_start", "connection_queued_end"),
                "dns_ms": interval("dns_resolvehost_start", "dns_resolvehost_end"),
                "connection_create_ms": interval(
                    "connection_create_start", "connection_create_end"
                ),
                "waiting_headers_ms": interval("request_headers_sent", "request_end"),
                "body_ms": interval("body_started_monotonic_ns", "body_received_monotonic_ns"),
                "decode_ms": interval("body_received_monotonic_ns", "decoded_monotonic_ns"),
                "trace_callback_cpu_ms": a["transport"]["callback_cpu_ns"] / 1e6,
                "trace_events_dropped": a["transport"]["events_dropped"],
                "failed_stage": a.get("failed_stage"),
                "provider_used_weight_1m": a.get("provider_used_weight_1m"),
            }
        )
    return {
        "routine_attempts_startup_and_window": attempts,
        "routine_attempts_inside_window": sum(a["inside_window"] for a in attempts),
        "outcomes": dict(collections.Counter(a["outcome"] for a in attempts)),
        "trace_cpu_total_ms": sum(a["trace_callback_cpu_ms"] for a in attempts),
        "trace_dropped": sum(a["trace_events_dropped"] for a in attempts),
        "phase_statistics_ms": {
            k: distribution([a[k] for a in attempts])
            for k in (
                "rtt_ms",
                "wake_lag_ms",
                "host_read_ms",
                "pool_queue_ms",
                "dns_ms",
                "connection_create_ms",
                "waiting_headers_ms",
                "body_ms",
                "decode_ms",
            )
        },
        "limits": [
            "Connection create contains DNS/TCP/TLS; DNS is nested, do not sum them twice.",
            "TLS alone and resolver-internal cache unknown; absent pool hooks are unobserved.",
            "HTTP header hook is not wire/fill timestamp; full RTT includes "
            "parsing/validation handoff.",
            "No additional calibration or account requests.",
        ],
    }


def selection_metrics(rows, duration):
    legacy = baseline()
    reason_seconds, proof_seconds, overlaps = (
        collections.Counter(),
        collections.Counter(),
        collections.Counter(),
    )
    count, violations, regressions, gains = 0, [], 0, 0.0
    counterfactual_quote_unavailable = 0
    comparisons = []
    expiry_intervals, active_expiry = [], None
    for i, row in enumerate(rows):
        s = row.get("status")
        if not s or not s.get("connected"):
            continue
        end = min(duration, rows[i + 1]["elapsed_s"] if i + 1 < len(rows) else duration)
        dt = max(0, end - row["elapsed_s"])
        if dt > 2:
            continue
        o = s["diagnostic_observation"]
        a = o["source_freshness"]
        selection = a.get("proof_selection_reason", "no_verified_proof")
        reason_seconds[a["reason"]] += dt
        proof_seconds[selection] += dt
        overlaps[tuple(sorted(o["blockers"]))] += dt
        if a["reason"] == "SOURCE_CLOCK_EVIDENCE_EXPIRED":
            if active_expiry is None:
                active_expiry = row["elapsed_s"]
        elif active_expiry is not None:
            expiry_intervals.append({"start_s": active_expiry, "end_s": row["elapsed_s"]})
            active_expiry = None
        inputs = s.get("decision_inputs", {})
        guard = inputs.get("data_guard")
        if guard and (
            inputs["data_valid"] != guard["passed"]
            or inputs.get("source_freshness") != guard.get("source_freshness")
        ):
            violations.append({"at_s": row["elapsed_s"], "kind": "decision_assessment"})
        latest = a.get("latest_proof")
        if latest is None:
            continue
        count += 1
        old = a.get("selected_proof_id") != a.get("latest_proof_id")
        if old and (
            not latest["valid"]
            or not 0 <= a["clock_evidence_age_s"] <= 120
            or a["clock_offset_low_ms"] < latest["clock_offset_low_ms"]
            or a["clock_offset_high_ms"] > latest["clock_offset_high_ms"]
        ):
            violations.append({"at_s": row["elapsed_s"], "kind": "noncontained_invalid_selection"})
        if s["source_clock"]["proof_cache_count"] > 3:
            violations.append({"at_s": row["elapsed_s"], "kind": "unbounded_cache"})
        clock = legacy.BinanceSourceClock(a["max_source_age_ms"])
        clock.evidence = legacy.ClockEvidence(
            Stamp(
                receipt_ns=latest["clock_received_wall_ns"],
                monotonic_ns=latest["clock_received_monotonic_ns"],
            ),
            latest["clock_base_offset_low_ms"],
            latest["clock_base_offset_high_ms"],
            int(latest["clock_proof_id"].split(":")[0]),
            0,
            {"synchronized": True},
        )
        q = o["quotes"]["binance"]
        if q is None:
            counterfactual_quote_unavailable += 1
            continue
        counterfactual = clock.assess(
            Stamp(
                receipt_ns=q["receipt_ns"],
                monotonic_ns=q["monotonic_ns"],
                exchange_ms=q["exchange_ms"],
            ),
            now_ns=a["evaluated_monotonic_ns"],
            wall_ns=a["evaluated_wall_ns"],
        )
        regressions += counterfactual["passed"] and not a["passed"]
        if a["passed"] and not counterfactual["passed"]:
            gains += dt
        if old:
            comparisons.append(
                {
                    "at_s": row["elapsed_s"],
                    "latest": a["latest_proof_id"],
                    "selected": a["selected_proof_id"],
                    "result": a["reason"],
                    "latest_counterfactual": counterfactual["reason"],
                }
            )
    if active_expiry is not None:
        expiry_intervals.append({"start_s": active_expiry, "end_s": duration})
    return {
        "actual_source_reason_seconds": dict(reason_seconds),
        "actual_selection_seconds": dict(proof_seconds),
        "verified_proof_assessment_samples": count,
        "counterfactual_quote_unavailable_samples": counterfactual_quote_unavailable,
        "older_selected_samples": len(comparisons),
        "older_selected_example": comparisons[:5],
        "decision_or_selection_violations": violations,
        "same_input_latest_pass_selected_reject_samples": regressions,
        "runtime_data_latest_only_counterfactual_gain_seconds": gains,
        "expiry_intervals": expiry_intervals,
        "limits": "Actual runtime outcomes are separate from latest-only offline "
        "counterfactual on identical captured quote/time/anchor. No alternative coverage.",
    }


def run(source, market, transport):
    manifest = json.loads((source / "started.json").read_text())
    with gzip.open(source / "snapshots.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    records, limitations = transport_records(transport)
    start_ns = round(manifest["start_monotonic"] * 1e9)
    end_ns = start_ns + manifest["duration_seconds"] * 1_000_000_000
    return {
        "operational": analyze(source, market, permission_reload_required=False),
        "transport": transport_metrics(records, rows, manifest, limitations),
        "clock": request_metrics(records, start_ns, end_ns),
        "selection": selection_metrics(rows, manifest["duration_seconds"]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "market", "transport", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(run(args.source, args.market, args.transport), indent=2) + "\n"
    )
    print(args.output)
