"""Bounded session failure tests: isolated exchange, no mainnet or credentials."""

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from test_probe_only import engine as engine_fixture
from test_reported_fee_probe import authorize as historical_reports

from lldesk.account import LocalOrderRejected
from lldesk.engine import Engine

engine = engine_fixture


def authorize(engine, *, active=True):
    historical_reports(engine)
    engine.config = engine.config.model_copy(
        update={
            "live_entry_scope": "strategy",
            "probe_accept_reported_fees": False,
            "session_accept_reported_fees": True,
        }
    )
    for tid in ("1001", "1002"):
        r = json.loads(engine.db.flag(f"fee_report:{tid}"))
        r["captured_at_utc"] = datetime.now(UTC).isoformat()
        engine.db.record_fee_report(tid, r)
    engine.last_complete_snapshot = engine.account_snapshot
    engine.strategy.coverage = lambda *args: {"ready": True}
    engine.session.authorize("one-off-test", engine.config, str(engine.account_snapshot.equity))
    if active:
        assert engine.start_session() == "sesi live dimulai"


@pytest.mark.parametrize("bad", ["missing", "qty", "cash", "captured", "funding", "parameters"])
async def test_session_preflight_fails_closed(engine, bad):
    authorize(engine, active=False)
    if bad in {"missing", "qty", "captured"}:
        r = json.loads(engine.db.flag("fee_report:1001"))
        if bad == "qty":
            r["qty"] = "0.00013"
        if bad == "captured":
            r.pop("captured_at_utc")
        engine.db.set_flag("fee_report:1001", "{}" if bad == "missing" else json.dumps(r))
    elif bad == "cash":
        engine.account_snapshot = replace(engine.account_snapshot, equity=Decimal("49.430001"))
    elif bad == "funding":
        engine.db.record_flows((("unexpected-transfer", "0.01"),))
    else:
        engine.config = engine.config.model_copy(update={"max_hold_seconds": 170})
    assert "diblok" in engine.start_session()
    assert engine.session.read()["status"] == "authorized"
    assert engine.session.read()["attempts"] == []
    assert engine.account.sent == []


async def test_three_rejections_consume_before_signing_and_no_fourth(engine):
    authorize(engine)

    async def reject(intent, *args, dispatch_check=None):
        assert dispatch_check()
        current = engine.session.read()
        assert current["attempts"][-1]["client_id"] == intent.client_id
        assert (
            engine.db.conn.execute(
                "SELECT state FROM intents WHERE client_id=?", (intent.client_id,)
            ).fetchone()[0]
            == "SENT"
        )
        assert intent.quantity == Decimal("0.00013")
        raise LocalOrderRejected("definitive signing rejection")

    engine.account.send_ioc = reject
    for n in range(3):
        await engine.enter("buy")
        assert len(engine.session.read()["attempts"]) == n + 1
        engine.last_complete_snapshot = replace(engine.account_snapshot, received_at=time.time())
        engine.session_tick()
    assert engine.session.read()["status"] == "closed"
    await engine.enter("buy")
    assert len(engine.session.read()["attempts"]) == 3
    assert "diblok" in engine.start_session()
    engine.session_tick()
    assert engine.session.read()["status"] == "closed"
    assert not engine.config.allow_live_orders


async def test_restart_preserves_deadline_quota_and_closes_entry_authority(engine):
    authorize(engine)
    cid = engine.db.next_client_id()
    engine.db.intent(cid, "ENTRY", "buy", "0.00013", "83000")
    assert engine.session.consume(cid)
    prior = engine.session.read()
    restarted = Engine(engine.config, engine.root)
    try:
        current = restarted.session.read()
        assert current["status"] == "ending"
        assert current["deadline_epoch"] == prior["deadline_epoch"]
        assert current["attempts"] == prior["attempts"]
        assert restarted.entries_stopped and restarted.db.flag("close_requested") == "1"
        assert "diblok" in restarted.start_session()
        assert not restarted.session.consume(cid)
    finally:
        restarted.recorder.close()
        restarted.db.close()


async def test_waiting_signer_rechecks_frozen_cash_and_does_not_consume(engine):
    authorize(engine)
    engine.account.wait = True
    task = asyncio.create_task(engine.enter("buy"))
    await engine.account.entered.wait()
    engine.account_snapshot = replace(engine.account_snapshot, equity=Decimal("49.44"))
    engine.account.release.set()
    await task
    assert engine.account.sent == []
    assert engine.session.read()["attempts"] == []


async def test_concurrent_entry_tasks_cannot_create_two_intents(engine):
    authorize(engine)
    await asyncio.gather(engine.enter("buy"), engine.enter("buy"))
    assert len(engine.session.read()["attempts"]) == 1
    assert len(engine.db.unresolved()) == 1
    assert len(engine.account.sent) == 1


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_new_unknown_fee_partial_fill_never_blocks_stop_or_close(engine, ambiguous):
    authorize(engine)
    cid = engine.db.next_client_id()
    engine.db.intent(cid, "ENTRY", "buy", "0.00013", "83000")
    assert engine.session.consume(cid)
    engine.db.order_state(cid, "FILLED", "entry-tx")
    engine.db.add_fill("1003", cid, "0.00006", "83000", None, 3000)
    snap = replace(
        engine.account_snapshot, position_qty=Decimal("0.00006"), position_price=Decimal("83000")
    )
    engine.account_snapshot = engine.account.current = snap
    engine._start_position_deadline()
    engine.account.send_stop = AsyncMock(
        side_effect=TimeoutError() if ambiguous else None, return_value="stop-tx"
    )
    assert engine.entry_fee_blockers("ENTRY")
    await engine.manage_position()
    assert engine.account.send_stop.await_count == 1
    stop_intent = engine.account.send_stop.await_args.args[0]
    assert stop_intent.quantity == Decimal("0.00006") and stop_intent.side == "sell"
    if not ambiguous:
        order = {
            "client_order_index": engine.protection_id,
            "order_index": 777,
            "market_index": 1,
            "status": "pending",
            "type": "stop-loss",
            "is_ask": True,
            "reduce_only": True,
            "remaining_base_amount": "0.00006",
            "trigger_price": "82709.5",
        }
        assert engine.stop_covers_position(order, snap)
        engine.protection_confirmed = True
        engine.db.set_flag("close_requested", "1")
    else:
        assert engine.session.read()["status"] == "ending"
    await engine.manage_position()
    assert len(engine.account.sent) == 1
    assert engine.account.sent[0].kind == "EXIT"
    assert engine.account.sent[0].quantity == Decimal("0.00006")
    assert (
        engine.db.conn.execute("SELECT fee FROM fills WHERE fill_id='1003'").fetchone()[0] is None
    )
    await engine.enter("buy")
    assert len(engine.session.read()["attempts"]) == 1


async def test_deadline_monotonic_survives_wall_clock_backwards_and_fee_pending(
    engine, monkeypatch
):
    authorize(engine)
    deadline = engine.session_end
    prior = engine.session.read()["deadline_epoch"]
    monkeypatch.setattr("lldesk.engine.time.monotonic", lambda: deadline + 0.001)
    monkeypatch.setattr("lldesk.engine.time.time", lambda: prior - 1000)
    engine.session_tick(risk_only=True)
    assert engine.session.read()["status"] == "ending"
    assert engine.db.flag("close_requested") == "1"
    assert engine.entries_stopped


async def test_deadline_does_not_declare_cleanup_when_order_or_late_fill_remains(engine):
    authorize(engine)
    cid = engine.db.next_client_id()
    engine.db.intent(cid, "ENTRY", "buy", "0.00013", "83000")
    assert engine.session.consume(cid)
    engine.db.order_state(cid, "UNKNOWN")
    engine.end_session("timeout")
    engine.session_tick()
    assert engine.session.read()["status"] == "ending"
    engine.db.order_state(cid, "FILLED", "entry-tx")
    engine.db.add_fill("1003", cid, "0.00013", "83000", None, 3000)
    engine.account_snapshot = replace(
        engine.account_snapshot, position_qty=Decimal("0.00013"), position_price=Decimal("83000")
    )
    await engine.manage_position()
    assert engine.account.sent[-1].kind == "EXIT"
    assert engine.session.read()["status"] == "ending"


async def test_cash_audit_after_new_fills_uses_reported_fee_and_preserves_null(engine, monkeypatch):
    authorize(engine)
    entry = engine.db.next_client_id()
    engine.db.intent(entry, "ENTRY", "buy", "0.00013", "83000")
    assert engine.session.consume(entry)
    engine.db.order_state(entry, "FILLED", "entry-tx")
    engine.db.add_fill("1003", entry, "0.00013", "83000", None, 3000)
    exit_ = engine.db.next_client_id()
    engine.db.intent(exit_, "EXIT", "sell", "0.00013", "83001")
    engine.db.order_state(exit_, "FILLED", "exit-tx")
    engine.db.add_fill("1004", exit_, "0.00013", "83001", None, 4000)
    reports = []
    for tid, cid, side, price, tx, stamp in [
        ("1003", entry, "Open Long", "83000", "entry-tx", 3000),
        ("1004", exit_, "Close Long", "83001", "exit-tx", 4000),
    ]:
        r = json.loads(engine.db.flag("fee_report:1001"))
        r.update(
            trade_id=tid,
            client_id=cid,
            qty="0.00013",
            side=side,
            price=price,
            transaction_hash=tx,
            exchange_ms=stamp,
        )
        if tid == "1004":
            r["reported_fee_usdc"] = "0.0001"
        reports.append(r)
    engine.account_snapshot = engine.account.current = replace(
        engine.account_snapshot,
        equity=Decimal("49.43003"),
        available=Decimal("49.43003"),
        received_at=time.time(),
    )
    engine.last_complete_snapshot = engine.account_snapshot
    assert engine.entry_fee_blockers("ENTRY")

    async def export(*args):
        for report in reports:
            engine.db.record_fee_report(report["trade_id"], report)
        return reports

    monkeypatch.setattr("lldesk.engine.collect_export", export)
    await engine.audit_session_cash()
    assert not engine.entry_fee_blockers("ENTRY")
    assert engine.db.pending_fees() == 4
    proof = next(e for e in engine.db.recent_events() if e["kind"] == "STRATEGY_CASH_AUDIT_RESULT")[
        "body"
    ]
    assert proof["gross_fill_pnl_usdc"] == "0.00013"
    assert proof["reported_or_exact_fee_sum_usdc"] == "0.0001"
    assert Decimal(proof["net_residual_usdc"]) == 0


async def test_audit_pending_never_reopens_entry_after_session_expiry(engine):
    authorize(engine)
    engine.end_session("deadline")
    assert not engine.session_entry_ready()
    assert "diblok" in engine.start_session()
    assert engine.session.read()["attempts"] == []


def test_without_explicit_session_authorization_start_live_is_blocked(engine):
    assert "otorisasi" in engine.start_session()
    assert engine.account.sent == []


async def test_restart_unconfirmed_stop_exits_actual_size_without_bookkeeping_lock(engine):
    authorize(engine)
    cid = engine.db.next_client_id()
    engine.db.intent(cid, "ENTRY", "buy", "0.00013", "83000")
    assert engine.session.consume(cid)
    engine.db.order_state(cid, "FILLED", "entry-tx")
    engine.db.add_fill("1003", cid, "0.00006", "83000", None, 3000)
    stop = engine.db.next_client_id()
    engine.db.intent(stop, "STOP", "sell", "0.00006", "82709.5")
    engine.db.order_state(stop, "UNKNOWN")
    engine._start_position_deadline()
    restarted = Engine(engine.config, engine.root)
    try:
        restarted.books = engine.books
        restarted.market = engine.market
        restarted.account = engine.account
        restarted.account_snapshot = replace(
            engine.account_snapshot,
            position_qty=Decimal("0.00006"),
            position_price=Decimal("83000"),
        )
        assert restarted.protection_id == stop and not restarted.protection_confirmed
        assert restarted.session.read()["status"] == "ending"
        await restarted.reconcile_lock.acquire()
        await asyncio.wait_for(restarted.manage_position(), timeout=0.2)
        assert engine.account.sent[-1].kind == "EXIT"
        assert engine.account.sent[-1].quantity == Decimal("0.00006")
        assert len(restarted.session.read()["attempts"]) == 1
        assert restarted.db.pending_fees() == 3
    finally:
        restarted.reconcile_lock.release()
        restarted.recorder.close()
        restarted.db.close()


async def test_third_flat_position_ends_entry_even_with_fee_and_cash_pending(engine):
    authorize(engine)
    for n in range(3):
        cid = engine.db.next_client_id()
        engine.db.intent(cid, "ENTRY", "buy", "0.00013", "83000")
        assert engine.session.consume(cid)
        engine.db.order_state(cid, "REJECTED" if n < 2 else "FILLED", "entry-tx")
    engine.db.add_fill("1003", cid, "0.00013", "83000", None, 3000)
    exit_ = engine.db.next_client_id()
    engine.db.intent(exit_, "EXIT", "sell", "0.00013", "83001")
    engine.db.order_state(exit_, "FILLED", "exit-tx")
    engine.db.add_fill("1004", exit_, "0.00013", "83001", None, 4000)
    engine.account_snapshot = replace(
        engine.account_snapshot, equity=Decimal("49.43013"), received_at=time.time()
    )
    engine.last_complete_snapshot = engine.account_snapshot
    engine.account.rate.claim("reconcile", cost=40)
    engine.session_tick()
    assert engine.session.read()["status"] == "ending"
    assert "kuota tiga" in engine.session.read()["stop_reason"]
    assert not engine.session_entry_ready() and engine.entries_stopped
    assert engine.db.pending_fees() == 4
    assert engine.fee_audit_task is None


async def test_closed_disarmed_session_idle_keeps_feed_readiness_separate(engine):
    authorize(engine)
    engine.end_session("account recovered after timeout")
    engine.finish_session(engine.db.net_cash_status("49.43", "0"))
    assert engine.session.read()["status"] == "closed"
    engine.data_valid = lambda: False
    engine.last_complete_snapshot = engine.account_snapshot
    engine.decide()
    assert engine.state.value == "IDLE"
    assert not engine.config.allow_live_orders and engine.entries_stopped
    # Fixture deliberately returns a signal even with invalid feed data.
    assert engine.last_decision.side == "buy"
    assert not engine.entry_tasks and not engine.db.unresolved()
    assert not engine.session_entry_ready()
    assert engine.live_blockers()
    # A stale/global-order snapshot still prevents a false flat/IDLE claim.
    engine.last_complete_snapshot = replace(
        engine.account_snapshot, active_orders=({"order_index": "unexpected"},)
    )
    engine.decide()
    assert engine.state.value == "PAUSED"
