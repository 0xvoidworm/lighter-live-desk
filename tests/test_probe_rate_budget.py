"""Actual adapter request paths with fake SDK responses, without signer/network."""

import time
from types import SimpleNamespace

import pytest

from lldesk.account import LighterAccount, RateBudget
from lldesk.config import Config


@pytest.fixture
def adapter():
    value = LighterAccount(Config(mode="live_capable", account_index=77))
    acct = SimpleNamespace(
        index=77,
        l1_address="fixture-public-address",
        positions=[],
        assets=[],
        total_asset_value="49.43",
        available_balance="49.43",
        account_type=0,
        account_trading_mode=1,
        collateral="49.43",
        total_order_count=0,
        pending_order_count=0,
    )
    history_calls = []

    async def account(**kwargs):
        return SimpleNamespace(code=200, accounts=[acct])

    async def active_orders(**kwargs):
        return SimpleNamespace(code=200, orders=[])

    async def order_statuses(**kwargs):
        return SimpleNamespace(code=200, orders=[])

    async def trades(**kwargs):
        return SimpleNamespace(code=200, trades=[])

    async def deposits(**kwargs):
        history_calls.append("deposit")
        rows = (
            []
            if kwargs["cursor"]
            else [SimpleNamespace(id="fixture", status="completed", asset_id=3, amount="49.43")]
        )
        return SimpleNamespace(code=200, deposits=rows, cursor="" if kwargs["cursor"] else "d")

    async def withdrawals(**kwargs):
        history_calls.append("withdraw")
        return SimpleNamespace(code=200, withdraws=[], cursor="w")

    async def transfers(**kwargs):
        history_calls.append("transfer")
        return SimpleNamespace(code=200, transfers=[], cursor="")

    value.account_api = SimpleNamespace(account=account)
    value.order_api = SimpleNamespace(
        account_active_orders=active_orders, account_orders=order_statuses, trades=trades
    )
    value.transaction_api = SimpleNamespace(
        deposit_history=deposits, withdraw_history=withdrawals, transfer_history=transfers
    )
    value.auth = lambda: "fixture-auth"
    return value, history_calls


async def exposure_reads(adapter, flows):
    snapshot = await adapter.probe_snapshot(1, flows)
    await adapter.order_statuses([99], priority="probe_read")
    await adapter.recent_trades(1, 0, priority="probe_read")
    assert snapshot.funding_flows == flows


async def test_probe_reads_leave_transaction_reserve_at_busy_read_budget(adapter):
    value, history_calls = adapter
    flows = (("deposit:fixture", "49.43"),)
    value.rate.claim("reconcile", cost=38)
    assert value.rate.can_claim("entry", cost=2)
    value.rate.claim("entry", cost=2)
    assert not value.rate.can_claim("reconcile")
    await exposure_reads(value, flows)
    value.rate.claim("probe_read", cost=3)
    assert len(value.rate.calls) == 47
    with pytest.raises(RuntimeError, match="rate reserve"):
        await exposure_reads(value, flows)
    for _ in range(4):
        value.rate.claim("exit", cost=2)  # stop, emergency close, cancel and one retry
    assert len(value.rate.calls) == 55
    assert not history_calls
    with pytest.raises(RuntimeError, match="rate reserve"):
        value.rate.claim("exit", cost=2)


async def test_final_probe_snapshot_fetches_funding_without_using_transaction_reserve(adapter):
    value, history_calls = adapter
    value.rate.claim("reconcile", cost=39)
    result = await value.probe_snapshot(1)
    await value.recent_trades(1, 0, priority="probe_read")
    assert result.funding_flows == (("deposit:fixture", "49.43"),)
    assert history_calls == ["deposit", "deposit", "withdraw", "withdraw", "transfer"]
    assert len(value.rate.calls) == 47
    assert value.rate.can_claim("exit", cost=8)


async def test_exit_reserve_exhaustion_cannot_fake_full_funding_audit(adapter):
    value, history_calls = adapter
    value.rate.claim("exit", cost=54)
    with pytest.raises(RuntimeError, match="rate reserve"):
        await value.probe_snapshot(1)
    assert not history_calls
    assert len(value.rate.calls) == 54


def test_entry_capacity_check_does_not_consume_or_raise_budget(monkeypatch):
    budget = RateBudget()
    budget.claim("reconcile", cost=38)
    for _ in range(10):
        assert budget.can_claim("entry", cost=2)
        assert not budget.can_claim("entry", cost=3)
    assert len(budget.calls) == 38
    now = time.monotonic()
    monkeypatch.setattr("lldesk.account.time.monotonic", lambda: now + 61)
    assert budget.can_claim("entry", cost=40)
    assert len(budget.calls) == 0
