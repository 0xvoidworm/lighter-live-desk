"""Offline Decimal analysis of one completed passive 20-minute recording."""

import argparse
import collections
import csv
import gzip
import json
import statistics
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def stats(values):
    values = sorted(values)
    if not values:
        return None

    def percentile(p):
        index = Decimal(len(values) - 1) * Decimal(str(p))
        lo = int(index)
        fraction = index - lo if isinstance(values[lo], Decimal) else float(index - lo)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * fraction

    return {
        "n": len(values),
        "min": values[0],
        "p05": percentile(0.05),
        "median": statistics.median(values),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": values[-1],
    }


def book_reversion_estimate(buy, sell, local_mid, target_mid, residual, fee_bps, buffer_bps):
    """Two same-size Lighter VWAP legs; hypothetical parallel book reversion.

    Spread/impact are already in VWAP. Metadata fee is an assumption, never an
    individual fill fee. A future exit at target_mid is not guaranteed executable.
    """
    side = "buy" if residual < 0 else "sell"
    entry, opposite = (buy, sell) if side == "buy" else (sell, buy)
    projected_exit = opposite * target_mid / local_mid
    gross = ((projected_exit - entry) if side == "buy" else (entry - projected_exit)) / entry
    return {
        "hypothetical_side": side,
        "projected_exit_price_at_basis": projected_exit,
        "edge_after_two_local_book_legs_bps": gross * 10000,
        "roundtrip_fee_assumption_bps": fee_bps * 2,
        "uncertainty_buffer_assumption_bps": buffer_bps,
        "edge_after_fee_assumption_and_5bps_buffer": gross * 10000 - fee_bps * 2 - buffer_bps,
        "fee_exact": None,
    }


def main(source, prefix):
    manifest = json.loads((source / "started.json").read_text())
    finish = json.loads((source / "finished.json").read_text())
    assert finish["analysis_window_seconds"] == 1200
    assert finish["configuration_unchanged"] and finish["source_hashes_unchanged"]
    with gzip.open(source / "snapshots.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    intervals = []
    block_seconds = collections.Counter()
    patterns = collections.Counter()
    pair_overlap = collections.Counter()
    root_causes = collections.Counter()
    ready = longest = current = 0.0
    basis_seconds = 0.0
    transitions = []
    calls = {}
    active_calls = {}
    reconciles = {}
    binance_calls = {}
    prior = None
    initial_unknown = rows[0]["elapsed_s"]
    if initial_unknown:
        block_seconds["collector_initialization"] += initial_unknown
        patterns[("collector_initialization",)] += initial_unknown
        intervals.append(
            {
                "row": {"elapsed_s": 0},
                "seconds": initial_unknown,
                "ready": False,
                "blockers": ["collector_initialization"],
                "residual": None,
            }
        )
    for i, r in enumerate(rows):
        dt = (rows[i + 1]["elapsed_s"] if i + 1 < len(rows) else 1200) - r["elapsed_s"]
        s = r.get("status")
        d = s.get("diagnostic_observation", {}) if s else {}
        pid = s.get("engine_pid") if s else None
        b = list(d.get("blockers", ["collector_unavailable"]))
        if s and s["live_blockers"]:
            for text, key in [
                ("batas loss harian sudah dipicu", "daily_loss_latch"),
                ("jurnal tidak sehat", "journal"),
                ("siklus posisi sebelumnya belum bersih", "lifecycle_idle"),
            ]:
                if text in s["live_blockers"] and key not in b:
                    b.append(key)
        if (
            s
            and d.get("observed_at", 0)
            < datetime.fromisoformat(r["captured_at_utc"]).timestamp() - 1
        ):
            b.append("collector_stale_engine_status")
        b = sorted(set(b))
        ok = not b
        if ok:
            ready += dt
            current += dt
            longest = max(longest, current)
        else:
            current = 0.0
        for name in b:
            block_seconds[name] += dt
        patterns[tuple(b)] += dt
        for a, j in enumerate(b):
            for k in b[a + 1 :]:
                pair_overlap[(j, k)] += dt
        if s:
            for name, q in d.get("quotes", {}).items():
                if q is None or q["age_ms"] >= manifest["parameters"]["max_data_age_ms"]:
                    root_causes["quote_" + name + "_invalid_or_stale"] += dt
            if d.get("fx_age_s") is None or d["fx_age_s"] >= 15:
                root_causes["fx_stale_or_missing"] += dt
            for name, ch in s["feed_channels"].items():
                if not ch["connected"]:
                    root_causes[name + "_disconnected"] += dt
            if not s["coverage"]["warmup_complete"]:
                root_causes["initial_warmup_incomplete"] += dt
            elif not s["coverage"]["coverage_complete"]:
                root_causes["coverage_after_warmup"] += dt
            assert not s["allow_live_orders"] and s["strategy_session"]["status"] == "closed"
            for c in s["transport_trace"]["recent"]:
                calls[(pid, c["id"])] = c
            for c in s["transport_trace"]["active"]:
                active_calls[(pid, c["id"])] = c
            for c in s["binance_transport_trace"]["recent"]:
                binance_calls[(pid, c["id"])] = c
            rec = s.get("reconcile_diagnostic")
            if rec:
                reconciles[(pid, rec["finished_at"])] = rec
            decision = s["decision"]
            residual = (
                (Decimal(decision["deviation_bps"]) - Decimal(decision["basis_bps"]))
                if decision["basis_bps"] is not None
                else None
            )
            if residual is not None:
                basis_seconds += dt
            state = (
                s["coverage"]["ready"],
                tuple(b),
                tuple((k, v["reconnects"]) for k, v in s["feed_channels"].items()),
            )
            if state != prior:
                transitions.append(
                    {
                        "elapsed_s": r["elapsed_s"],
                        "utc": r["captured_at_utc"],
                        "technical_ready": ok,
                        "blockers": b,
                        "coverage": s["coverage"],
                        "feeds": s["feed_channels"],
                        "request_budget": s["request_budget"],
                    }
                )
                prior = state
        else:
            residual = None
        intervals.append(
            {"row": r, "seconds": dt, "ready": ok, "blockers": b, "residual": residual}
        )
    start = manifest["start_epoch"]
    end = manifest["deadline_epoch"]
    all_observed_starts = sorted(c["started_at"] for c in calls.values() if c["started_at"] < end)
    carry_in = [c for c in calls.values() if c["started_at"] < start <= c["finished_at"] < end]
    unfinished = [
        c for key, c in active_calls.items() if key not in calls and start <= c["started_at"] < end
    ]
    calls = {key: c for key, c in calls.items() if start <= c["started_at"] < end}
    reconciles = {key: c for key, c in reconciles.items() if start <= c["finished_at"] < end}
    binance_calls = {key: c for key, c in binance_calls.items() if start <= c["started_at"] < end}
    endpoints = {}
    for path in sorted({c["path"] for c in calls.values()}):
        batch = [c for c in calls.values() if c["path"] == path]
        phases = collections.Counter()
        pagination = collections.Counter()
        for c in batch:
            for phase, ms in c["phase_ms"].items():
                phases[phase] += ms
            p = c.get("pagination")
            if p:
                pagination[
                    (p["items"], p["requested_cursor"], p["returned_cursor"], p["cursor_echo"])
                ] += 1
        endpoints[path] = {
            "requests_started_and_completed_observed": len(batch),
            "reasons": dict(collections.Counter(c.get("reason", "unclassified") for c in batch)),
            "local_claimed_weight": sum(c.get("local_quota_weight", 1) for c in batch),
            "http_attempts": sum(c["http_attempts"] for c in batch),
            "retries_observed": sum(max(0, c["http_attempts"] - 1) for c in batch),
            "server_response_observed": sum(c["status"] is not None for c in batch),
            "outcomes": dict(collections.Counter(c["outcome"] for c in batch)),
            "elapsed_ms": stats([c["elapsed_ms"] for c in batch]),
            "phase_ms_sum": dict(phases),
            "failures": [
                {"utc_epoch": c["finished_at"], "error": c["error"], "phase": c["last_phase"]}
                for c in batch
                if c["outcome"] != "ok"
            ],
            "pagination_patterns": [
                {
                    "items": k[0],
                    "requested_cursor": k[1],
                    "returned_cursor": k[2],
                    "cursor_echo": k[3],
                    "count": n,
                }
                for k, n in pagination.items()
            ],
        }
    groups = {}
    for name, subset in [
        ("all_with_basis", [v for v in intervals if v["residual"] is not None]),
        ("technical_ready", [v for v in intervals if v["ready"] and v["residual"] is not None]),
        (
            "technical_blocked_with_basis",
            [v for v in intervals if not v["ready"] and v["residual"] is not None],
        ),
    ]:
        vals = [v["residual"] for v in subset]
        groups[name] = {
            "seconds": sum(v["seconds"] for v in subset),
            "signed_bps": stats(vals),
            "absolute_bps": stats([abs(v) for v in vals]),
            "excursions": {},
        }
        for level in [Decimal(1), Decimal(2), Decimal(5), Decimal(8)]:
            for sign in ["positive", "negative", "absolute"]:
                total = peak = run = 0.0
                for v in intervals:
                    belongs = v["residual"] is not None and (
                        name == "all_with_basis"
                        or (v["ready"] if name == "technical_ready" else not v["ready"])
                    )
                    x = v["residual"]
                    passes = belongs and (
                        x > level
                        if sign == "positive"
                        else x < -level
                        if sign == "negative"
                        else abs(x) > level
                    )
                    run = run + v["seconds"] if passes else 0
                    if passes:
                        total += v["seconds"]
                        peak = max(peak, run)
                groups[name]["excursions"][f"{sign}_gt_{level}_bps"] = {
                    "seconds": total,
                    "longest_s": peak,
                }
    economics = []
    for v in intervals:
        if v["residual"] is None:
            continue
        s = v["row"]["status"]
        d = s["diagnostic_observation"]
        orders = d["minimum_orders"]
        if not d["decision_input_match"] or not all(
            k in orders and not orders[k].get("error") and orders[k].get("local_vwap")
            for k in ["buy", "sell"]
        ):
            continue
        if orders["buy"]["quantity"] != orders["sell"]["quantity"]:
            continue
        ql, qb = d["quotes"]["lighter"], d["quotes"]["binance"]
        if not ql or not qb or not d["fx"]:
            continue
        local_mid = (Decimal(ql["bid"]) + Decimal(ql["ask"])) / 2
        binance_mid = (Decimal(qb["bid"]) + Decimal(qb["ask"])) / 2
        basis = Decimal(s["decision"]["basis_bps"])
        fx = Decimal(d["fx"])
        target = (binance_mid / fx) * (1 + basis / 10000)
        estimate = book_reversion_estimate(
            Decimal(orders["buy"]["local_vwap"]),
            Decimal(orders["sell"]["local_vwap"]),
            local_mid,
            target,
            v["residual"],
            Decimal(orders["buy"]["metadata_fee_bps"]),
            Decimal(5),
        )
        economics.append(
            {
                "elapsed_s": v["row"]["elapsed_s"],
                "utc": v["row"]["captured_at_utc"],
                "technical_ready": v["ready"],
                "blockers": v["blockers"],
                "residual_bps": v["residual"],
                "raw_bps": s["decision"]["deviation_bps"],
                "basis_bps": basis,
                "binance_mid_normalized_usdc": binance_mid / fx,
                "target_mid_at_basis": target,
                "original_signal_side": s["decision"]["side"],
                "original_signal_filters": s["decision"]["reasons"],
                "threshold_bps": max(Decimal(8), 4 * Decimal(s["decision"]["scale_bps"])),
                "observed_buy_sell_quotes": d["quotes"],
                "receipt_skew_ms": d["receipt_skew_ms"],
                "fx": d["fx"],
                "fx_age_s": d["fx_age_s"],
                "minimum_orders": orders,
                **estimate,
            }
        )
    largest = []
    for name, items in [
        ("all", economics),
        ("technical_ready", [v for v in economics if v["technical_ready"]]),
    ]:
        peaks = []
        for v in sorted(items, key=lambda x: abs(x["residual_bps"]), reverse=True):
            if any(abs(v["elapsed_s"] - p["elapsed_s"]) < 30 for p in peaks):
                continue
            peaks.append(v.copy())
            if len(peaks) == 5:
                break
        for p in peaks:
            p["group"] = name
            p["followup"] = {}
            for delay in [1, 5, 10, 30]:
                later = next(
                    (r for r in intervals if r["row"]["elapsed_s"] >= p["elapsed_s"] + delay), None
                )
                if later and later["residual"] is not None:
                    qs = later["row"]["status"]["diagnostic_observation"]["quotes"]["lighter"]
                    if qs:
                        p["followup"][str(delay)] = {
                            "residual_bps": later["residual"],
                            "technical_ready": later["ready"],
                            "local_bid": qs["bid"],
                            "local_ask": qs["ask"],
                        }
            largest.append(p)
    budgets = [r["status"]["request_budget"] for r in rows if "status" in r]
    latest = next(r["status"] for r in reversed(rows) if "status" in r)
    raw_peak = min(
        (
            v
            for v in intervals
            if v["row"].get("status", {}).get("decision", {}).get("deviation_bps")
        ),
        key=lambda v: Decimal(v["row"]["status"]["decision"]["deviation_bps"]),
    )
    market_summary = source / "market-window-summary.json"
    recorder_summary = json.loads(market_summary.read_text()) if market_summary.exists() else None
    if recorder_summary:
        recorder_summary.pop("events", None)
    result = {
        "manifest": manifest,
        "finish": finish,
        "sampling": (
            "local status ~4Hz, left-held intervals; durations are snapshot estimates, "
            "not proof of sub-sample continuity"
        ),
        "sample_count": len(rows),
        "sampled_analysis_seconds": sum(v["seconds"] for v in intervals),
        "max_sample_interval_s": max(v["seconds"] for v in intervals),
        "technical_ready_seconds": ready,
        "technical_ready_longest_interval_s": longest,
        "basis_available_seconds": basis_seconds,
        "basis_unavailable_seconds": 1200 - basis_seconds,
        "blocker_seconds_overlapping": dict(block_seconds),
        "root_condition_seconds_overlapping": dict(root_causes),
        "blocker_combinations": [
            {"blockers": list(k), "seconds": n} for k, n in patterns.most_common()
        ],
        "pair_overlap_seconds": [
            {"blockers": list(k), "seconds": n} for k, n in pair_overlap.most_common()
        ],
        "request_endpoints": endpoints,
        "request_rolling_window": {
            "started_in_window": len(calls) + len(unfinished),
            "mean_per_minute_over_20m": (len(calls) + len(unfinished)) / 20,
            "maximum_observed_starts_per_rolling_60s": max(
                (
                    sum(t - 60 < x <= t for x in all_observed_starts)
                    for t in all_observed_starts
                    if start <= t < end
                ),
                default=0,
            ),
            "maximum_local_accounted_used_per_rolling_60s": max(
                b["used_last_60s"] for b in budgets
            ),
            "unconsumed_reservations_peak": max(b.get("reserved_unconsumed", 0) for b in budgets),
            "scope": (
                "SDK GET starts observed by trace, including carry-in starts retained; "
                "other processes/SDK startup outside the recording are not measured"
            ),
        },
        "account_age_seconds": stats(
            [r["status"]["account"]["age_s"] for r in rows if r.get("status", {}).get("account")]
        ),
        "cash_audit_matched_seconds": sum(
            v["seconds"]
            for v in intervals
            if v["row"].get("status", {}).get("net_cash_audit", {}).get("status") == "matched"
        ),
        "request_window_boundary": {
            "completed_carry_in_requests": carry_in,
            "observed_started_not_completed_before_last_sample": unfinished,
            "end_of_window_unobserved_completions_are_not_assumed": True,
        },
        "existing_recorder_observations": recorder_summary,
        "reconciliation_count": len(reconciles),
        "reconciliation_outcomes": dict(
            collections.Counter(r["outcome"] for r in reconciles.values())
        ),
        "reconciliation_elapsed_ms": stats([r["elapsed_ms"] for r in reconciles.values()]),
        "reconciliation_lock_wait_ms": stats([r["lock_wait_ms"] for r in reconciles.values()]),
        "reconciliation_request_counts": dict(
            collections.Counter(len(r["requests"]) for r in reconciles.values())
        ),
        "reconciliation_failures": [r for r in reconciles.values() if r["outcome"] != "ok"],
        "binance_trace_requests": list(binance_calls.values()),
        "budget_observed_used": stats([b["used_last_60s"] for b in budgets]),
        "minimum_reserved_transaction_slots": min(
            b["transaction_slots_remaining"] for b in budgets
        ),
        "local_caps": {
            "normal": 40,
            "urgent_read": 47,
            "transaction": 55,
            "entry_checks": {"normal_cost": 2, "urgent_read_cost": 14, "used_must_not_exceed": 33},
        },
        "no_order_attempts": True,
        "fee_exact_unknown_count": latest["account"]["pending_fee_count"],
        "groups": groups,
        "raw_mid_deviation_bps": stats(
            [
                Decimal(r["status"]["decision"]["deviation_bps"])
                for r in rows
                if "status" in r and r["status"]["decision"]["deviation_bps"] is not None
            ]
        ),
        "largest_negative_raw_mid_deviation": {
            "utc": raw_peak["row"]["captured_at_utc"],
            "elapsed_s": raw_peak["row"]["elapsed_s"],
            "decision": raw_peak["row"]["status"]["decision"],
            "blockers": raw_peak["blockers"],
            "quotes": raw_peak["row"]["status"]["diagnostic_observation"]["quotes"],
            "fx_age_s": raw_peak["row"]["status"]["diagnostic_observation"]["fx_age_s"],
        },
        "largest_deviations": largest,
        "formal_signals_observed": sum(
            bool(r["status"]["decision"]["side"]) for r in rows if "status" in r
        ),
        "technical_formal_signals_observed": sum(
            bool(v["row"]["status"]["decision"]["side"]) for v in intervals if v["ready"]
        ),
        "economics_technical_ready": {
            "estimates": len([v for v in economics if v["technical_ready"]]),
            "edge_after_two_legs_bps": stats(
                [v["edge_after_two_local_book_legs_bps"] for v in economics if v["technical_ready"]]
            ),
            "edge_after_fee_and_buffer_bps": stats(
                [
                    v["edge_after_fee_assumption_and_5bps_buffer"]
                    for v in economics
                    if v["technical_ready"]
                ]
            ),
            "positive_after_assumptions_samples": sum(
                v["technical_ready"] and v["edge_after_fee_assumption_and_5bps_buffer"] > 0
                for v in economics
            ),
        },
        "economics_assumptions": (
            "same-size two Lighter VWAP legs; opposite book shifts proportionally to "
            "prior-basis target with Binance/FX held fixed; full spread included once in "
            "VWAPs; no extra spread subtraction. Hypothetical reversion, fee model from "
            "metadata, exact fee NULL, 5bps configured uncertainty buffer. "
            "Not fill/PnL or measured latency."
        ),
        "max_event_loop_lag_ms": max(
            r["status"]["event_loop_lag_ms"] for r in rows if "status" in r
        ),
        "transitions": transitions,
        "final_observed": {
            "utc": rows[-1]["captured_at_utc"],
            "state": latest["state"],
            "account": latest["account"],
            "net_cash_audit": latest["net_cash_audit"],
            "allow_live_orders": latest["allow_live_orders"],
            "session_status": latest["strategy_session"]["status"],
            "reconnects": latest["reconnects"],
        },
    }
    prefix.with_suffix(".json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    with prefix.with_suffix(".csv").open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["elapsed_s", "interval_s", "technical_ready", "residual_bps", "blockers"])
        writer.writerows(
            [
                v["row"]["elapsed_s"],
                v["seconds"],
                v["ready"],
                v["residual"],
                ";".join(v["blockers"]),
            ]
            for v in intervals
        )
    print(prefix.with_suffix(".json"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    args = parser.parse_args()
    main(args.source, args.output_prefix)
