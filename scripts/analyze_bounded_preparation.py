"""Offline summary of a preparation that never activated a trading session."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def analyze(directory: Path, root: Path) -> dict:
    start = json.loads((directory / "started.json").read_text())
    finish = json.loads((directory / "finished.json").read_text())
    record = finish["session"]
    assert not finish["active_seen"] and record["started_at"] is None
    assert record["deadline_epoch"] is None and record["status"] == "closed"
    assert not record["attempts"] and finish["source_hashes_unchanged"]
    seconds, overlap, evaluations, reasons = Counter(), Counter(), set(), Counter()
    failures, market_intervals, latest = {}, [], None
    peak_used, minimum_exit_capacity, component_max = 0, 55, {}
    fx_max_age, max_sample_gap, longest = 0.0, 0.0, 0.0
    signal_count, eligible_residuals, quotes_invalid_ms = 0, [], {}
    last_market_state = None
    with gzip.open(directory / "snapshots.jsonl.gz", "rt") as source:
        for line in source:
            row = json.loads(line)
            if "status" not in row:
                continue
            status = row["status"]
            observation = status["diagnostic_observation"]
            if latest:
                dt = row["epoch"] - latest["epoch"]
                blockers = tuple(latest["status"]["diagnostic_observation"]["blockers"])
                overlap[blockers] += dt
                for blocker in blockers:
                    seconds[blocker] += dt
                if not blockers:
                    seconds["technical_ready"] += dt
                    longest += dt
            decision = status["decision"]
            key = status["decision_inputs"].get("monotonic_ns")
            if key not in evaluations:
                evaluations.add(key)
                reasons.update(decision["reasons"])
                signal_count += int(decision["side"] is not None)
                if observation["technical_ready"] and observation["decision_input_match"]:
                    if decision["deviation_bps"] is not None and decision["basis_bps"] is not None:
                        eligible_residuals.append(
                            float(decision["deviation_bps"]) - float(decision["basis_bps"])
                        )
            budget = status["request_budget"]
            used = budget["used_last_60s"] + budget["reserved_unconsumed"]
            peak_used = max(peak_used, used)
            minimum_exit_capacity = min(minimum_exit_capacity, 55 - used)
            for name, age in status["account"]["component_ages_s"].items():
                component_max[name] = max(component_max.get(name, 0), age)
            fx_max_age = max(fx_max_age, observation["fx_age_s"] or 0)
            max_sample_gap = max(max_sample_gap, status["coverage"]["max_sample_gap_s"])
            for request in status["fx_diagnostic"]["recent"]:
                if request.get("outcome") == "failed":
                    failures[request["id"]] = request
            market_state = tuple(
                name
                for name in ("market_data", "fx_normalization")
                if not observation["checks"][name]
            )
            if market_state != last_market_state:
                market_intervals.append(
                    {
                        "at_utc": datetime.fromtimestamp(row["epoch"], UTC).isoformat(),
                        "blockers": list(market_state),
                        "quote_ages_ms": {
                            name: quote["age_ms"] if quote else None
                            for name, quote in observation["quotes"].items()
                        },
                        "queue_backlog": status["queue_backlog"],
                        "event_loop_lag_ms": status["event_loop_lag_ms"],
                    }
                )
                last_market_state = market_state
            for name, quote in observation["quotes"].items():
                if quote:
                    quotes_invalid_ms[name] = max(quotes_invalid_ms.get(name, 0), quote["age_ms"])
            latest = row
    assert not eligible_residuals and signal_count == 0 and seconds["technical_ready"] == 0
    connection = sqlite3.connect("file:" + str(root / "state/desk.db") + "?mode=ro", uri=True)
    commands = list(
        connection.execute(
            "SELECT id,kind,state,requested_at,result FROM commands "
            "WHERE requested_at>=? ORDER BY id",
            (start["preparation_started_epoch"],),
        )
    )
    new_intents = list(
        connection.execute(
            "SELECT client_id FROM intents WHERE created_at>=?", (record["authorized_at"],)
        )
    )
    new_fills = list(
        connection.execute(
            "SELECT fill_id FROM fills WHERE received_at>=?", (record["authorized_at"],)
        )
    )
    assert not new_intents and not new_fills
    assert not any(command[1] in {"START_LIVE", "PROBE_ONCE"} for command in commands)
    connection.close()
    final = finish["final_status"]
    return {
        "result": "preparation_blocked_no_activation_no_order",
        "session_id": record["id"],
        "session_record": record,
        "basis_commits": ["283ab14", "5dfd09b"],
        "engine_pid": start["engine_pid"],
        "source_hashes": start["source_hashes"],
        "source_hashes_unchanged": True,
        "frozen_config": start["config"],
        "preparation_started_utc": datetime.fromtimestamp(
            start["preparation_started_epoch"], UTC
        ).isoformat(),
        "preparation_closed_utc": datetime.fromtimestamp(record["ended_at"], UTC).isoformat(),
        "preparation_seconds": record["ended_at"] - start["preparation_started_epoch"],
        "preparation_deadline_utc": datetime.fromtimestamp(
            start["preparation_deadline_epoch"], UTC
        ).isoformat(),
        "active_seconds": 0,
        "entry_attempts": 0,
        "new_intents": 0,
        "new_fills": 0,
        "positions_completed": 0,
        "signal_valid_observed": 0,
        "observed_evaluations": len(evaluations),
        "sampled_filter_counts": dict(reasons),
        "active_eligible_residual_distribution": None,
        "preparation_eligible_residual_count": 0,
        "lost_opportunities_during_preflight": None,
        "activation_preflight_runs": 0,
        "per_entry_preflight_runs": 0,
        "interpretation": "No eligible data: coverage/basis blocked. "
        "Cannot assess market or threshold. "
        "Observed evaluations are deduplicated captured inputs, not a total engine-call counter. "
        "No opportunity lost during preflight can be assessed because preflight never ran.",
        "capture_start_utc": start["collector_started_utc"],
        "capture_finish_utc": finish["finished_at_utc"],
        "status_samples": finish["samples"],
        "local_status_errors": finish["local_status_errors"],
        "technical_ready_s": 0,
        "longest_technical_ready_s": longest,
        "blocker_s": dict(seconds),
        "overlap_s": [{"blockers": list(k), "seconds": v} for k, v in overlap.items()],
        "max_valid_sample_gap_s": max_sample_gap,
        "market_validity_transitions": market_intervals,
        "failed_fx_requests": list(failures.values()),
        "max_fx_conservative_age_s": fx_max_age,
        "max_quote_age_ms": quotes_invalid_ms,
        "max_component_age_s": component_max,
        "peak_lighter_used_including_leases": peak_used,
        "minimum_capacity_to_55": minimum_exit_capacity,
        "commands": commands,
        "final_state": final["state"],
        "order_permission": final["allow_live_orders"],
        "entries_stopped": final["entries_stopped"],
        "final_account": final["account"],
        "cash_audit": final["net_cash_audit"],
        "fee_reports": final["fee_reports"],
        "history_complete": final["history_audit"]["complete"],
        "reconnects": final["reconnects"],
        "raw_snapshot_sha256": hashlib.sha256(
            (directory / "snapshots.jsonl.gz").read_bytes()
        ).hexdigest(),
        "initial_capture_unobserved_preparation_s": datetime.fromisoformat(
            start["collector_started_utc"]
        ).timestamp()
        - start["preparation_started_epoch"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = analyze(args.directory, Path(__file__).resolve().parents[1])
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("Saved bounded preparation summary: " + str(args.output))
