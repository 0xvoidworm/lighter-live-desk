import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lldesk.account import AccountSnapshot, LighterAccount
from lldesk.config import Config
from lldesk.engine import ACCOUNT_RECONCILE_INTERVAL_SECONDS, Engine
from lldesk.models import EngineState
from lldesk.strategy import Decision


@pytest.fixture
def monitor(tmp_path):
    config = Config(
        mode="live_capable",
        account_index=77,
        expected_account_type_code=0,
        expected_account_trading_mode=1,
    )
    engine = Engine(config, tmp_path)
    engine.account = SimpleNamespace(account_index=77)
    engine.account_snapshot = AccountSnapshot(
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
    )
    engine.last_reconcile_ok = time.monotonic()
    engine.reconcile_failed = False
    engine.history_required = False
    engine.state_refresh_failed = False
    engine.state = EngineState.PAUSED
    engine.data_valid = lambda: True
    decision = Decision(None, Decimal(0), Decimal(0), Decimal(1), ())
    engine.strategy = SimpleNamespace(evaluate=lambda *args: decision)
    yield engine
    engine.recorder.close()
    engine.db.close()


async def test_flat_idle_keeps_margin_and_order_gates_closed(monitor):
    monitor.decide()
    assert monitor.state == EngineState.IDLE
    assert monitor.entries_stopped
    assert not monitor.margin_settings_valid(monitor.account_snapshot)
    blockers = monitor.live_blockers()
    assert "konfigurasi observe/allow_live_orders=false" in blockers
    assert "mode akun atau margin BTC belum cocok dengan verifikasi lokal" in blockers
    monitor.db.command("START_LIVE")
    await monitor.commands()
    await monitor.enter("buy")
    assert monitor.state == EngineState.IDLE
    assert monitor.entries_stopped
    assert monitor.session_end == 0
    assert not monitor.db.unresolved()
    assert not monitor.entry_tasks


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position_qty", Decimal("0.001")),
        ("active_orders", ({"client_order_index": 1},)),
        ("other_positions", ({"market_id": 2},)),
        ("other_assets", ({"symbol": "LIT"},)),
        ("account_type_code", 1),
        ("account_trading_mode", 9),
        ("received_at", time.time() - 30),
    ],
)
def test_idle_leaves_standby_when_exposure_or_account_verification_changes(monitor, field, value):
    monitor.decide()
    monitor.account_snapshot = replace(monitor.account_snapshot, **{field: value})
    monitor.decide()
    assert monitor.state == EngineState.PAUSED
    assert monitor.entries_stopped


@pytest.mark.parametrize(
    "flag",
    (
        "funding_change_pending",
        "account_incident_pending",
        "total_loss_stop",
        "close_requested",
        "probe_pending",
    ),
)
def test_idle_does_not_hide_persistent_review_flags(monitor, flag):
    monitor.decide()
    monitor.db.set_flag(flag, "1")
    monitor.decide()
    assert monitor.state == EngineState.PAUSED


@pytest.mark.parametrize(
    "fault", ("stale_reconcile", "failed_reconcile", "market", "journal", "intent", "probe")
)
def test_idle_requires_fresh_successful_reconciliation_and_clean_journal(monitor, fault):
    monitor.decide()
    if fault == "stale_reconcile":
        monitor.last_reconcile_ok = time.monotonic() - 16
        monitor.history_required = True
    elif fault == "failed_reconcile":
        monitor.reconcile_failed = True
    elif fault == "market":
        monitor.data_valid = lambda: False
    elif fault == "journal":
        monitor.journal_ok = False
    elif fault == "probe":
        monitor.probe_active = True
    else:
        monitor.db.intent(1, "ENTRY", "buy", "0.0001", "80000")
    monitor.decide()
    assert monitor.state == EngineState.PAUSED
    assert monitor.entries_stopped


def test_idle_cannot_skip_warmup_or_recover_halted_engine(monitor):
    monitor.state = EngineState.WARMING_UP
    monitor.strategy = SimpleNamespace(evaluate=lambda *args: Decision(None, None, None, None, ()))
    monitor.decide()
    assert monitor.state == EngineState.WARMING_UP
    monitor.strategy = SimpleNamespace(
        evaluate=lambda *args: Decision(None, Decimal(0), Decimal(0), Decimal(1), ())
    )
    monitor.state = EngineState.HALTED
    monitor.decide()
    assert monitor.state == EngineState.HALTED


async def test_failed_reconcile_needs_success_before_idle_recovers(monitor):
    async def failed():
        raise RuntimeError("mock unavailable")

    monitor.reconcile = failed
    await monitor.safe_reconcile()
    monitor.decide()
    assert monitor.reconcile_failed
    assert monitor.state == EngineState.PAUSED
    assert "rekonsiliasi akun terakhir gagal" in monitor.live_blockers()

    async def recovered():
        monitor.last_reconcile_ok = time.monotonic()

    monitor.reconcile = recovered
    await monitor.safe_reconcile()
    monitor.decide()
    assert not monitor.reconcile_failed
    assert monitor.state == EngineState.IDLE
    assert monitor.entries_stopped


async def test_full_flat_reconciliation_cadence_keeps_exit_reserve(monkeypatch):
    adapter = LighterAccount(Config(mode="live_capable", account_index=77))
    clock = [1000.0]
    monkeypatch.setattr("lldesk.account.time.monotonic", lambda: clock[0])
    account = SimpleNamespace(
        index=77,
        l1_address="fixture",
        positions=[],
        assets=[],
        total_asset_value="49.43",
        available_balance="49.43",
        account_type=0,
        account_trading_mode=1,
    )

    async def account_read(**kwargs):
        return SimpleNamespace(code=200, accounts=[account])

    async def orders_read(**kwargs):
        return SimpleNamespace(code=200, orders=[])

    async def deposits(**kwargs):
        rows = (
            []
            if kwargs["cursor"]
            else [SimpleNamespace(id="fixture", status="completed", asset_id=3, amount="49.43")]
        )
        return SimpleNamespace(code=200, deposits=rows, cursor="" if kwargs["cursor"] else "d")

    async def withdrawals(**kwargs):
        return SimpleNamespace(code=200, withdraws=[], cursor="w")

    async def transfers(**kwargs):
        return SimpleNamespace(code=200, transfers=[], cursor="")

    async def trades(**kwargs):
        return SimpleNamespace(code=200, trades=[])

    adapter.account_api = SimpleNamespace(account=account_read)
    adapter.order_api = SimpleNamespace(account_active_orders=orders_read, trades=trades)
    adapter.transaction_api = SimpleNamespace(
        deposit_history=deposits, withdraw_history=withdrawals, transfer_history=transfers
    )
    adapter.auth = lambda: "fixture"
    for cycle in range(51):
        clock[0] = 1000 + cycle * ACCOUNT_RECONCILE_INTERVAL_SECONDS
        snapshot = await adapter.snapshot(1)
        assert snapshot.funding_flows == (("deposit:fixture", "49.43"),)
        assert await adapter.recent_trades(1, 0) == []
        assert len(adapter.rate.calls) <= 40
    adapter.rate.claim("exit", cost=12)
    assert len(adapter.rate.calls) <= 55
