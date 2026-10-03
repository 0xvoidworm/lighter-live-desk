"""Replay missing-fee mainnet shape with isolated exchange and controlled clocks."""

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_probe_only import engine as engine_fixture

from lldesk.account import LighterAccount
from lldesk.config import Config
from lldesk.db import Database
from lldesk.engine import Engine
from lldesk.models import OrderIntent, OrderState, Stamp

engine = engine_fixture  # Register the shared isolated exchange fixture here.


def exposed(engine, qty="0.00012", *, fee=None):
    engine.probe_active = True
    engine.db.set_flag("probe_pending", "1")
    engine.db.set_flag("probe_used", "1")
    engine.db.intent(1, "PROBE", "buy", "0.00012", "83010")
    engine.db.add_fill("33141524453", 1, qty, "83000", fee, 1791020396773)
    engine.account.current = replace(
        engine.account_snapshot, position_qty=Decimal(qty), position_price=Decimal(83000)
    )
    engine.account.statuses = {1: {"status": "filled", "filled_base_amount": qty}}
    engine.account.send_stop = AsyncMock(return_value="stop ACK")
    engine.account.cancel = AsyncMock(return_value="cancel ACK")


async def stop_worker(engine):
    for task in (engine.risk_task, engine.risk_poll_task, engine.risk_action_task):
        if task:
            task.cancel()
    await asyncio.gather(
        *(t for t in (engine.risk_task, engine.risk_poll_task, engine.risk_action_task) if t),
        return_exceptions=True,
    )


@pytest.mark.parametrize("qty", ["0.00012", "0.00006"])
async def test_priority_stop_ignores_hung_bookkeeping_and_reconcile_lock(engine, qty):
    exposed(engine, qty)
    stop_seen = asyncio.Event()

    async def stop(*args):
        stop_seen.set()
        return "stop ACK"

    engine.account.send_stop = AsyncMock(side_effect=stop)
    await engine.reconcile_lock.acquire()
    engine.reconcile_task = asyncio.create_task(asyncio.Event().wait())
    engine.risk_task = asyncio.create_task(engine.risk_loop())
    try:
        await asyncio.wait_for(stop_seen.wait(), 1)
        assert engine.account.send_stop.call_args.args[0].quantity == Decimal(qty)
        assert engine.account.send_stop.call_args.args[0].side == "sell"
        assert engine.db.pending_fees() == 1
        assert engine.db.flag("probe_used") == "1"
        assert not engine.account.sent  # No second entry or premature normal close.
        assert engine.last_reconcile_ok > 0
    finally:
        engine.reconcile_task.cancel()
        await asyncio.gather(engine.reconcile_task, return_exceptions=True)
        engine.reconcile_lock.release()
        await stop_worker(engine)


async def test_unknown_stop_closes_once_for_actual_partial_size_while_fee_pending(engine):
    exposed(engine, "0.00006")
    engine.account.send_stop = AsyncMock(side_effect=TimeoutError)
    await engine.recover_exposure()
    await engine.manage_position()  # Stop fails/ambiguous; no stop retry.
    await engine.manage_position()
    await engine.manage_position()
    assert engine.account.send_stop.await_count == 1
    assert len(engine.account.sent) == 1 and engine.account.sent[0].kind == "EXIT"
    assert engine.account.sent[0].quantity == Decimal("0.00006")
    assert engine.db.pending_fees() == 1
    assert not engine.protection_confirmed
    assert engine.db.flag("probe_pending") == "1"


async def test_unconfirmed_stop_deadline_preempts_hung_dispatch(engine, monkeypatch):

    exposed(engine)
    engine.account_snapshot = engine.account.current
    engine._start_position_deadline()
    engine.unprotected_since = time.monotonic() - 9
    engine.protection_id = 2
    engine.db.intent(2, "STOP", "sell", "0.00012", "82000")
    engine.db.order_state(2, OrderState.UNKNOWN)
    engine.risk_action_task = asyncio.create_task(asyncio.Event().wait())
    engine.risk_task = asyncio.create_task(engine.risk_loop())
    try:
        async with asyncio.timeout(1):
            while not engine.account.sent:
                await asyncio.sleep(0.01)
        assert engine.account.sent[0].kind == "EXIT"
        assert engine.account.send_stop.await_count == 0
        assert engine.db.pending_fees() == 1
    finally:
        await stop_worker(engine)


async def test_holding_deadline_survives_live_wall_clock_jump(engine, monkeypatch):
    exposed(engine)
    await engine.recover_exposure()
    engine.protection_confirmed = True
    engine.probe_active = False
    engine.db.set_flag("probe_pending", "0")
    engine.position_opened_at = time.monotonic() - 178
    original = time.time()
    # Wall clock moves backward; the already armed monotonic deadline still fires.
    monkeypatch.setattr("lldesk.engine.time.time", lambda: original - 3600)
    await engine.manage_position()
    assert len(engine.account.sent) == 1 and engine.account.sent[0].kind == "EXIT"
    assert engine.db.flag("close_requested") == "1"


async def test_restart_unknown_stop_does_not_restart_180_seconds_or_duplicate_stop(engine):
    exposed(engine, "0.00006")
    engine.db.intent(2, "STOP", "sell", "0.00006", "82000")
    engine.db.order_state(2, OrderState.UNKNOWN)
    engine.db.set_flag("position_opened_epoch", str(time.time() + 3600))
    resumed = Engine(engine.config, engine.root)
    try:
        resumed.account = engine.account
        resumed.market = engine.market
        resumed.books = engine.books
        await resumed.recover_exposure()
        await resumed.manage_position()
        assert not resumed.protection_confirmed
        assert resumed.account.send_stop.await_count == 0
        assert len(resumed.account.sent) == 1
        assert resumed.account.sent[0].kind == "EXIT"
        assert resumed.account.sent[0].quantity == Decimal("0.00006")
        assert time.monotonic() - resumed.position_opened_at >= 180
        assert resumed.db.flag("probe_used") == "1"
    finally:
        resumed.recorder.close()
        resumed.db.close()


async def test_pending_fee_can_be_backfilled_without_duplicate_fill_or_entry(engine):
    exposed(engine)
    trade = {
        "trade_id": 33141524453,
        "ask_account_id": 88,
        "bid_account_id": 77,
        "bid_client_id": 1,
        "is_maker_ask": True,
        "taker_fee": None,
        "price": "83000",
        "size": "0.00012",
    }
    engine._record_trade(trade, 77)
    assert engine.status()["account"]["recorded_fees"] is None
    assert engine.db.pending_fees() == 1
    engine._record_trade({**trade, "taker_fee": 123}, 77)
    assert engine.db.pending_fees() == 0
    assert engine.db.conn.execute("SELECT fee FROM fills").fetchone()[0] == "0.000123"
    assert engine.db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert not engine.account.sent


async def test_unknown_fees_block_probe_before_consuming_one_entry_grant(engine):
    engine.db.intent(1, "PROBE", "buy", "0.00012", "83010")
    engine.db.order_state(1, OrderState.FILLED)
    engine.db.add_fill("123", 1, "0.00012", "83000", None, None)
    assert "fee trade belum diketahui" in await engine.probe_once()
    assert engine.db.flag("probe_used") is None
    assert not engine.entry_tasks and not engine.account.sent


async def test_ambiguous_cancel_does_not_retry_on_same_pending_order(engine):
    exposed(engine)
    engine.db.intent(2, "STOP", "sell", "0.00012", "82000")
    engine.protection_id = 2
    engine.account.cancel = AsyncMock(side_effect=TimeoutError)
    stop = {"order_index": 999, "status": "pending"}
    await engine._cancel_flat_stop(stop)
    await engine._cancel_flat_stop(stop)
    assert engine.account.cancel.await_count == 1
    assert engine.db.unresolved()[-1]["state"] == OrderState.UNKNOWN


async def test_stale_flat_read_cannot_cancel_stop_or_clear_ambiguous_close(engine):
    exposed(engine)
    engine.db.intent(2, "STOP", "sell", "0.00012", "82000")
    engine.db.intent(3, "EXIT", "sell", "0.00012", "82000")
    engine.protection_id, engine.exit_id = 2, 3
    engine.account.current = replace(engine.account.current, position_qty=Decimal(0))
    engine.account.statuses.update(
        {
            2: {"status": "pending", "filled_base_amount": "0", "order_index": 999},
            3: {"status": "canceled", "filled_base_amount": "0"},
        }
    )
    await engine.recover_exposure()
    assert engine.protection_id == 2 and engine.exit_id == 3
    engine.account.cancel.assert_not_awaited()
    assert engine.db.flag("probe_pending") == "1"
    assert not engine.account.sent


async def test_fresh_position_can_protect_while_order_history_times_out(engine):
    exposed(engine)

    async def slow_statuses(*args, **kwargs):
        await asyncio.sleep(10)

    engine.account.order_statuses = AsyncMock(side_effect=slow_statuses)
    recovery = asyncio.create_task(engine.recover_exposure())
    try:
        await asyncio.sleep(0.01)
        await engine.manage_position()
        engine.account.send_stop.assert_awaited_once()
        assert not recovery.done()
        assert not engine.account.sent and engine.db.pending_fees() == 1
    finally:
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)


async def test_stale_uncertain_position_cannot_guess_exit_size(engine):
    exposed(engine)
    engine.account_snapshot = replace(engine.account.current, received_at=time.time() - 10)
    engine.db.set_flag("close_requested", "1")
    engine.account.position_snapshot = AsyncMock(side_effect=TimeoutError)
    await engine.close_position()
    await engine.recover_exposure()
    assert not engine.account.sent
    assert engine.db.flag("close_requested") == "1"
    assert engine.db.flag("probe_used") == "1"


async def test_position_read_uses_one_reserved_request_without_audit_history(engine):
    adapter = LighterAccount(Config(account_index=77))
    position = SimpleNamespace(
        market_id=1,
        position="0.00006",
        sign=1,
        avg_entry_price="83000",
        unrealized_pnl="0",
        realized_pnl="0",
        total_funding_paid_out="0",
        margin_mode=0,
        initial_margin_fraction="100",
    )
    account = SimpleNamespace(
        index=77,
        positions=[position],
        assets=[],
        total_asset_value="49.43",
        available_balance="49.43",
        account_type=0,
        account_trading_mode=1,
    )
    adapter.account_api = SimpleNamespace(
        account=AsyncMock(return_value=SimpleNamespace(code=200, accounts=[account]))
    )
    adapter.order_api = SimpleNamespace(account_active_orders=AsyncMock(side_effect=TimeoutError))
    adapter.funding_history = AsyncMock(side_effect=TimeoutError)
    for _ in range(40):
        adapter.rate.claim("reconcile")
    snap = await adapter.position_snapshot(1, engine.account_snapshot)
    assert snap.position_qty == Decimal("0.00006")
    assert len(adapter.rate.calls) == 41
    adapter.order_api.account_active_orders.assert_not_awaited()
    adapter.funding_history.assert_not_awaited()
    assert not adapter.rate.can_claim("reconcile") and adapter.rate.can_claim("exit", cost=2)


async def test_routine_protected_poll_preserves_urgent_read_reserve_for_exit_deadline(engine):
    exposed(engine)
    engine.probe_active = False
    engine.db.set_flag("probe_pending", "0")
    engine.db.order_state(1, OrderState.FILLED)
    engine.account_snapshot = engine.account.current
    engine.db.intent(2, "STOP", "sell", "0.00012", "82000")
    engine.protection_id, engine.protection_confirmed = 2, True
    engine.position_opened_at = time.monotonic()
    engine.unprotected_since = time.monotonic()
    for _ in range(40):
        engine.account.rate.claim("reconcile")
    priorities = []

    async def position_read(*args, priority, **kwargs):
        priorities.append(priority)
        engine.account.rate.claim(priority)
        return replace(engine.account.current, received_at=time.time())

    engine.account.position_snapshot = AsyncMock(side_effect=position_read)
    engine.risk_task = asyncio.create_task(engine.risk_loop())
    try:
        await asyncio.sleep(0.1)
        assert not priorities  # Routine polling cannot spend the urgent reserve.
        engine.position_opened_at = time.monotonic() - 178
        async with asyncio.timeout(1):
            while not priorities or not engine.account.sent:
                await asyncio.sleep(0.01)
        assert priorities == ["probe_read"]
        assert engine.account.sent[0].kind == "EXIT"
        assert engine.db.pending_fees() == 1
        assert len(engine.account.rate.calls) < 47
    finally:
        await stop_worker(engine)


def test_legacy_inferred_zero_fees_migrate_to_unknown_and_preserve_cash_event(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    db.conn.execute("DELETE FROM migrations WHERE version=3")
    db.intent(1, "PROBE", "buy", "0.00012", "83000")
    db.add_fill("123", 1, "0.00012", "83000", "0", 1000)
    db.event(
        "AUDITED_FILL",
        {"trade_id": "123", "fee_source": "aggregate cash audit; raw API fee absent"},
    )
    db.event("FEE_CASH_AUDIT", {"cash_delta": "0.000576"})
    db.close()
    resumed = Database(path)
    try:
        assert resumed.pending_fees() == 1
        assert resumed.conn.execute("SELECT fee FROM fills").fetchone()[0] is None
        assert any(e["kind"] == "FEE_CASH_AUDIT" for e in resumed.recent_events())
    finally:
        resumed.close()


async def test_stop_and_close_lock_waits_have_total_bound_without_signer_dispatch():
    adapter = LighterAccount(Config(account_index=77))
    adapter.signer = AsyncMock()
    await adapter.lock.acquire()
    intent = OrderIntent(1, "EXIT", "sell", Decimal("0.00012"), Decimal(83000), Stamp())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            await adapter.send_ioc(intent, 1, 5, 1)
        assert time.monotonic() - started < 3.5
        adapter.signer.create_order.assert_not_awaited()
    finally:
        adapter.lock.release()
