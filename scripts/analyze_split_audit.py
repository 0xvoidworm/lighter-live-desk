"""Offline analysis of the single split-lane recording; no network or signer."""

import argparse
import gzip
import json
import statistics
from collections import Counter, defaultdict, deque
from itertools import chain
from pathlib import Path


def describe(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {
        "n": len(values),
        "median": statistics.median(values),
        "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
        "max": max(values),
    }


def market_evidence(directory: Path, manifest: dict):
    """Read only this process's already-recorded events inside the frozen window."""
    counts, previous, gaps = Counter(), {}, defaultdict(list)
    exceptional = []
    start, end = manifest["start_epoch"], manifest["deadline_epoch"]
    for path in sorted(directory.glob("market-*.jsonl.gz")):
        with gzip.open(path, "rt") as stream:
            first = json.loads(next(stream))
            if first.get("pid") != manifest["expected_engine_pid"]:
                continue
            # The first row is a real event, not a metadata header.
            for value in chain((first,), (json.loads(line) for line in stream)):
                stamp = value["receipt_ns"] / 1e9
                if stamp >= end:
                    break
                if stamp < start:
                    continue
                key = value["venue"] + "/" + value["kind"]
                counts[key] += 1
                if key in previous:
                    gaps[key].append(stamp - previous[key])
                previous[key] = stamp
                if value["kind"] in {"disconnect", "gap", "overflow", "error"}:
                    exceptional.append({"elapsed_s": stamp - start, "channel": key})
    return {
        "method": "existing market recorder, same PID, same 1200-second window; receipt gaps",
        "event_counts": dict(counts),
        "max_inter_event_gap_s": {key: max(values) for key, values in gaps.items()},
        "disconnect_gap_overflow_error_events": exceptional,
    }


def compare_baseline(path: Path, after: dict):
    before = json.loads(path.read_text())
    return {
        "source_file": str(path),
        "code_commit": before["manifest"]["code_commit"],
        "duration_seconds": before["manifest"]["duration_seconds"],
        "before": {
            "requests_per_minute": before["request_rolling_window"]["mean_per_minute_over_20m"],
            "sdk_rolling_60s_peak": before["request_rolling_window"][
                "maximum_observed_starts_per_rolling_60s"
            ],
            "account_max_age_s": before["account_age_seconds"]["max"],
            "active_orders_max_age_s": None,
            "quota_block_s": before["blocker_seconds_overlapping"]["entry_request_reserve"],
            "evaluation_ready_s": before["technical_ready_seconds"],
            "evaluation_longest_s": before["technical_ready_longest_interval_s"],
            "complete_history_audits": before["reconciliation_count"],
            "minimum_exit_slots": before["minimum_reserved_transaction_slots"],
        },
        "after": {
            "requests_per_minute": after["requests_per_minute"],
            "sdk_rolling_60s_peak": after["sdk_rolling_60s_peak"],
            "account_max_age_s": after["component_ages_s"]["account"]["max"],
            "active_orders_max_age_s": after["component_ages_s"]["active_orders"]["max"],
            "quota_block_s": after["durations_s"]["entry_request_reserve"],
            "evaluation_ready_s": after["durations_s"]["signal_evaluation_ready"],
            "evaluation_longest_s": after["signal_evaluation_longest_s"],
            "complete_history_audits": sum(after["complete_audit_outcomes"].values()),
            "minimum_exit_slots": after["minimum_transaction_slots_remaining"],
        },
        "limits": (
            "Independent windows, changed history guard explicitly authorized; "
            "new engine had ~55s startup before capture. Readiness gain is not a pure "
            "scheduler causal estimate. Old order-component age was not separately recorded."
        ),
    }


def analyze(source: Path):
    manifest = json.loads((source / "started.json").read_text())
    finish = json.loads((source / "finished.json").read_text())
    duration = manifest["duration_seconds"]
    with gzip.open(source / "snapshots.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    seconds = Counter()
    overlaps = Counter()
    # The first local status read starts a few milliseconds after the marker.
    seconds["capture_unknown"] = rows[0]["elapsed_s"]
    overlaps[("capture_unknown",)] = rows[0]["elapsed_s"]
    calls, active, audits, states, preflights = {}, {}, {}, {}, {}
    ages = defaultdict(list)
    runs, run_start, run_end = [], None, None
    proof_seconds = proof_longest = proof_run = 0.0
    min_exit = 55
    quota_pass_coverage = coverage_seconds = 0.0
    observed_gaps = []
    budgets = defaultdict(list)
    history_ages = []
    readiness_transitions = []
    previous_blockers = None
    last = None
    for i, row in enumerate(rows):
        start = row["elapsed_s"]
        end = rows[i + 1]["elapsed_s"] if i + 1 < len(rows) else duration
        dt = max(0.0, min(duration, end) - max(0.0, start))
        observed_gaps.append(dt)
        data = row.get("status")
        if not data:
            seconds["capture_unknown"] += dt
            overlaps[("capture_unknown",)] += dt
            if run_start is not None:
                runs.append(run_end - run_start)
                run_start = None
            proof_run = 0
            continue
        last = data
        assert data["allow_live_orders"] is False and data["entries_stopped"] is True
        assert data["strategy_session"]["status"] == "closed" and not data["pending_orders"]
        assert not data["preflight"]["send_authorization"]
        if data.get("account"):
            assert float(data["account"]["position_qty"]) == 0
            assert data["account"]["exchange_active_order_count"] == 0
            assert data["account"]["other_exchange_position_count"] == 0
            for name, age in data["account"].get("component_ages_s", {}).items():
                ages[name].append(age)
                if age is None or age >= 15:
                    seconds["stale_component_" + name] += dt
        observation = data["diagnostic_observation"]
        blockers = tuple(sorted(observation["blockers"]))
        if blockers != previous_blockers:
            readiness_transitions.append({"elapsed_s": start, "blockers": list(blockers)})
            previous_blockers = blockers
        for blocker in blockers:
            seconds[blocker] += dt
        overlaps[blockers] += dt
        if observation["technical_ready"]:
            seconds["signal_evaluation_ready"] += dt
            if run_start is None:
                run_start = start
            run_end = min(duration, end)
        elif run_start is not None:
            runs.append(run_end - run_start)
            run_start = None
        if observation["checks"]["coverage"]:
            coverage_seconds += dt
            if not observation["checks"]["entry_request_reserve"]:
                quota_pass_coverage += dt
        budget = data["request_budget"]
        budgets["consumed"].append(budget["used_last_60s"])
        budgets["unconsumed_reserved"].append(budget["reserved_unconsumed"])
        budgets["consumed_plus_reserved"].append(
            budget["used_last_60s"] + budget["reserved_unconsumed"]
        )
        min_exit = min(min_exit, budget["transaction_slots_remaining"])
        used = budget["used_last_60s"] + budget["reserved_unconsumed"]
        # A completed proof is not send-ready if capacity disappeared after issue.
        proof_ready = (
            data["preflight"]["ready_to_send_technically"] and used + 2 <= 40 and used + 14 <= 47
        )
        if proof_ready:
            proof_seconds += dt
            proof_run += dt
            proof_longest = max(proof_longest, proof_run)
        else:
            proof_run = 0
        if data["history_audit"]["complete"]:
            seconds["history_complete"] += dt
        history_ages.append(data["history_audit"].get("age_seconds"))
        for record in data["transport_trace"]["recent"]:
            calls[record["id"]] = record
        for record in data["transport_trace"]["active"]:
            active[record["id"]] = record
        for key, target in (("reconcile_diagnostic", audits), ("account_state_diagnostic", states)):
            record = data.get(key)
            if record:
                target[record["finished_at"]] = record
        record = data["preflight"].get("diagnostic")
        if record:
            preflights[record["attempt"]] = record
    if run_start is not None:
        runs.append(run_end - run_start)
    start_epoch, end_epoch = manifest["start_epoch"], manifest["deadline_epoch"]
    observed = {**active, **calls}
    within = [r for r in observed.values() if start_epoch <= r["started_at"] < end_epoch]
    endpoints = {}
    for path in sorted({r["path"] for r in within}):
        records = [r for r in within if r["path"] == path]
        endpoints[path] = {
            "starts": len(records),
            "local_weight_total": sum(r.get("local_quota_weight", 1) for r in records),
            "reasons": dict(Counter(r.get("reason", "unknown") for r in records)),
            "durations_ms": describe([r.get("elapsed_ms") for r in records]),
            "outcomes": dict(Counter(r.get("outcome", "in_flight") for r in records)),
            "retries_observed": sum(max(0, r.get("http_attempts", 1) - 1) for r in records),
            "failed_phases": dict(
                Counter(r.get("last_phase") for r in records if r.get("outcome") == "error")
            ),
            "phase_durations_ms": {
                phase: describe([r.get("phase_ms", {}).get(phase, 0) for r in records])
                for phase in sorted({k for r in records for k in r.get("phase_ms", {})})
            },
        }
    rolling, peak = deque(), 0
    for record in sorted(observed.values(), key=lambda r: r["started_at"]):
        stamp = record["started_at"]
        while rolling and stamp - rolling[0] >= 60:
            rolling.popleft()
        rolling.append(stamp)
        if start_epoch <= stamp < end_epoch:
            peak = max(peak, len(rolling))
    full_records = [r for r in audits.values() if start_epoch <= r["finished_at"] < end_epoch]
    state_records = [r for r in states.values() if start_epoch <= r["finished_at"] < end_epoch]
    preflight_intervals = [
        (r["finished_at"] - r["elapsed_ms"] / 1000 - start_epoch, r["finished_at"] - start_epoch)
        for r in preflights.values()
    ]
    quota_during_preflight = 0.0
    quota_samples_outside_preflight = 0
    for i, row in enumerate(rows):
        if (
            not row.get("status")
            or row["status"]["diagnostic_observation"]["checks"]["entry_request_reserve"]
        ):
            continue
        start = row["elapsed_s"]
        end = rows[i + 1]["elapsed_s"] if i + 1 < len(rows) else duration
        if not any(a <= start <= b for a, b in preflight_intervals):
            quota_samples_outside_preflight += 1
        for preflight_start, preflight_end in preflight_intervals:
            quota_during_preflight += max(0, min(end, preflight_end) - max(start, preflight_start))
    result = {
        "manifest": manifest,
        "completion": finish,
        "method": (
            "existing local snapshots, left-hold intervals; SDK GET starts incl in-flight; "
            "no sign/broadcast; send cost simulated only"
        ),
        "sample_count": len(rows),
        "max_sample_gap_s": max(observed_gaps),
        "request_starts": len(within),
        "requests_per_minute": len(within) * 60 / duration,
        "sdk_rolling_60s_peak": peak,
        "component_ages_s": {k: describe(v) for k, v in ages.items()},
        "local_rolling_budget": {k: describe(v) for k, v in budgets.items()},
        "history_audit_age_s": describe(history_ages),
        "durations_s": dict(seconds),
        "blocker_overlap_s": [{"blockers": list(k), "seconds": v} for k, v in overlaps.items()],
        "readiness_transitions": readiness_transitions,
        "signal_evaluation_longest_s": max(runs, default=0),
        "coverage_pass_seconds": coverage_seconds,
        "quota_block_when_coverage_pass_s": quota_pass_coverage,
        "quota_block_during_own_preflight_s": quota_during_preflight,
        "quota_block_outside_preflight_s": (
            seconds["entry_request_reserve"] - quota_during_preflight
        ),
        "quota_block_samples_outside_preflight": quota_samples_outside_preflight,
        "interval_uncertainty_s": max(observed_gaps),
        "quota_block_fraction_when_coverage_pass": quota_pass_coverage / coverage_seconds
        if coverage_seconds
        else None,
        "completed_preflight_ready_without_send_s": proof_seconds,
        "completed_preflight_longest_s": proof_longest,
        "complete_audit_outcomes": dict(Counter(r["outcome"] for r in full_records)),
        "complete_audit_costs": dict(Counter(str(len(r["requests"])) for r in full_records)),
        "full_audit_durations_ms": describe([r["elapsed_ms"] for r in full_records]),
        "full_audit_lock_wait_ms": describe([r["lock_wait_ms"] for r in full_records]),
        "state_refresh_outcomes": dict(Counter(r["outcome"] for r in state_records)),
        "state_refresh_costs": dict(Counter(str(len(r["requests"])) for r in state_records)),
        "state_refresh_durations_ms": describe([r["elapsed_ms"] for r in state_records]),
        "preflights": list(preflights.values()),
        "minimum_transaction_slots_remaining": min_exit,
        "endpoints": endpoints,
        "final_observed": {
            k: last.get(k)
            for k in (
                "state",
                "account",
                "history_audit",
                "request_budget",
                "allow_live_orders",
                "entries_stopped",
                "reconnects",
                "net_cash_audit",
                "fee_lookup",
                "fee_reports",
            )
        },
    }
    assert abs(sum(overlaps.values()) - duration) < 0.001
    assert min_exit >= 15
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--market-directory", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    result = analyze(args.source)
    if args.market_directory:
        result["market_evidence"] = market_evidence(args.market_directory, result["manifest"])
    if args.baseline:
        result["baseline_comparison"] = compare_baseline(args.baseline, result)
    args.output.write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "request_starts",
                    "requests_per_minute",
                    "sdk_rolling_60s_peak",
                    "signal_evaluation_longest_s",
                    "minimum_transaction_slots_remaining",
                )
            }
        )
    )
