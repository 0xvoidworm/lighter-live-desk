"""Analyze immutable local-status evidence; no network, engine, or signing imports.

Python is used to integrate with the existing recording/statistics pipeline.
"""

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

from analyze_transport_observation import distribution


def integrate(rows, left, right):
    blockers, reasons, overlap = Counter(), Counter(), Counter()
    covered = ready = streak = longest = 0.0
    expiry, opened = [], None
    for i, row in enumerate(rows):
        obs = row["status"]["diagnostic_observation"]
        at = obs["observed_at"]
        end = rows[i + 1]["status"]["diagnostic_observation"]["observed_at"] if i + 1 < len(
            rows
        ) else right
        lo, hi = max(at, left), min(end, right)
        if hi <= lo or end - at > 2:
            continue
        dt = hi - lo
        covered += dt
        overlap[tuple(sorted(obs["blockers"]))] += dt
        blockers.update({key: dt for key in obs["blockers"]})
        reason = obs["source_freshness"]["reason"]
        if "source_freshness" in obs["blockers"]:
            reasons[reason] += dt
        if reason == "SOURCE_CLOCK_EVIDENCE_EXPIRED":
            if opened is None:
                opened = lo
        elif opened is not None:
            expiry.append({"start_epoch": opened, "end_epoch": lo})
            opened = None
        if obs["technical_ready"]:
            ready += dt
            streak += dt
            longest = max(longest, streak)
        else:
            streak = 0
    if opened is not None:
        expiry.append({"start_epoch": opened, "end_epoch": right})
    return {
        "denominator_s": right - left,
        "observed_s": covered,
        "unknown_s": max(0, right - left - covered),
        "technical_ready_s": ready,
        "longest_ready_interval_s": longest,
        "blocker_s": dict(blockers),
        "source_reason_blocker_s": dict(reasons),
        "exclusive_overlap_s": [{"blockers": list(k), "seconds": v} for k, v in overlap.items()],
        "expiry_intervals": expiry,
    }


def calibration(attempt):
    events = attempt["transport"]["events"]
    ph = {row["phase"]: row["monotonic_ns"] for row in events}

    def span(begin, end):
        a, b = ph.get(begin, attempt.get(begin)), ph.get(end, attempt.get(end))
        return (b - a) / 1e6 if a is not None and b is not None else None

    row = {key: attempt.get(key) for key in (
        "id", "started_ns", "started_monotonic_ns", "scheduled_monotonic_ns",
        "send_monotonic_ns", "received_ns", "received_monotonic_ns", "returned_server_ms",
        "host", "validation", "outcome", "status", "latest_proof_id", "duration_ms",
        "http_attempts", "http_retries", "weight", "exception", "failed_stage",
    )}
    accepted = (attempt.get("validation") or {}).get("accepted", False)
    row["accepted"] = accepted
    row["phases_ms"] = {
        "resolver": span("dns_resolvehost_start", "dns_resolvehost_end"),
        "connection_create_including_resolver": span(
            "connection_create_start", "connection_create_end"
        ),
        "pool_queue": span("connection_queued_start", "connection_queued_end"),
        "waiting_headers": span("request_headers_sent", "request_end"),
        "body": span("body_started_monotonic_ns", "body_received_monotonic_ns"),
        "decode": span("body_received_monotonic_ns", "decoded_monotonic_ns"),
    }
    row["connection_reused"] = attempt["transport"]["connection_reused"]
    row["headers_send_hooks"] = sum(e["phase"] == "request_headers_sent" for e in events)
    row["trace_cpu_ms"] = attempt["transport"]["callback_cpu_ns"] / 1e6
    row["trace_events_dropped"] = attempt["transport"]["events_dropped"]
    row["resolver_http_attempts"] = attempt.get("resolver_http_attempts")
    row["resolver_http_attempts_dropped"] = attempt.get("resolver_http_attempts_dropped", 0)
    if accepted:
        padding = attempt["host"]["host_error_ms"] + 1
        row["offset_interval_ms"] = [
            attempt["returned_server_ms"] - attempt["received_ns"] / 1e6 - padding,
            attempt["returned_server_ms"] - attempt["started_ns"] / 1e6 + padding,
        ]
        row["proof_expires_monotonic_ns"] = attempt["received_monotonic_ns"] + 120_000_000_000
    return row


def run(directory):
    if (directory / "manifest.json").exists():
        manifest = json.loads((directory / "manifest.json").read_text())
        for row in manifest["files"]:
            path = directory / row["name"]
            assert path.stat().st_size == row["bytes"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    finish = json.loads((directory / "finished.json").read_text())
    if "session" in finish:
        record = finish["session"]
        windows = {
            "preparation": (record["authorized_at"], record["started_at"]),
            "active": (record["started_at"], record["entry_stopped_at"]),
            "cleanup": (record["entry_stopped_at"], record["ended_at"]),
        }
        left, right = record["authorized_at"], record["entry_stopped_at"]
    else:
        started = json.loads((directory / "started.json").read_text())
        left, right = started["start_epoch"], started["deadline_epoch"]
        windows = {"observation": (left, right)}
    rows = []
    attempts = {}
    for line in gzip.open(directory / "snapshots.jsonl.gz", "rt"):
        row = json.loads(line)
        if "status" not in row:
            continue
        rows.append(row)
        for attempt in row["status"].get("source_clock", {}).get("recent", []):
            if left <= attempt["started_ns"] / 1e9 < right:
                attempts[attempt["started_monotonic_ns"]] = attempt
    rows.sort(key=lambda r: r["status"]["diagnostic_observation"]["monotonic_ns"])
    warm = next((r["status"]["diagnostic_observation"]["observed_at"] for r in rows
                 if r["status"].get("coverage", {}).get("warmup_complete")), None)
    if "observation" in windows and warm is not None and warm < right:
        windows["after_warmup"] = (max(left, warm), right)
    calibrations = [calibration(a) for _, a in sorted(attempts.items())]
    account_ages, rolling, loop, backlog = [], [], [], []
    for row in rows:
        s = row["status"]
        if not left <= s["diagnostic_observation"]["observed_at"] < right:
            continue
        account_ages.extend((s.get("account") or {}).get("component_ages_s", {}).values())
        if s.get("request_budget"):
            rolling.append(s["request_budget"]["used_last_60s"])
        loop.append(s.get("event_loop_lag_ms"))
        backlog.append(s.get("queue_backlog"))
    return {
        "window_start_epoch": left, "window_end_epoch": right,
        "status_records": len(rows), "first_warmup_complete_epoch": warm,
        "readiness": {name: integrate(rows, *window) for name, window in windows.items()},
        "calibrations": calibrations, "calibration_count": len(calibrations),
        "accepted": sum(a["accepted"] for a in calibrations),
        "reused": sum(a["connection_reused"] is True for a in calibrations),
        "rtt_ms": distribution([a["duration_ms"] for a in calibrations]),
        "phase_statistics_ms": {k: distribution([a["phases_ms"][k] for a in calibrations])
                                for k in calibrations[0]["phases_ms"]} if calibrations else {},
        "trace_cpu_total_ms": sum(a["trace_cpu_ms"] for a in calibrations),
        "trace_events_dropped": sum(a["trace_events_dropped"] for a in calibrations),
        "account_component_age_s": distribution(account_ages),
        "lighter_request_rolling_used": distribution(rolling),
        "event_loop_lag_ms": distribution(loop), "market_queue_backlog": distribution(backlog),
        "first_reconnect_counters": rows[0]["status"].get("reconnects"),
        "last_reconnect_counters": rows[-1]["status"].get("reconnects"),
        "limits": [
            "Left-step integration of observed diagnostic state; unknown gaps >2s not credited.",
            "Resolver contains lock/cache/DoH; its internal phases and TLS alone are unknown.",
            "Headers hooks count send attempts, not confirmed server receipt.",
            "No missing samples filled and no coverage counterfactual constructed.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.directory), indent=2))
