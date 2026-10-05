"""Passive observation of existing books/account. Never grants entry authority."""

from __future__ import annotations

import json
import time
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from .market import dec
from .risk import size_entry

if TYPE_CHECKING:
    from .engine import Engine


def observe(engine: Engine) -> dict:
    now, wall = time.monotonic(), time.time()
    snap, market, config = engine.account_snapshot, engine.market, engine.config
    quotes = {name: book.quote() for name, book in engine.books.items()}
    source_freshness = engine.source_freshness()
    checks = {
        "market_data": engine.data_valid(),
        "source_freshness": source_freshness["passed"],
        "fx_normalization": bool(engine.fx is not None and abs((engine.fx - 1) * 10000) <= 10),
        "coverage": getattr(engine.strategy, "coverage", lambda: {"ready": False})()["ready"],
        "basis_scale": engine.last_decision.basis_bps is not None
        and engine.last_decision.scale_bps is not None
        and engine.last_decision.scale_bps > 0,
        "account_identity": bool(
            engine.account
            and snap
            and getattr(engine.account, "account_index", None) == config.account_index
            and snap.account_type_code == config.expected_account_type_code
        ),
        "margin_settings": bool(snap and engine.margin_settings_valid(snap)),
        "account_fresh": engine.account_state_valid(),
        "reconciliation_fresh": engine.history_valid(),
        "reconciliation_idle": not (engine.reconcile_task and not engine.reconcile_task.done()),
        "entry_request_reserve": engine.entry_rate_ready(),
        "flat_clean": bool(
            snap
            and not snap.position_qty
            and not snap.active_orders
            and not snap.other_positions
            and not snap.other_assets
            and not engine.db.unresolved()
        ),
        "lifecycle_idle": not (
            engine.entry_tasks
            or engine.dispatching_entries
            or engine.probe_active
            or engine.protection_id is not None
            or engine.exit_id is not None
        ),
        "journal": engine.journal_ok and engine.state != "HALTED",
        "incident_flags": not any(
            engine.db.flag(flag) == "1"
            for flag in [
                "funding_change_pending",
                "account_incident_pending",
                "total_loss_stop",
                "probe_pending",
            ]
        ),
        "funding_known": 0 < Decimal(engine.db.gross_deposits()) <= config.max_funding_usd,
        "cash_audit": bool(
            snap
            and engine.db.net_cash_status(str(snap.equity), str(snap.position_qty))["status"]
            == "matched"
        ),
        "loss_limits": bool(
            snap
            and engine.baseline
            and not engine.baseline.check(config, snap.equity, engine.current_flow)
        ),
    }
    # Validate evidence independently; closed-session policy flags grant no authority.
    evidence_ok = True
    for (fill_id,) in engine.db.conn.execute("SELECT fill_id FROM fills WHERE fee IS NULL"):
        try:
            report = json.loads(engine.db.flag(f"fee_report:{fill_id}") or "{}")
            captured = datetime.fromisoformat(report["captured_at_utc"])
            evidence_ok &= captured.tzinfo is not None and engine.db.fee_report_matches(
                fill_id, report, config.account_index
            )
        except (ValueError, KeyError, TypeError):
            evidence_ok = False
    checks["historical_fee_evidence"] = evidence_ok
    minimum = {}
    local, remote = quotes["lighter"], quotes["binance"]
    for side in ["buy", "sell"]:
        if snap and market and local:
            best = local.ask if side == "buy" else local.bid
            try:
                fee = dec(market["taker_fee"])  # metadata estimate; NOT individual exact fee
                sizing = size_entry(
                    config,
                    min(snap.equity, snap.available),
                    Decimal(0),
                    Decimal(0),
                    best,
                    side,
                    dec(market["min_base_amount"]),
                    dec(market["min_quote_amount"]),
                    int(market["size_decimals"]),
                    int(market["price_decimals"]),
                    Decimal(35),
                    fee,
                    minimum_only=True,
                )
                depth = engine.books["lighter"].price_for(side, sizing.quantity)
                counter_side = "sell" if side == "buy" else "buy"
                comparator = engine.books["binance"].price_for(counter_side, sizing.quantity)
                fx_observation = engine.fx_monitor.latest
                levels = (
                    engine.books["lighter"].asks if side == "buy" else engine.books["lighter"].bids
                )
                within_limit = sum(
                    (
                        q
                        for p, q in levels.items()
                        if (p <= sizing.price if side == "buy" else p >= sizing.price)
                    ),
                    Decimal(0),
                )
                minimum[side] = {
                    "quantity": str(sizing.quantity),
                    "reference_price": str(best),
                    "limit_price": str(sizing.price),
                    "notional": str(sizing.notional),
                    "planned_risk": str(sizing.planned_risk),
                    "local_vwap": str(depth) if depth else None,
                    "binance_comparator_vwap": str(comparator) if comparator else None,
                    "binance_comparator_vwap_usdc": str(
                        fx_observation.normalized_btc(comparator, counter_side)
                    )
                    if comparator is not None and fx_observation and engine.fx_monitor.usable
                    else None,
                    "fx_conversion_side": "buy_USDC_at_ask"
                    if counter_side == "sell"
                    else "sell_USDC_at_bid",
                    "impact_bps": str(abs(depth / best - 1) * 10000) if depth else None,
                    "within_ioc_limit_qty": str(within_limit),
                    "fillable_within_limit": within_limit >= sizing.quantity,
                    "metadata_fee_bps": str(fee),
                    "individual_fee_exact": None,
                }
            except (ValueError, KeyError):
                minimum[side] = {"error": "minimum sizing or metadata unavailable"}
    checks["minimum_sizing_depth"] = len(minimum) == 2 and all(
        not v.get("error") and v["local_vwap"] is not None and v["fillable_within_limit"]
        for v in minimum.values()
    )
    observed_quotes = {}
    for name, q in quotes.items():
        observed_quotes[name] = (
            {
                "bid": str(q.bid),
                "ask": str(q.ask),
                "bid_size": str(q.bid_size),
                "ask_size": str(q.ask_size),
                "spread_bps": str((q.ask - q.bid) / q.mid * 10000),
                "receipt_ns": q.stamp.receipt_ns,
                "monotonic_ns": q.stamp.monotonic_ns,
                "exchange_ms": q.stamp.exchange_ms,
                "age_ms": (time.monotonic_ns() - q.stamp.monotonic_ns) / 1e6,
            }
            if q
            else None
        )
    match = all(
        q
        and engine.last_decision_inputs.get("quotes", {}).get(name)
        and engine.last_decision_inputs["quotes"][name]["monotonic_ns"] == q.stamp.monotonic_ns
        for name, q in quotes.items()
    )
    return {
        "technical_ready": all(checks.values()),
        "checks": checks,
        "blockers": [name for name, ok in checks.items() if not ok],
        "definition": (
            "signal-evaluation readiness including full preflight admission cost; "
            "not completed preflight; authorization/signal excluded; "
            "reported evidence validated, no fee-policy enable"
        ),
        "observed_at": wall,
        "source_freshness": source_freshness,
        "monotonic_ns": time.monotonic_ns(),
        "quotes": observed_quotes,
        "minimum_orders": minimum,
        "decision_input_match": match,
        "fx": str(engine.fx) if engine.fx is not None else None,
        "fx_age_s": now - engine.fx_at if engine.fx is not None else None,
        "fx_observation": engine.fx_monitor.status()["observation"],
        "request_entry_requirements": {
            "complete_audit_reads": engine.audit_scheduler.request_cost,
            "normal_cost": engine.audit_scheduler.request_cost + 2,
            "urgent_read_envelope": engine.audit_scheduler.request_cost + 14,
            "max_used_before_audit": min(
                40 - engine.audit_scheduler.request_cost - 2,
                47 - engine.audit_scheduler.request_cost - 14,
            ),
        },
        "entry_authorized": False,
        "local_metadata_fee_model_only": True,
        "receipt_skew_ms": abs(local.stamp.receipt_ns - remote.stamp.receipt_ns) / 1e6
        if local and remote
        else None,
    }
