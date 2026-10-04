"""Offline analysis of saved desk status; never connects, signs or changes the engine.

Python follows this project's existing engine/SDK toolchain. Inputs remain private;
outputs contain derived metrics and public market observations only.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import statistics
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def read_rows(path):
    return [json.loads(line) for line in path.open() if line.strip()]


def epoch(row):
    return datetime.fromisoformat(row["observed_at_utc"]).timestamp()


def distribution(values):
    values = sorted(values)
    if not values:
        return None

    def quantile(p):
        index = Decimal(len(values) - 1) * Decimal(str(p))
        lo = int(index)
        fraction = index - lo
        if not isinstance(values[lo], Decimal):
            fraction = float(fraction)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * fraction

    return {
        "n": len(values),
        "min": values[0],
        "p05": quantile(0.05),
        "median": statistics.median(values),
        "p95": quantile(0.95),
        "p99": quantile(0.99),
        "max": values[-1],
    }


def blockers(row):
    result = []
    decision = row["decision"]
    account = row.get("account") or {}
    if decision["basis_bps"] is None or Decimal(decision.get("scale_bps") or 0) <= 0:
        result.append("basis_or_scale_unavailable")
    if not row["coverage"]["ready"]:
        result.append("coverage")
    if not all(q["valid"] for q in row["quotes"].values()):
        result.append("quote_age_or_book")
    if not all(c["connected"] for c in row["feed_channels"].values()):
        result.append("feed_channel")
    if account.get("age_s", 999) >= 15 or account.get("funding_audit_age_s", 999) >= 15:
        result.append("account_or_funding_age")
    # Same inequalities as RateBudget.can_claim(entry,2) and (probe_read,14).
    used = row["request_budget"]["used_last_60s"]
    if used + 2 > 40 or used + 14 > 47:
        result.append("request_reserve")
    if not row["fee_reports"]["all_unknown_fills_covered"]:
        result.append("fee_evidence")
    if row["net_cash_audit"]["status"] != "matched":
        result.append("cash_audit")
    if row["pending_orders"] or Decimal(account.get("position_qty", 0)):
        result.append("position_or_intent")
    if not row["journal_ok"] or row["state"] != "LIVE" or row["entries_stopped"]:
        result.append("engine_state")
    return result


def analyze(old_path, prior_path, journal_path, new_paths, prefix):
    prior = json.loads(prior_path.read_text())
    session = prior["session"]
    start, stop = session["started_at"], session["entry_stopped_at"]
    rows = [r for r in read_rows(old_path) if start <= epoch(r) <= stop]
    records = []
    by_blocker = collections.Counter()
    primary = collections.Counter()
    for i, row in enumerate(rows):
        dt = min(epoch(rows[i + 1]) if i + 1 < len(rows) else stop, stop) - epoch(row)
        b = blockers(row)
        for name in b:
            by_blocker[name] += dt
        primary[b[0] if b else "observable_guards_pass"] += dt
        decision = row["decision"]
        residual = (
            (Decimal(decision["deviation_bps"]) - Decimal(decision["basis_bps"]))
            if decision["basis_bps"] is not None
            else None
        )
        records.append(
            {
                "at_utc": row["observed_at_utc"],
                "duration_s": dt,
                "residual_bps": residual,
                "eligible_observed": not b,
                "blockers": b,
                "row": row,
            }
        )

    groups = {}
    for name, subset in [
        ("basis_available", [r for r in records if r["residual_bps"] is not None]),
        ("observable_guards_pass", [r for r in records if r["eligible_observed"]]),
        (
            "blocked_with_basis",
            [r for r in records if not r["eligible_observed"] and r["residual_bps"] is not None],
        ),
    ]:
        values = [r["residual_bps"] for r in subset]
        excursions = {}
        for level in [0.5, 1, 2, 3, 5, 8]:
            for direction in ["positive", "negative", "absolute"]:
                total = longest = current = 0.0
                for r in records:
                    value = r["residual_bps"]
                    in_group = value is not None and (
                        name == "basis_available"
                        or (
                            r["eligible_observed"]
                            if name == "observable_guards_pass"
                            else not r["eligible_observed"]
                        )
                    )
                    passes = in_group and (
                        (value > level)
                        if direction == "positive"
                        else (value < -level)
                        if direction == "negative"
                        else (abs(value) > level)
                    )
                    current = current + r["duration_s"] if passes else 0
                    if passes:
                        total += r["duration_s"]
                        longest = max(longest, current)
                excursions[f"{direction}_gt_{level}_bps"] = {
                    "total_s": total,
                    "longest_observed_episode_s": longest,
                }
        spreads = [Decimal(r["row"]["quotes"]["lighter"]["spread_bps"]) for r in subset]
        depth_bounds = {"buy": [], "sell": []}
        for r in subset:
            q = r["row"]["quotes"]["lighter"]
            for side, reference in [("buy", "ask"), ("sell", "bid")]:
                vwap = q[f"{side}_25_vwap"]
                if vwap is not None:
                    impact = (Decimal(vwap) / Decimal(q[reference]) - 1) * 10000
                    depth_bounds[side].append(max(Decimal(0), impact if side == "buy" else -impact))
        groups[name] = {
            "duration_s": sum(r["duration_s"] for r in subset),
            "signed_bps": distribution(values),
            "absolute_bps": distribution([abs(v) for v in values]),
            "excursions": excursions,
            "lighter_spread_bps": distribution(spreads),
            "minimum_size_impact_upper_bound_bps": {
                side: distribution(values) for side, values in depth_bounds.items()
            },
        }

    closest = []
    eligible = [r for r in records if r["eligible_observed"]]
    for direction, record in [
        ("long", min(eligible, key=lambda r: r["residual_bps"])),
        ("short", max(eligible, key=lambda r: r["residual_bps"])),
    ]:
        d, q = record["row"]["decision"], record["row"]["quotes"]["lighter"]
        closest.append(
            {
                "direction": direction,
                "at_utc": record["at_utc"],
                "raw_bps": d["deviation_bps"],
                "basis_bps": d["basis_bps"],
                "residual_bps": record["residual_bps"],
                "threshold_bps": max(Decimal(8), Decimal(d["scale_bps"]) * 4),
                "spread_plus_buffer_bps": Decimal(q["spread_bps"]) + 5,
                "rejected_by": d["reasons"],
                "directional_flow_depth_filters": "not evaluated without threshold candidate",
            }
        )

    observations = []
    for path in new_paths:
        new = read_rows(path)
        calls = {
            c["id"]: c
            for row in new
            for c in row["transport_trace"]["recent"]
            if c["started_at"] >= epoch(new[0])
        }
        reconciles = {
            r["reconcile_diagnostic"]["finished_at"]: r["reconcile_diagnostic"]
            for r in new
            if r.get("reconcile_diagnostic")
            and r["reconcile_diagnostic"]["finished_at"] >= epoch(new[0])
        }
        observations.append(
            {
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "start_utc": new[0]["observed_at_utc"],
                "end_utc": new[-1]["observed_at_utc"],
                "samples": len(new),
                "routes": dict(collections.Counter(c["path"] for c in calls.values())),
                "outcomes": dict(collections.Counter(c["outcome"] for c in calls.values())),
                "request_elapsed_ms": distribution([c["elapsed_ms"] for c in calls.values()]),
                "reconciliation_elapsed_ms": distribution(
                    [r["elapsed_ms"] for r in reconciles.values()]
                ),
                "max_reconcile_lock_wait_ms": max(
                    (r["lock_wait_ms"] for r in reconciles.values()), default=None
                ),
                "max_event_loop_lag_ms": max(r["event_loop_lag_ms"] for r in new),
                "max_queue_backlog": max(r["queue_backlog"] for r in new),
                "minimum_transaction_slots": min(
                    r["request_budget"]["transaction_slots_remaining"] for r in new
                ),
                "reconnects": new[-1]["reconnects"],
                "final": {
                    "equity_usdc": new[-1]["account"]["equity"],
                    "position_qty": new[-1]["account"]["position_qty"],
                    "unresolved_intents": len(new[-1]["pending_orders"]),
                    "exchange_active_order_count": new[-1]["account"].get(
                        "exchange_active_order_count"
                    ),
                    "other_exchange_position_count": new[-1]["account"].get(
                        "other_exchange_position_count"
                    ),
                    "allow_live_orders": new[-1]["allow_live_orders"],
                    "session_status": new[-1]["strategy_session"]["status"],
                },
            }
        )

    warnings = [
        {
            "utc": datetime.fromtimestamp(int(r["__REALTIME_TIMESTAMP"]) / 1e6, UTC).isoformat(),
            "pid": r["_PID"],
            "message": r["MESSAGE"],
        }
        for r in read_rows(journal_path)
        if "reconciliation blocked:" in r["MESSAGE"]
    ]
    result = {
        "baseline_commit": "8814332",
        "session_id": session["id"],
        "session_entry_active_s": stop - start,
        "sampled_interval_s": sum(r["duration_s"] for r in records),
        "max_snapshot_interval_s": max(r["duration_s"] for r in records),
        "sampling_method": (
            "left-held status snapshots; not continuous proof. Historical FX age, queue, "
            "signing-lock state and reconcile success age were not recorded per evaluation."
        ),
        "groups": groups,
        "blocker_seconds_overlapping": dict(by_blocker),
        "primary_blocker_seconds": dict(primary),
        "closest_subthreshold_observations": closest,
        "formal_signal_candidates": sum(bool(r["row"]["decision"]["side"]) for r in records),
        "coverage_transitions": prior["coverage_transitions"],
        "feed_transitions": prior["feed_transitions"],
        "original_process_reconcile_warnings": warnings,
        "readonly_observations": observations,
        "historical_source_sha256": hashlib.sha256(old_path.read_bytes()).hexdigest(),
        "historical_fee_exact_unknown_count": 4,
        "threshold_origin": (
            "4b5af97, hardcoded unvalidated heuristic max(8,4*MAD*1.4826); "
            "no measured calibration found"
        ),
        "legacy_recording_limit": (
            "hour-14 gzip corrupt and not tied to engine run; replay source quote mismatch; "
            "no defensible full depth/latency replay"
        ),
        "depth_bound_method": (
            "0 <= minimum-size impact <= saved $25 VWAP impact versus BBO, same side/book; "
            "minimum quantity .00012 BTC (~$10.2) below saved quote size. "
            "No measured execution slippage."
        ),
        "no_new_authorization_or_broadcast": True,
    }
    prefix.with_suffix(".json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    with prefix.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["utc", "interval_s", "residual_bps", "observable_guards_pass", "blockers"])
        writer.writerows(
            [
                r["at_utc"],
                r["duration_s"],
                r["residual_bps"],
                r["eligible_observed"],
                ";".join(r["blockers"]),
            ]
            for r in records
        )
    print(prefix.with_suffix(".json"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ["historical", "prior_audit", "journal", "output_prefix"]:
        parser.add_argument("--" + key.replace("_", "-"), required=True, type=Path)
    parser.add_argument("--readonly", required=True, nargs="+", type=Path)
    args = parser.parse_args()
    analyze(args.historical, args.prior_audit, args.journal, args.readonly, args.output_prefix)
