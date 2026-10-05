"""Offline audit: assertions describe desired safety behavior, so defects fail.

Run with the existing Lighter Live Desk venv. Every engine uses pytest tmp_path;
all exchange adapters/signers below are mocks. No secrets or network access.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lldesk.account import AccountSnapshot, LighterAccount, RateBudget
from lldesk.config import Config
from lldesk.engine import Engine
from lldesk.market import Feeds
from lldesk.models import EngineState, MarketEvent, Stamp
from lldesk.risk import size_entry
from lldesk.strategy import Decision


def snapshot(qty="0", equity="50"):
    return AccountSnapshot(
        equity=Decimal(equity),
        available=Decimal("40"),
        account_type_code=0,
        position_qty=Decimal(qty),
        position_price=Decimal("83000") if Decimal(qty) else Decimal(0),
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
        collateral=Decimal(equity),
        funding_flows=(("fixture-deposit", "50"),),
        account_trading_mode=0,
        margin_mode=0,
        initial_margin_fraction=Decimal(1),
    )


@pytest.fixture
def engine(tmp_path):
    config = Config(
        mode="live_capable",
        account_index=77,
        expected_account_type_code=0,
        expected_account_trading_mode=0,
        expected_margin_mode=0,
        expected_initial_margin_fraction=Decimal(1),
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
    value.account_snapshot = snapshot()
    value._update_baseline(value.account_snapshot)
    value.db.event(
        "NET_CASH_AUDIT",
        {
            "equity_after": "50",
            "cash_change_usdc": "0",
            "fingerprint": value.db.accounting_fingerprint(),
        },
    )
    value.state_refresh_failed = False
    value.history_required = False
    value.fx, value.fx_at = Decimal(1), time.monotonic()
    value.strategy.coverage = lambda: {"ready": True}
    value.source_freshness = lambda: {"passed": True, "reason": "isolated_fixture"}
    value.books["binance"].binance_snapshot(
        {"lastUpdateId": 1, "bids": [["83000", "2"]], "asks": [["83000.1", "2"]]}, Stamp()
    )
    value.state = EngineState.LIVE
    value.entries_stopped = False
    value.session_end = time.monotonic() + 1800
    value.last_decision = Decision("buy", Decimal(30), Decimal(0), Decimal(1), ())
    yield value
    value.recorder.close()
    value.db.close()


class Exchange:
    account_index = 77

    def __init__(self, current, statuses=None):
        self.current = current
        self.statuses = statuses or {}
        self.stops = []
        self.exits = []
        self.rate = RateBudget()

    async def snapshot(self, market_id):
        return replace(self.current, received_at=time.time())

    async def order_statuses(self, ids):
        return {key: item for key, item in self.statuses.items() if key in ids}

    async def recent_trades(self, market_id, after_id):
        return []

    async def send_stop(self, intent, *args):
        self.stops.append(intent)
        return "mock-stop"

    async def send_ioc(self, intent, *args, **kwargs):
        self.exits.append(intent)
        return "mock-ioc"


@pytest.mark.asyncio
async def test_exit_retries_after_terminal_cancel_variant(engine):
    """Canceled IOC is definitive; remaining position must allow the next exit."""
    engine.db.intent(1, "ENTRY", "buy", "0.0002", "83000")
    engine.db.order_state(1, "FILLED")
    engine.db.add_fill("entry-fill", 1, "0.0002", "83000", "0", None)
    engine.db.intent(2, "EXIT", "sell", "0.0002", "82751")
    engine.db.order_state(2, "SENT")
    engine.db.set_flag("exit_attempts", "1")
    engine.db.set_flag("close_requested", "1")
    engine.exit_id = 2
    exchange = Exchange(
        snapshot("0.0002"),
        {2: {"status": "canceled-too-much-slippage", "filled_base_amount": "0"}},
    )
    engine.account = exchange
    await engine.reconcile()
    await engine.manage_position()
    assert exchange.exits, (
        "terminal canceled-too-much-slippage left exit_id=2; "
        "intent is CANCELED and is excluded from subsequent status queries"
    )


@pytest.mark.asyncio
async def test_funding_cap_does_not_skip_position_protection(engine):
    """A balance guard must still protect a known open position."""
    engine.db.intent(1, "ENTRY", "buy", "0.0002", "83000")
    engine.db.order_state(1, "FILLED")
    engine.db.add_fill("entry-fill", 1, "0.0002", "83000", "0", None)
    exchange = Exchange(snapshot("0.0002", equity="50.10"))
    engine.account = exchange
    await engine.safe_reconcile()
    await engine.manage_position()
    assert exchange.stops or exchange.exits, (
        "equity=50.10 aborted reconcile before stop/exit management; "
        f"position={engine.account_snapshot.position_qty}, reason={engine.reason}"
    )


@pytest.mark.asyncio
async def test_confirmed_stop_covers_latest_position_quantity(engine):
    """Later fills cannot leave only the first partial quantity protected."""
    engine.db.intent(1, "ENTRY", "buy", "0.0002", "83000")
    engine.db.order_state(1, "SENT")
    engine.db.add_fill("partial-1", 1, "0.0001", "83000", "0", None)
    exchange = Exchange(
        snapshot("0.0001"),
        {1: {"status": "open", "filled_base_amount": "0.0001"}},
    )
    engine.account = exchange
    await engine.reconcile()
    await engine.manage_position()
    stop_id = engine.protection_id
    exchange.statuses[stop_id] = {
        "client_order_index": stop_id,
        "order_index": 199,
        "status": "pending",
        "reduce_only": True,
        "type": "stop-loss",
        "remaining_base_amount": "0.0001",
        "is_ask": True,
        "market_index": 1,
        "trigger_price": "82709.5",
    }
    await engine.reconcile()
    assert engine.protection_confirmed, "a fully covering stop must be confirmed first"
    exchange.current = snapshot("0.0002")
    engine.db.add_fill("partial-2", 1, "0.0001", "83000", "0", None)
    exchange.statuses = {
        1: {"status": "filled", "filled_base_amount": "0.0002"},
        stop_id: {
            "client_order_index": stop_id,
            "order_index": 199,
            "status": "pending",
            "reduce_only": True,
            "type": "stop-loss",
            "remaining_base_amount": "0.0001",
            "initial_base_amount": "0.0001",
            "is_ask": True,
            "market_index": 1,
            "trigger_price": "82709.5",
        },
    }
    await engine.reconcile()
    await engine.manage_position()
    covered = sum((item.quantity for item in exchange.stops), Decimal(0))
    assert not engine.protection_confirmed or covered >= abs(exchange.current.position_qty), (
        "protection_confirmed=True with stop qty=0.0001 and position qty=0.0002"
    )
    assert exchange.exits and exchange.exits[-1].quantity == Decimal("0.0002")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["CANCEL_ENTRIES", "STOP_ENTRIES"])
async def test_cancel_blocks_entry_waiting_for_signer_lock(engine, monkeypatch, command):
    """A local entry waiting for the signer must be invalidated by cancel."""
    adapter = LighterAccount(engine.config)
    engine.config = engine.config.model_copy(update={"session_accept_reported_fees": True})
    engine.session.authorize(
        "offline-lock-test", engine.config, str(engine.account_snapshot.equity)
    )
    engine.session.activate()
    engine.loaded_session_permission_id = "offline-lock-test"  # Previously admitted fixture.
    engine.db.event(
        "NET_CASH_AUDIT",
        {
            "equity_after": str(engine.account_snapshot.equity),
            "cash_change_usdc": "0",
            "fingerprint": engine.db.accounting_fingerprint(),
        },
    )
    engine.last_reconcile_ok = time.monotonic()
    engine.reconcile_failed = False

    class Signer:
        ORDER_TYPE_LIMIT = 0
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0

        def __init__(self):
            self.calls = []

        async def create_order(self, **kwargs):
            self.calls.append(kwargs)
            return None, SimpleNamespace(code=200, tx_hash="mock-ack"), None

    signer = Signer()
    adapter.signer = signer

    async def statuses(ids):
        return {}

    async def reconcile_noop(*, reserved=False):
        # Isolated completed audit; this test targets the actual signer-lock guard.
        engine.last_reconcile_ok = time.monotonic()
        engine.history_required = False
        engine.reconcile_failed = False
        engine.state_refresh_failed = False
        engine.audit_binding = engine.preflight_binding()

    adapter.order_statuses = statuses
    engine.account = adapter
    engine.books["binance"].bridged = True
    engine.feeds = Feeds(1, engine.queue)
    engine.feeds.binance_public_ok = engine.feeds.binance_trades_ok = True
    monkeypatch.setattr(engine, "data_valid", lambda: True)
    monkeypatch.setattr(engine, "safe_reconcile", reconcile_noop)
    await adapter.lock.acquire()
    entry_task = asyncio.create_task(engine.enter("buy"))
    try:
        for _ in range(20):
            if engine.db.unresolved():
                break
            await asyncio.sleep(0)
        assert engine.db.unresolved(), "test setup did not reach durable intent"
        engine.db.command(command)
        await engine.commands()
        assert engine.entries_stopped
    finally:
        adapter.lock.release()
        await entry_task
    assert not signer.calls, "create_order ran after CANCEL_ENTRIES marked intent UNKNOWN"
    assert engine.db.conn.execute("SELECT state FROM intents").fetchone()[0] == "CANCELED"


@pytest.mark.asyncio
async def test_account_timestamp_tracks_equity_response_not_history_completion(monkeypatch):
    """Slow later calls must not make an old equity response look fresh."""
    config = Config(mode="live_capable", account_index=77)
    adapter = LighterAccount(config)
    clock = [1000.0]
    account_value = SimpleNamespace(
        index=77,
        l1_address="mock-public-address",
        positions=[],
        assets=[],
        total_asset_value="50",
        available_balance="40",
        account_type=0,
        account_trading_mode=0,
    )

    async def account(**kwargs):
        return SimpleNamespace(code=200, accounts=[account_value])

    async def orders(**kwargs):
        return SimpleNamespace(code=200, orders=[])

    async def history(address):
        clock[0] += 20
        return ()

    adapter.account_api = SimpleNamespace(account=account)
    adapter.order_api = SimpleNamespace(account_active_orders=orders)
    adapter.auth = lambda: "mock-auth"
    adapter.funding_history = history
    monkeypatch.setattr("lldesk.account.time.time", lambda: clock[0])
    actual = await adapter.snapshot(1)
    assert clock[0] - actual.received_at >= 20, (
        f"equity response at 1000, completed at {clock[0]}, "
        f"snapshot received_at={actual.received_at} makes 20s-old equity look new"
    )


@pytest.mark.asyncio
async def test_failed_reconciliation_preserves_exit_rate_capacity(engine):
    """Repeated failed polling must leave capacity for a close order."""
    adapter = LighterAccount(engine.config)

    async def unavailable_account(**kwargs):
        return SimpleNamespace(code=503, accounts=[])

    adapter.account_api = SimpleNamespace(account=unavailable_account)
    engine.account = adapter
    for _ in range(55):
        await engine.safe_reconcile()
    assert engine.last_account_poll > 0, "failed polling must record its attempt time"
    try:
        adapter.rate.claim("exit")
        exit_available = True
    except RuntimeError:
        exit_available = False
    assert exit_available, "failed polls consumed all 55 slots shared with emergency exits"


@pytest.mark.asyncio
async def test_stop_command_remains_responsive_during_account_poll(engine):
    """A slow account poll must not disable controls past the 3s heartbeat window."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowExchange(Exchange):
        async def snapshot(self, market_id):
            entered.set()
            await release.wait()
            return self.current

    engine.account = SlowExchange(snapshot())
    engine.queue.put_nowait(MarketEvent("binance", "bookTicker", {}, Stamp()))
    engine.db.save_status(engine.status())
    worker = asyncio.create_task(engine.loop())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        command_id = engine.db.command("STOP_ENTRIES")
        await asyncio.sleep(3.2)
        command_state = engine.db.conn.execute(
            "SELECT state FROM commands WHERE id=?", (command_id,)
        ).fetchone()[0]
        heartbeat_age = time.time() - engine.db.read_status()["updated_at"]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert command_state == "DONE" and heartbeat_age < 3, (
        f"command={command_state}, heartbeat_age={heartbeat_age:.1f}s: "
        "account polling blocks controls and makes API report DISCONNECTED"
    )


@pytest.mark.asyncio
async def test_rejected_entry_does_not_claim_ownership_of_manual_position(engine):
    """An unfilled rejected entry is not evidence that a position is owned."""
    engine.db.intent(1, "ENTRY", "buy", "0.0002", "83000")
    engine.db.order_state(1, "REJECTED")
    exchange = Exchange(snapshot("0.0001"))
    engine.account = exchange
    await engine.reconcile()
    assert engine.manual_exposure and not exchange.stops, (
        "REJECTED entry was accepted as ownership; engine submitted stop on manual position"
    )


@pytest.mark.asyncio
async def test_probe_uses_minimum_legal_size(engine, monkeypatch):
    """Runbook promises a minimum-size execution probe."""
    exchange = Exchange(snapshot())
    engine.account = exchange
    engine.books["binance"].bridged = True
    engine.feeds = Feeds(1, engine.queue)
    engine.feeds.binance_public_ok = engine.feeds.binance_trades_ok = True
    monkeypatch.setattr(engine, "data_valid", lambda: True)

    async def reconcile_noop(*, reserved=False):
        engine.last_reconcile_ok = time.monotonic()
        engine.reconcile_failed = False
        engine.history_required = False
        engine.audit_binding = engine.preflight_binding()

    monkeypatch.setattr(engine, "safe_reconcile", reconcile_noop)
    engine.state = EngineState.READY
    engine.entries_stopped = True
    engine.session_end = 0
    engine.last_reconcile_ok = time.monotonic()
    engine.reconcile_failed = False
    engine.db.record_flows((("fixture-deposit", "50"),))
    await engine.probe_once()
    await asyncio.gather(*engine.entry_tasks)
    assert len(exchange.exits) == 1
    sent = exchange.exits[0]
    assert sent.kind == "PROBE"
    assert sent.quantity == Decimal("0.00013"), (
        f"probe qty={sent.quantity}, notional={sent.quantity * sent.limit_price}; "
        "minimum legal BTC size at this price is 0.00013"
    )


@pytest.mark.asyncio
async def test_cancel_revokes_an_entry_scheduled_before_task_starts(engine, monkeypatch):
    engine.config = engine.config.model_copy(update={"session_accept_reported_fees": True})
    engine.session.authorize(
        "offline-epoch-test", engine.config, str(engine.account_snapshot.equity)
    )
    engine.session.activate()
    engine.loaded_session_permission_id = "offline-epoch-test"  # Previously admitted fixture.
    exchange = Exchange(snapshot())
    engine.account = exchange
    engine.books["binance"].bridged = True
    engine.feeds = Feeds(1, engine.queue)
    engine.feeds.binance_public_ok = engine.feeds.binance_trades_ok = True
    monkeypatch.setattr(engine, "data_valid", lambda: True)
    task = asyncio.create_task(engine.enter("buy", scheduled_epoch=engine.entry_epoch))
    await engine.cancel_entries()
    await task
    assert not exchange.exits and not engine.db.unresolved()


@pytest.mark.parametrize(
    "change",
    [
        {"market_index": 2},
        {"is_ask": False},
        {"remaining_base_amount": "0.0001"},
        {"trigger_price": "82000"},
        {"reduce_only": False},
        {"type": "limit"},
        {"status": "canceled-too-much-slippage"},
        {"client_order_index": 999},
        {"order_index": 0},
    ],
)
def test_protection_requires_market_side_quantity_trigger_and_reduce_only(engine, change):
    engine.protection_id = 2
    position = snapshot("0.0002")
    order = {
        "client_order_index": 2,
        "order_index": 199,
        "market_index": 1,
        "is_ask": True,
        "remaining_base_amount": "0.0002",
        "trigger_price": "82709.5",
        "reduce_only": True,
        "type": "stop-loss",
        "status": "pending",
    }
    assert engine.stop_covers_position(order, position)
    assert not engine.stop_covers_position(order | change, position)


def test_probe_minimum_cannot_exceed_risk_budget():
    with pytest.raises(ValueError, match="minimum"):
        size_entry(
            Config(max_trade_risk_usd=Decimal("0.01")),
            Decimal(50),
            Decimal(0),
            Decimal(0),
            Decimal(83000),
            "buy",
            Decimal("0.00007"),
            Decimal(10),
            5,
            1,
            Decimal(35),
            Decimal(0),
            minimum_only=True,
        )


@pytest.mark.asyncio
async def test_market_burst_keeps_order_and_bounds_heartbeat_writes(tmp_path, monkeypatch):
    engine = Engine(Config(), tmp_path)
    received = []
    saved = []
    monkeypatch.setattr(engine, "on_market", lambda event: received.append(event.payload["n"]))
    monkeypatch.setattr(engine.db, "save_status", lambda status: saved.append(time.monotonic()))
    for index in range(2000):
        engine.queue.put_nowait(MarketEvent("binance", "bookTicker", {"n": index}, Stamp()))
    command_id = engine.db.command("STOP_ENTRIES")
    worker = asyncio.create_task(engine.loop())
    try:
        async with asyncio.timeout(2):
            while len(received) < 2000:
                await asyncio.sleep(0.01)
        assert received == list(range(2000)), "batching must preserve every market event's order"
        assert len(saved) <= 8, "status writes must be bounded instead of one per market event"
        assert (
            engine.db.conn.execute(
                "SELECT state FROM commands WHERE id=?", (command_id,)
            ).fetchone()[0]
            == "DONE"
        )
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        engine.recorder.close()
        engine.db.close()
