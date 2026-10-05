"""Probe controls use isolated exchanges; no secrets or mainnet requests."""

import asyncio
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lldesk.account import AccountSnapshot, DispatchBlocked, LocalOrderRejected, RateBudget
from lldesk.config import Config
from lldesk.engine import JAKARTA, Engine
from lldesk.models import EngineState, OrderState, Stamp
from lldesk.strategy import Decision, DeviationStrategy


class Exchange:
    account_index = 77

    def __init__(self, snapshot):
        self.current = snapshot
        self.sent = []
        self.wait = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.statuses = {}
        self.rate = RateBudget()
        self.probe_scopes = []

    async def send_ioc(self, intent, *args, dispatch_check=None):
        self.entered.set()
        if self.wait:
            await self.release.wait()
        if intent.kind in {"ENTRY", "PROBE"} and (dispatch_check is None or not dispatch_check()):
            raise DispatchBlocked("mock signer blocked")
        self.rate.claim("entry" if intent.kind in {"ENTRY", "PROBE"} else "exit", cost=2)
        self.sent.append(intent)
        return "mock-ack"

    async def snapshot(self, market_id):
        return replace(
            self.current,
            received_at=time.time(),
            received_monotonic=time.monotonic(),
            orders_received_at=time.time(),
            orders_received_monotonic=time.monotonic(),
        )

    async def state_snapshot(self, market_id, previous=None):
        return await self.snapshot(market_id)

    async def probe_snapshot(self, market_id, verified_flows=None, **kwargs):
        self.probe_scopes.append(verified_flows)
        return await self.snapshot(market_id)

    async def position_snapshot(self, market_id, previous=None, **kwargs):
        return await self.snapshot(market_id)

    async def order_statuses(self, ids, **kwargs):
        return {key: value for key, value in self.statuses.items() if key in ids}

    async def recent_trades(self, market_id, after_id, **kwargs):
        return []


@pytest.fixture
def engine(tmp_path):
    config = Config(
        mode="live_capable",
        account_index=77,
        expected_account_type_code=0,
        expected_account_trading_mode=1,
        expected_margin_mode=0,
        expected_initial_margin_fraction=Decimal("100.00"),
        allow_live_orders=True,
        live_entry_scope="strategy",
    )
    value = Engine(config, tmp_path)
    value.market = {
        "market_id": 1,
        "min_base_amount": "0.00007",
        "min_quote_amount": "10",
        "size_decimals": 5,
        "price_decimals": 1,
        "taker_fee": "0.0000",
    }
    value.books["lighter"].lighter_snapshot(
        {
            "order_book": {
                "nonce": 1,
                "bids": [{"price": "83000", "size": "2"}],
                "asks": [{"price": "83000.1", "size": "2"}],
            }
        },
        Stamp(),
    )
    value.account_snapshot = AccountSnapshot(
        equity=Decimal("49.43"),
        available=Decimal("49.43"),
        account_type_code=0,
        position_qty=Decimal(0),
        position_price=Decimal(0),
        unrealized_pnl=Decimal(0),
        realized_pnl=Decimal(0),
        funding=Decimal(0),
        active_orders=(),
        other_positions=(),
        other_assets=(),
        received_at=time.time(),
        received_monotonic=time.monotonic(),
        orders_received_at=time.time(),
        orders_received_monotonic=time.monotonic(),
        exchange_order_count=0,
        exchange_pending_count=0,
        collateral=Decimal("49.43"),
        account_trading_mode=1,
        margin_mode=0,
        initial_margin_fraction=Decimal("100.00"),
        funding_flows=(("fixture-deposit", "49.43"),),
    )
    value.db.record_flows(value.account_snapshot.funding_flows)
    value.account = Exchange(value.account_snapshot)
    value.last_reconcile_ok = time.monotonic()
    value.reconcile_failed = False
    value.history_required = False
    value.state_refresh_failed = False
    value.state = EngineState.READY
    value.data_valid = lambda: True
    # Other lifecycle tests isolate market admission; source guard has dedicated replay tests.
    value.source_freshness = lambda: {"passed": True, "reason": "isolated_fixture"}
    value.last_decision = Decision("buy", Decimal(30), Decimal(0), Decimal(1), ())
    value.strategy = DeviationStrategy(config.warmup_seconds)
    value.strategy.evaluate = lambda *args: value.last_decision
    value.strategy.coverage = lambda: {"ready": True}
    value.fx, value.fx_at = Decimal(1), time.monotonic()
    value.books["binance"].binance_snapshot(
        {"lastUpdateId": 1, "bids": [["83000", "2"]], "asks": [["83000.1", "2"]]}, Stamp()
    )
    value._update_baseline(value.account_snapshot)
    value.db.event(
        "NET_CASH_AUDIT",
        {
            "equity_after": "49.43",
            "cash_change_usdc": "0",
            "fingerprint": value.db.accounting_fingerprint(),
        },
    )

    async def no_reconcile(*, reserved=False):
        if reserved:
            await Engine.safe_reconcile(value, reserved=True)

    value.safe_reconcile = no_reconcile
    yield value
    assert not value.entry_tasks
    value.recorder.close()
    value.db.close()


async def drain(engine):
    await asyncio.gather(*engine.entry_tasks)
    await asyncio.sleep(0)


async def test_missing_mainnet_fee_preserves_fill_for_protection_and_blocks_next_entry(engine):
    from unittest.mock import AsyncMock

    engine.probe_active = True
    engine.db.set_flag("probe_pending", "1")
    engine.db.intent(1, "PROBE", "buy", "0.00012", "83001")
    engine.account.current = replace(
        engine.account_snapshot,
        position_qty=Decimal("0.00012"),
        position_price=Decimal("83000"),
    )
    trade = {
        "trade_id": 33141524453,
        "ask_account_id": 88,
        "bid_account_id": 77,
        "ask_client_id": 999,
        "bid_client_id": 1,
        "is_maker_ask": True,
        "maker_fee": None,
        "taker_fee": None,
        "size": "0.00012",
        "price": "83000",
    }

    async def trades(*args, **kwargs):
        return [trade]

    engine.account.recent_trades = trades
    engine.account.send_stop = AsyncMock(return_value="mock stop ack")
    for _ in range(2):
        await engine.reconcile()
    await engine.manage_position()
    engine.account.send_stop.assert_awaited_once()
    assert engine.account.send_stop.call_args.args[0].quantity == Decimal("0.00012")
    engine.protection_confirmed = True
    await engine.manage_position()
    assert len(engine.account.sent) == 1
    assert engine.account.sent[0].kind == "EXIT"
    assert engine.account.sent[0].quantity == Decimal("0.00012")
    assert engine.account_snapshot.position_qty == Decimal("0.00012")
    assert engine.entries_stopped and not engine.manual_exposure
    assert engine.db.flag("close_requested") == "1"
    assert engine.db.pending_fees() == 1
    assert engine.db.flag("probe_pending") == "1"
    assert engine.db.conn.execute("SELECT fee FROM fills").fetchone()[0] is None
    assert any("fee trade belum" in b for b in engine.live_blockers())


def test_missing_fee_repeat_requires_matching_previously_audited_fill(engine):
    engine.db.intent(1, "PROBE", "buy", "0.00012", "83001")
    engine.db.add_fill("123", 1, "0.00012", "83000", "0", None)
    trade = {
        "trade_id": 123,
        "ask_account_id": 88,
        "bid_account_id": 77,
        "ask_client_id": 999,
        "bid_client_id": 1,
        "is_maker_ask": True,
        "taker_fee": None,
        "size": "0.00012",
        "price": "83000",
    }
    engine._record_trade(trade, 77)
    assert engine.db.flag("account_incident_pending") is None
    assert engine.db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    trade["price"] = "83001"
    with pytest.raises(ValueError, match="economics changed"):
        engine._record_trade(trade, 77)


async def test_probe_does_not_open_strategy_even_with_immediate_signal(engine):
    for command in ("PROBE_ONCE", "PROBE_ONCE", "START_LIVE"):
        engine.db.command(command)
    await engine.commands()
    assert engine.state == EngineState.PROBING
    assert engine.entries_stopped
    assert len(engine.entry_tasks) == 1
    assert engine.db.flag("probe_used") == "1"
    assert engine.db.flag("live_ever") == "1"
    assert 0 < engine.session_end - time.monotonic() <= engine.config.max_hold_seconds
    engine.decide()
    await engine.enter("sell")
    await drain(engine)
    assert len(engine.account.sent) == 1
    assert engine.account.sent[0].kind == "PROBE"
    assert engine.account.sent[0].quantity == Decimal("0.00013")
    assert engine.entries_stopped


def fault(engine, name):
    if name in {"LIVE", "IDLE", "WARMING_UP", "HALTED"}:
        engine.state = EngineState(name)
    elif name == "orders_disabled":
        engine.config.allow_live_orders = False
    elif name == "data":
        engine.data_valid = lambda: False
    elif name == "coverage":
        engine.last_decision = Decision(None, None, None, None, ())
    elif name == "journal":
        engine.journal_ok = False
    elif name == "stale_reconcile":
        engine.last_reconcile_ok = time.monotonic() - 16
        engine.history_required = True  # New policy: required audit, not mere age >15s.
    elif name == "failed_reconcile":
        engine.reconcile_failed = True
    elif name == "account":
        engine.account = None
    elif name == "snapshot":
        engine.account_snapshot = None
    elif name == "market":
        engine.market = None
    elif name == "manual":
        engine.manual_exposure = True
    elif name == "rate":
        engine.account.rate.claim("exit", cost=40)  # Urgent work can invalidate send admission.
    elif name == "reconciling":
        engine.reconcile_task = SimpleNamespace(done=lambda: False)
    elif name == "over_budget":
        engine.db.record_flows((("extra", "0.58"),))
    elif name == "intent":
        engine.db.intent(99, "ENTRY", "buy", "0.00013", "83000")
    elif name == "session":
        engine.session_end = time.monotonic() + 30
    elif name == "protection":
        engine.protection_id = 99
    elif name == "exit":
        engine.exit_id = 99
    elif name in {
        "probe_used",
        "funding_change_pending",
        "account_incident_pending",
        "total_loss_stop",
        "close_requested",
    }:
        engine.db.set_flag(name, "1")
    elif name == "daily_loss":
        engine.db.set_flag("daily_stop_day", datetime.now(JAKARTA).date().isoformat())
    else:
        changes = {
            "stale_snapshot": {"received_at": time.time() - 16},
            "tier": {"account_type_code": 1},
            "trading_mode": {"account_trading_mode": 0},
            "margin": {"margin_mode": 1},
            "leverage": {"initial_margin_fraction": Decimal("5")},
            "position": {"position_qty": Decimal("0.00013")},
            "orders": {"active_orders": ({"client_order_index": 99},)},
            "other_position": {"other_positions": ({"market_id": 2},)},
            "other_asset": {"other_assets": ({"symbol": "LIT"},)},
            "funds": {"equity": Decimal(2), "available": Decimal(2)},
        }
        engine.account_snapshot = replace(engine.account_snapshot, **changes[name])


@pytest.mark.parametrize(
    "name",
    [
        "LIVE",
        "IDLE",
        "WARMING_UP",
        "HALTED",
        "orders_disabled",
        "data",
        "coverage",
        "journal",
        "stale_reconcile",
        "failed_reconcile",
        "account",
        "snapshot",
        "market",
        "manual",
        "rate",
        "reconciling",
        "over_budget",
        "intent",
        "session",
        "protection",
        "exit",
        "probe_used",
        "funding_change_pending",
        "account_incident_pending",
        "total_loss_stop",
        "close_requested",
        "daily_loss",
        "stale_snapshot",
        "tier",
        "trading_mode",
        "margin",
        "leverage",
        "position",
        "orders",
        "other_position",
        "other_asset",
        "funds",
    ],
)
async def test_failed_preflight_never_consumes_probe_or_schedules_order(engine, name):
    exchange = engine.account
    fault(engine, name)
    assert "probe diblok" in await engine.probe_once()
    assert not engine.entry_tasks
    assert not exchange.sent
    assert engine.db.flag("probe_pending") != "1"
    if name != "probe_used":
        assert engine.db.flag("probe_used") != "1"


@pytest.mark.parametrize("command", ["STOP_ENTRIES", "CANCEL_ENTRIES", "CLOSE_POSITION"])
async def test_stop_reaches_probe_before_scheduled_task_runs(engine, command):
    engine.db.command("PROBE_ONCE")
    engine.db.command(command)
    await engine.commands()
    await drain(engine)
    assert not engine.account.sent
    assert not engine.db.unresolved()
    assert engine.entries_stopped
    assert engine.db.flag("probe_used") == "1"


@pytest.mark.parametrize(
    "name",
    [
        "orders_disabled",
        "data",
        "coverage",
        "journal",
        "stale_reconcile",
        "failed_reconcile",
        "account",
        "snapshot",
        "market",
        "manual",
        "rate",
        "close_requested",
        "funding_change_pending",
        "account_incident_pending",
        "total_loss_stop",
        "daily_loss",
        "stale_snapshot",
        "tier",
        "trading_mode",
        "margin",
        "leverage",
        "position",
        "orders",
        "other_position",
        "other_asset",
        "funds",
        "stop",
        "expired",
    ],
)
async def test_probe_rechecks_changes_while_waiting_for_signer(engine, name):
    exchange = engine.account
    exchange.wait = True
    await engine.probe_once()
    await asyncio.wait_for(exchange.entered.wait(), 1)
    if name == "stop":
        engine.db.command("STOP_ENTRIES")
        await engine.commands()
    elif name == "expired":
        engine.session_end = time.monotonic() - 1
    else:
        fault(engine, name)
    exchange.release.set()
    await drain(engine)
    assert not exchange.sent
    assert not engine.db.unresolved()
    assert engine.db.flag("probe_used") == "1"
    assert engine.entries_stopped


async def test_flat_reconciliation_finishes_probe_without_rearming_strategy(engine):
    await engine.probe_once()
    await drain(engine)
    intent = engine.account.sent[0]
    engine.db.order_state(intent.client_id, OrderState.CANCELED)
    await Engine.safe_reconcile(engine)
    assert not engine.probe_active
    assert engine.db.flag("probe_pending") == "0"
    assert engine.db.flag("probe_used") == "1"
    assert engine.session_end == 0
    assert engine.session_positions == 0
    engine.decide()
    assert engine.state == EngineState.READY
    assert engine.entries_stopped
    assert not engine.entry_tasks
    assert "probe diblok" in await engine.probe_once()


async def test_restart_never_replays_uncertain_probe(engine):
    await engine.probe_once()
    await drain(engine)
    engine.db.order_state(engine.account.sent[0].client_id, OrderState.UNKNOWN)
    resumed = Engine(engine.config, engine.root)
    try:
        assert resumed.state == EngineState.STARTING
        assert resumed.entries_stopped
        assert resumed.probe_active
        assert resumed.session_end == 0
        assert resumed.db.flag("probe_used") == "1"
        assert "probe diblok" in await resumed.probe_once()
        assert not resumed.entry_tasks
    finally:
        resumed.recorder.close()
        resumed.db.close()


@pytest.mark.parametrize("completed", [2, 3])
async def test_closed_probe_is_excluded_from_strategy_position_count(engine, completed):
    engine.session_positions = completed
    await engine.probe_once()
    await drain(engine)
    engine.db.order_state(engine.account.sent[0].client_id, OrderState.FILLED)
    engine.last_position_qty = Decimal("0.00013")
    await Engine.safe_reconcile(engine)
    assert engine.session_positions == completed
    assert not engine.probe_active
    assert engine.entries_stopped


async def test_strategy_entry_still_respects_session_position_cap(engine):
    engine.state = EngineState.LIVE
    engine.session_end = time.monotonic() + 60
    engine.session_positions = engine.config.max_session_positions
    await engine.enter("buy")
    assert not engine.account.sent
    assert not engine.db.unresolved()


async def test_probe_waits_for_stop_cancel_confirmation_before_finishing(engine):
    await engine.probe_once()
    await drain(engine)
    engine.db.order_state(engine.account.sent[0].client_id, OrderState.CANCELED)
    engine.db.intent(99, "STOP", "sell", "0.00013", "82000")
    engine.db.order_state(99, OrderState.UNKNOWN)
    engine.protection_id = 99
    await Engine.safe_reconcile(engine)
    assert engine.probe_active
    assert engine.protection_id == 99
    assert "uji probe belum selesai" in engine.start_session()
    engine.account.statuses = {99: {"status": "canceled", "filled_base_amount": "0"}}
    await Engine.safe_reconcile(engine)
    assert not engine.reconcile_failed
    assert engine.probe_active
    await Engine.safe_reconcile(engine)
    assert not engine.probe_active
    assert engine.protection_id is None
    assert engine.entries_stopped


async def test_exposure_read_does_not_refresh_funding_audit_or_finish_probe(engine):
    await engine.probe_once()
    await drain(engine)
    engine.last_reconcile_ok = time.monotonic() - 16
    verified_at = engine.last_reconcile_ok
    await Engine.safe_reconcile(engine)
    assert not engine.reconcile_failed
    assert engine.last_reconcile_ok == verified_at
    assert engine.account_snapshot.received_at > time.time() - 1
    assert engine.probe_active
    assert engine.account.probe_scopes[-1] == engine.account_snapshot.funding_flows
    assert "audit riwayat lengkap wajib belum selesai" in engine.live_blockers()


async def test_probe_cannot_finish_when_final_funding_audit_fails(engine):
    await engine.probe_once()
    await drain(engine)
    engine.db.order_state(engine.account.sent[0].client_id, OrderState.CANCELED)

    async def unavailable(market_id, verified_flows=None):
        assert verified_flows is None
        raise RuntimeError("mock funding audit failed")

    engine.account.probe_snapshot = unavailable
    await Engine.safe_reconcile(engine)
    assert engine.reconcile_failed
    assert engine.probe_active
    assert engine.db.flag("probe_pending") == "1"
    assert engine.entries_stopped


async def test_probe_only_scope_rejects_strategy_even_before_and_after_probe(engine):
    engine.config = engine.config.model_copy(update={"live_entry_scope": "probe_only"})
    assert "START_LIVE dilarang" in engine.start_session()
    assert not engine.probe_blockers()
    engine.state = EngineState.LIVE
    engine.session_end = time.monotonic() + 60
    await engine.enter("buy")
    assert not engine.account.sent
    engine.state = EngineState.READY
    engine.session_end = 0
    await engine.probe_once()
    await drain(engine)
    assert len(engine.account.sent) == 1
    assert engine.account.sent[0].kind == "PROBE"
    assert "START_LIVE dilarang" in engine.start_session()


async def test_probe_waits_for_exchange_stop_confirmation_before_closing(engine):
    from unittest.mock import AsyncMock

    engine.probe_active = True
    engine.db.set_flag("probe_pending", "1")
    engine.account_snapshot = replace(engine.account_snapshot, position_qty=Decimal("0.00013"))
    engine.protection_id = 99
    engine.protect_sent_at = time.monotonic()
    engine.last_decision = Decision("buy", Decimal(1), Decimal(0), Decimal(1), ())
    engine.close_position = AsyncMock(return_value="mock close")
    await engine.manage_position()
    engine.close_position.assert_not_awaited()
    engine.protection_confirmed = True
    await engine.manage_position()
    engine.close_position.assert_awaited_once()


async def test_unknown_stop_immediately_requests_emergency_close_without_resending_stop(engine):
    from unittest.mock import AsyncMock

    engine.probe_active = True
    engine.db.set_flag("probe_pending", "1")
    engine.account_snapshot = replace(
        engine.account_snapshot, position_qty=Decimal("0.00013"), position_price=Decimal("83000")
    )
    engine.account.send_stop = AsyncMock(side_effect=TimeoutError)
    engine.close_position = AsyncMock(return_value="mock close")
    await engine.protect_position(engine.account_snapshot)
    assert engine.db.flag("close_requested") == "1"
    assert engine.db.unresolved()[0]["state"] == OrderState.UNKNOWN
    await engine.manage_position()
    engine.close_position.assert_awaited_once()
    engine.account.send_stop.assert_awaited_once()
    assert not engine.protection_confirmed


async def test_stop_confirmation_timeout_closes_even_when_reconciliation_is_down(engine):
    from unittest.mock import AsyncMock

    engine.probe_active = True
    engine.account_snapshot = replace(engine.account_snapshot, position_qty=Decimal("0.00013"))
    engine.protection_id = 99
    engine.protect_sent_at = time.monotonic() - 9
    engine.reconcile_failed = True
    engine.data_valid = lambda: False
    engine.close_position = AsyncMock(return_value="mock close")
    await engine.manage_position()
    engine.close_position.assert_awaited_once()
    assert not engine.protection_confirmed


def test_source_budget_never_uses_collateral_conversion_difference_as_deposit_headroom(engine):
    engine.config = engine.config.model_copy(update={"source_funding_spent_usd": Decimal(50)})
    status = engine.status()["account"]
    assert status["gross_history_inflow"] == "49.43"
    assert status["source_funding_budget_remaining"] == "0"
    assert "funding_budget_remaining" not in status
    engine.config = engine.config.model_copy(update={"source_funding_spent_usd": None})
    assert engine.status()["account"]["source_funding_budget_remaining"] is None


def test_probe_preflight_reserves_reads_and_transaction_capacity(engine):
    engine.account.rate.claim("reconcile", cost=34)
    assert engine.account.rate.can_claim("entry", cost=2)
    assert "jatah pengiriman entry belum tersedia" in engine.probe_blockers()


async def test_restart_reconciles_exchange_stop_and_closes_actual_partial_fill(engine):
    qty = Decimal("0.00007")
    engine.db.set_flag("probe_used", "1")
    engine.db.set_flag("probe_pending", "1")
    engine.db.intent(70, "PROBE", "buy", "0.00013", "83010")
    engine.db.order_state(70, OrderState.FILLED)
    engine.db.add_fill("fixture-partial", 70, str(qty), "83000", "0", 1000)
    engine.db.intent(99, "STOP", "sell", str(qty), "82461.3")
    engine.db.order_state(99, OrderState.SENT)
    engine.account.current = replace(
        engine.account_snapshot, position_qty=qty, position_price=Decimal("83000")
    )
    engine.account.statuses = {
        99: {
            "status": "pending",
            "reduce_only": True,
            "is_ask": True,
            "market_index": 1,
            "type": "stop-loss",
            "remaining_base_amount": str(qty),
            "filled_base_amount": "0",
            "trigger_price": "82709.5",
            "order_index": 199,
            "client_order_index": 99,
        }
    }
    resumed = Engine(engine.config, engine.root)
    try:
        resumed.account = engine.account
        resumed.market = engine.market
        resumed.books = engine.books
        resumed.data_valid = lambda: False  # lost comparator never blocks reducing exposure
        await resumed.safe_reconcile()
        assert not resumed.reconcile_failed
        assert resumed.protection_confirmed
        assert not resumed.account.sent
        await resumed.manage_position()
        assert len(resumed.account.sent) == 1
        assert resumed.account.sent[0].kind == "EXIT"
        assert resumed.account.sent[0].quantity == qty
        await resumed.manage_position()
        assert len(resumed.account.sent) == 1
        assert resumed.db.flag("probe_pending") == "1"
    finally:
        resumed.recorder.close()
        resumed.db.close()


async def test_flat_probe_keeps_persistent_pending_until_cancel_and_full_audit(engine):
    engine.probe_active = True
    engine.db.set_flag("probe_pending", "1")
    engine.last_position_qty = Decimal("0.00013")
    engine.db.intent(99, "STOP", "sell", "0.00013", "82000")
    engine.db.order_state(99, OrderState.UNKNOWN)
    engine.protection_id = 99
    await Engine.safe_reconcile(engine)
    assert engine.db.flag("probe_pending") == "1"
    resumed = Engine(engine.config, engine.root)
    try:
        assert resumed.probe_active
        assert resumed.protection_id == 99
        assert resumed.entries_stopped
    finally:
        resumed.recorder.close()
        resumed.db.close()


async def test_fill_after_flat_snapshot_cannot_finish_probe_or_discard_ownership(engine):
    from unittest.mock import AsyncMock

    engine.probe_active = True
    engine.db.set_flag("probe_pending", "1")
    engine.db.intent(70, "PROBE", "buy", "0.00013", "83010")
    engine.db.order_state(70, OrderState.SENT)
    engine.account.statuses = {70: {"status": "filled", "filled_base_amount": "0.00013"}}
    engine.account.recent_trades = AsyncMock(
        return_value=[
            {
                "trade_id": 1,
                "bid_account_id": 77,
                "ask_account_id": 88,
                "bid_client_id": 70,
                "is_maker_ask": True,
                "taker_fee": "0",
                "size": "0.00013",
                "price": "83000",
                "timestamp": 1000,
            }
        ]
    )
    engine.account.send_stop = AsyncMock(return_value="mock-stop")
    await Engine.safe_reconcile(engine)
    assert engine.probe_active
    assert engine.db.flag("last_flat_epoch") is None
    assert engine.last_account_poll < time.monotonic() - 11
    engine.account.current = replace(
        engine.account_snapshot, position_qty=Decimal("0.00013"), position_price=Decimal("83000")
    )
    await Engine.safe_reconcile(engine)
    assert not engine.manual_exposure
    await engine.manage_position()
    engine.account.send_stop.assert_awaited_once()


async def test_foreground_polls_stop_before_confirmation_deadline_and_then_closes(engine):
    engine.probe_active = True
    engine.db.set_flag("probe_pending", "1")
    engine.db.intent(70, "PROBE", "buy", "0.00013", "83010")
    engine.db.order_state(70, OrderState.FILLED)
    engine.db.add_fill("fixture", 70, "0.00013", "83000", "0", 1000)
    engine.db.intent(99, "STOP", "sell", "0.00013", "82461.3")
    engine.db.order_state(99, OrderState.SENT)
    engine.protection_id = 99
    engine.protect_sent_at = time.monotonic() - 3
    engine.last_account_poll = time.monotonic() - 3
    engine.account_snapshot = replace(
        engine.account_snapshot, position_qty=Decimal("0.00013"), position_price=Decimal("83000")
    )
    engine.account.current = engine.account_snapshot
    engine.account.statuses = {
        99: {
            "status": "pending",
            "reduce_only": True,
            "is_ask": True,
            "market_index": 1,
            "type": "stop-loss",
            "remaining_base_amount": "0.00013",
            "filled_base_amount": "0",
            "trigger_price": "82709.5",
            "order_index": 199,
            "client_order_index": 99,
        }
    }
    closed = asyncio.Event()

    async def reconcile():
        await Engine.safe_reconcile(engine)

    async def close():
        assert engine.protection_confirmed
        engine.exit_id = 777
        closed.set()
        return "mock close"

    engine.safe_reconcile = reconcile
    engine.close_position = close
    task = asyncio.create_task(engine.loop())
    try:
        await asyncio.wait_for(closed.wait(), 1)
        assert engine.protection_confirmed
        assert time.monotonic() - engine.protect_sent_at < 8
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if engine.reconcile_task:
            await asyncio.gather(engine.reconcile_task, return_exceptions=True)


async def test_local_signing_rejection_is_final_and_never_rearms_probe(engine):
    from unittest.mock import AsyncMock

    engine.account.send_ioc = AsyncMock(side_effect=LocalOrderRejected("OrderExpiry is invalid"))
    await engine.probe_once()
    await drain(engine)
    assert not engine.db.unresolved()
    row = engine.db.conn.execute("SELECT state FROM intents").fetchone()
    assert row[0] == OrderState.REJECTED
    await Engine.safe_reconcile(engine)
    assert not engine.probe_active
    assert engine.db.flag("probe_used") == "1"
    assert "probe diblok" in await engine.probe_once()
    engine.account.send_ioc.assert_awaited_once()
