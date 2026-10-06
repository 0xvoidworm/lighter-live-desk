"""Offline transport evidence analysis; no engine, signer, exchange or network calls.

Uses the project's existing Python JSON/gzip analysis pipeline and exact capture window.
"""

import argparse
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

from analyze_control_feed_observation import analyze


def distribution(values):
    ordered = sorted(x for x in values if x is not None)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max": ordered[-1],
        "mean": sum(ordered) / len(ordered),
    }


def transport_records(directory):
    rows, limitations = [], []
    for path in sorted(directory.glob("transport-*.jsonl.gz")):
        with gzip.open(path, "rt") as stream:
            try:
                for line in stream:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        limitations.append({"file": path.name, "reason": "incomplete_json_line"})
            except EOFError:
                limitations.append({"file": path.name, "reason": "open_gzip_member_no_trailer"})
    return sorted(rows, key=lambda x: x["monotonic_ns"]), limitations


def transport_metrics(rows, snapshots, manifest, limitations):
    start_ns = round(manifest["start_monotonic"] * 1e9)
    end_ns = start_ns + round(manifest["duration_seconds"] * 1e9)
    startup = [r for r in rows if r["monotonic_ns"] < end_ns]
    window = [r for r in startup if r["monotonic_ns"] >= start_ns]
    events, connections = Counter(), defaultdict(list)
    callbacks, loop_lags, pong_matches = defaultdict(list), [], Counter()
    for row in startup:
        if row.get("connection_id"):
            connections[row["connection_id"]].append(row)
    for row in window:
        events[row["kind"]] += 1
        if row.get("callback_lag_ms") is not None:
            callbacks[row["kind"]].append(row["callback_lag_ms"])
        if row["kind"] == "loop_timer_lag":
            loop_lags.append(row["lag_ms"])
        if row["kind"] == "pong_observed":
            pong_matches[str(row.get("matched"))] += 1
    stages = {"snapshot_applied", "depth_bridged", "receipt_source_valid", "coverage_observed"}
    timeline = []
    for row in startup:
        if row["kind"] in stages:
            timeline.append({**row, "elapsed_s": (row["monotonic_ns"] - start_ns) / 1e9})
    summaries = []
    for identity, records in connections.items():
        starts = [r for r in records if r["kind"] == "handshake_started"]
        completions = [r for r in records if r["kind"] == "handshake_completed"]
        summaries.append(
            {
                "connection_id": identity,
                "channel": records[0].get("channel"),
                "url": starts[0].get("url") if starts else None,
                "previous_connection_id": starts[0].get("previous_connection_id")
                if starts
                else None,
                "handshake_ms": (completions[0]["monotonic_ns"] - starts[0]["monotonic_ns"]) / 1e6
                if starts and completions
                else None,
                "events": dict(Counter(r["kind"] for r in records)),
                "closure_timeline": [
                    r
                    for r in records
                    if r["kind"]
                    in {
                        "close_initiated",
                        "close_followup",
                        "connection_exception",
                        "disconnect",
                        "pong_timeout_callback",
                        "transport_connection_lost",
                    }
                ],
            }
        )
    telemetry = [r["status"]["websocket_telemetry"] for r in snapshots if r.get("status")]
    first, last = telemetry[0], telemetry[-1]
    counters = {
        k: last[k] - first[k]
        for k in [
            "events_emitted",
            "events_written",
            "events_dropped",
            "write_errors",
            "bytes_written",
            "hook_total_ms",
        ]
    }
    counters["hook_time_share_percent"] = counters["hook_total_ms"] / (
        manifest["duration_seconds"] * 10
    )
    decision_violations = 0
    for row in snapshots:
        inputs = (row.get("status") or {}).get("decision_inputs", {})
        guard = inputs.get("data_guard")
        if guard and (
            inputs["data_valid"] != guard["passed"]
            or inputs.get("source_freshness") != guard.get("source_freshness")
        ):
            decision_violations += 1
    final_frames = {c["connection_id"]: c for c in last["connections"]}
    delayed = [r for r in window if r["kind"] == "loop_timer_lag"]
    concurrent_rest = Counter(
        (request.get("host"), request.get("path"), request.get("last_phase"))
        for row in delayed
        for request in row.get("context", {}).get("active_rest", [])
    )
    local_context = {
        "loop_lag_events_with_active_rest": sum(
            bool(r.get("context", {}).get("active_rest")) for r in delayed
        ),
        "loop_lag_market_queue": distribution(
            [r.get("context", {}).get("market_queue") for r in delayed]
        ),
        "loop_lag_account_queue": distribution(
            [r.get("context", {}).get("account_queue") for r in delayed]
        ),
        "market_recorder_cumulative_max_ms_at_lag_events": distribution(
            [r.get("context", {}).get("market_recorder_max_ms") for r in delayed]
        ),
        "concurrent_rest_phases": [
            {"host": k[0], "path": k[1], "phase": k[2], "observations": v}
            for k, v in concurrent_rest.items()
        ],
        "limit": "Cumulative recorder maxima and concurrent REST do not prove a callback's cause.",
    }
    return {
        "window_event_counts": dict(events),
        "captured_window_events": len(window),
        "startup_and_window_connections": summaries,
        "recovery_stage_timeline": timeline,
        "callback_lag_ms": {k: distribution(v) for k, v in callbacks.items()},
        "loop_lag_events_over_25ms": distribution(loop_lags),
        "local_context_at_lag_events": local_context,
        "library_pong_match_observations": dict(pong_matches),
        "recorder_window_counter_deltas": counters,
        "recorder_final_since_process_start": {
            k: v for k, v in last.items() if k not in {"connections", "recent"}
        },
        "final_connection_frames_since_boot": final_frames,
        "decision_guard_recording_violations": decision_violations,
        "incomplete_member_limitations": limitations,
        "last_captured_monotonic_ns": rows[-1]["monotonic_ns"] if rows else None,
        "capture_extends_beyond_window": bool(rows and rows[-1]["monotonic_ns"] >= end_ns),
        "limits": [
            "Frame counters include startup; window event counters exclude startup.",
            "Hook time covers measured recorder operations, not a comparative CPU benchmark.",
            "Library dequeue is not kernel arrival; app frames are observed after read.",
            "Timer lag and REST concurrency are correlation, not host/network/upstream causation.",
            "Coverage timestamps are existing status observations, not invented exact transitions.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--market", required=True, type=Path)
    parser.add_argument("--transport", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.source / "started.json").read_text())
    with gzip.open(args.source / "snapshots.jsonl.gz", "rt") as stream:
        snapshots = [json.loads(line) for line in stream]
    rows, limitations = transport_records(args.transport)
    result = {
        "operational": analyze(args.source, args.market, permission_reload_required=False),
        "transport": transport_metrics(rows, snapshots, manifest, limitations),
    }
    args.output.write_text(json.dumps(result, indent=2))
    print(args.output)
