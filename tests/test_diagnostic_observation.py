"""Passive readiness is independent of authorization and consumes no quota."""

import time
from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import pytest
from test_probe_only import engine as engine_fixture

from lldesk.diagnostics import observe
from lldesk.models import EngineState, Stamp
from lldesk.risk import EquityBaseline

engine = engine_fixture


def prepare(engine):
    engine.config = engine.config.model_copy(update={"allow_live_orders": False})
    engine.state = EngineState.IDLE
    engine.fx, engine.fx_at = Decimal(1), time.monotonic()
    engine.market["taker_fee"] = "0.0000"
    engine.strategy.coverage = lambda: {"ready": True}
    engine.books["binance"].binance_snapshot(
        {"lastUpdateId": 1, "bids": [["83000", "2"]], "asks": [["83000.1", "2"]]}, Stamp()
    )
    engine.baseline = EquityBaseline(
        Decimal("49.43"), Decimal("49.43"), Decimal("49.43"), Decimal("49.43"), "fixture"
    )
    engine.current_flow = Decimal("49.43")
    engine.db.event(
        "NET_CASH_AUDIT",
        {
            "equity_after": "49.43",
            "cash_change_usdc": "0",
            "fingerprint": engine.db.accounting_fingerprint(),
        },
    )


def test_source_guard_and_report_use_one_assessment_at_boundary(engine):
    prepare(engine)
    engine.source_freshness = Mock(
        side_effect=[
            {"passed": True, "reason": "SOURCE_FRESH_WITH_CLOCK_BOUND"},
            {"passed": False, "reason": "SOURCE_DATA_LATE"},
        ]
    )
    result = observe(engine)
    assert result["technical_ready"] and result["source_freshness"]["passed"]
    assert result["checks"]["source_freshness"] == result["source_freshness"]["passed"]
    engine.source_freshness.assert_called_once()


async def test_passive_technical_readiness_never_opens_entry(engine):
    prepare(engine)
    flags = list(engine.db.conn.execute("SELECT * FROM flags"))
    assert observe(engine)["technical_ready"]
    assert not observe(engine)["entry_authorized"]
    assert not engine.config.allow_live_orders and engine.state == EngineState.IDLE
    assert list(engine.db.conn.execute("SELECT * FROM flags")) == flags
    assert not engine.account.rate.calls
    await engine.enter("buy")
    assert not engine.account.sent and not engine.db.unresolved()


@pytest.mark.parametrize(
    "fault,blocker",
    [
        ("reserve", "entry_request_reserve"),
        ("account", "account_fresh"),
        ("reconcile", "reconciliation_fresh"),
        ("coverage", "coverage"),
        ("cash", "cash_audit"),
        ("depth", "minimum_sizing_depth"),
    ],
)
def test_diagnostic_reports_real_guards_without_altering_them(engine, fault, blocker):
    prepare(engine)
    if fault == "reserve":
        engine.account.rate.claim("reconcile", cost=34)
    if fault == "account":
        engine.account_snapshot = replace(engine.account_snapshot, received_at=time.time() - 16)
    if fault == "reconcile":
        engine.last_reconcile_ok = time.monotonic() - 16
        engine.history_required = True
    if fault == "coverage":
        engine.strategy.coverage = lambda: {"ready": False}
    if fault == "cash":
        engine.account_snapshot = replace(engine.account_snapshot, equity=Decimal("49.44"))
    if fault == "depth":
        engine.books["lighter"].asks[Decimal("83000.1")] = Decimal("0.00001")
    used = len(engine.account.rate.calls)
    result = observe(engine)
    assert blocker in result["blockers"] and not result["technical_ready"]
    assert len(engine.account.rate.calls) == used and not engine.config.allow_live_orders


def test_depth_measurement_keeps_exact_fee_unknown(engine):
    prepare(engine)
    result = observe(engine)
    for side in ["buy", "sell"]:
        order = result["minimum_orders"][side]
        assert Decimal(order["quantity"]) == Decimal("0.00013")
        assert order["individual_fee_exact"] is None
        assert order["metadata_fee_bps"] == "0.0000"
