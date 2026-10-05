"""Two-lane policy and actual preflight paths, entirely isolated from mainnet."""

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from test_diagnostic_observation import prepare
from test_probe_only import engine as engine_fixture
from test_probe_rate_budget import adapter as adapter_fixture
from test_protection_priority import exposed, stop_worker

from lldesk.account import RateBudget
from lldesk.engine import Engine
from lldesk.models import AccountEvent, Stamp
from lldesk.preflight import component_ages, state_complete

engine = engine_fixture
adapter = adapter_fixture


async def ready_proof(engine):
    prepare(engine)
    engine.preflight_in_progress = True
    with engine.account.rate.reserve("preflight", 10):
        assert await engine.prepare_preflight("one-attempt", sendable=True)
    engine.preflight_in_progress = False
    assert engine.preflight_valid("one-attempt", sending=True)


@pytest.mark.parametrize("fault", ["account", "orders", "pending", "missing", "cursor"])
async def test_each_state_component_is_independently_required(adapter, fault):
    value, history_calls = adapter
    original = await value.state_snapshot(1)
    assert len(value.rate.calls) == 2 and not history_calls
    assert state_complete(original)
    if fault == "account":
        changed = replace(original, received_monotonic=time.monotonic() - 16)
    elif fault == "orders":
        changed = replace(original, orders_received_monotonic=time.monotonic() - 16)
    elif fault == "pending":
        changed = replace(original, exchange_pending_count=1)
    elif fault == "missing":
        changed = replace(original, collateral=None)
    else:
        value.order_api.account_active_orders = AsyncMock(
            return_value=type(
                "Response",
                (),
                {
                    "code": 200,
                    "orders": [],
                    "next_cursor": "unread-page",
                },
            )()
        )
        with pytest.raises(RuntimeError, match="incomplete"):
            await value.state_snapshot(1, original)
        return
    assert not state_complete(changed)


async def test_position_get_cannot_refresh_old_order_stamp(adapter):
    value, _ = adapter
    original = replace(
        await value.state_snapshot(1), orders_received_monotonic=time.monotonic() - 16
    )
    later = await value.position_snapshot(1, original)
    assert component_ages(later)["account"] < 1
    assert component_ages(later)["active_orders"] >= 16
    assert not state_complete(later)


@pytest.mark.parametrize(
    "fault",
    [
        "account",
        "orders",
        "funds",
        "checkpoint",
        "config",
        "reconnect",
        "history",
        "expiry",
        "attempt",
        "cash",
    ],
)
async def test_invalid_evidence_cannot_pass_dispatch(engine, fault, monkeypatch):
    await ready_proof(engine)
    if fault == "account":
        engine.account_snapshot = replace(
            engine.account_snapshot, received_monotonic=time.monotonic() - 16
        )
    elif fault == "orders":
        engine.account_snapshot = replace(
            engine.account_snapshot, orders_received_monotonic=time.monotonic() - 16
        )
    elif fault == "funds":
        engine.account_snapshot = replace(engine.account_snapshot, equity=Decimal("49.44"))
    elif fault == "checkpoint":
        engine.db.set_flag("last_trade_id", "999")
    elif fault == "config":
        engine.config = engine.config.model_copy(update={"max_trade_risk_usd": Decimal("0.4")})
    elif fault == "reconnect":
        engine.on_account(AccountEvent(77, {"type": "disconnect"}, Stamp()))
    elif fault == "history":
        engine.reconcile_failed = True
    elif fault == "expiry":
        engine.preflight_evidence = replace(
            engine.preflight_evidence, expires=time.monotonic() - 0.001
        )
    elif fault == "cash":
        engine.db.event(
            "NET_CASH_AUDIT",
            {
                "equity_after": "48",
                "cash_change_usdc": "0",
                "fingerprint": engine.db.accounting_fingerprint(),
            },
        )
    else:
        assert not engine.preflight_valid("another-attempt", sending=True)
        return
    assert not engine.preflight_valid("one-attempt", sending=True)
    assert not engine.account.sent and not engine.db.unresolved()


async def test_restart_cannot_reuse_evidence_from_another_process(engine, tmp_path):
    await ready_proof(engine)
    other = Engine(engine.config, tmp_path / "new-process")
    try:
        assert other.preflight_evidence is None
        assert other.preflight_boot_id != engine.preflight_boot_id
        assert not other.preflight_valid("one-attempt", sending=True)
    finally:
        other.recorder.close()
        other.db.close()


async def test_read_only_preflight_does_not_create_dummy_intent_or_enable_orders(engine):
    prepare(engine)
    before = engine.db.conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    assert "tanpa signing" in await engine.diagnose_preflight()
    proof = engine.preflight_evidence
    assert proof and not proof.sendable
    assert engine.preflight_valid(proof.attempt)
    assert not engine.preflight_valid(proof.attempt, sending=True)
    assert before == engine.db.conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    assert not engine.account.sent and not engine.config.allow_live_orders
    assert not engine.account.rate.reservations


async def test_old_complete_history_allows_evaluation_but_is_not_dispatch_evidence(engine):
    from lldesk.diagnostics import observe

    prepare(engine)
    engine.last_reconcile_ok = time.monotonic() - 90
    assert engine.history_valid() and engine.account_state_valid()
    assert observe(engine)["technical_ready"]
    assert engine.preflight_evidence is None
    assert not engine.preflight_valid("unbound-old-audit", sending=True)


async def test_diagnostic_command_does_not_wait_on_network_in_coordinator(engine):
    started = asyncio.Event()

    async def wait():
        started.set()
        await asyncio.Event().wait()

    engine.diagnose_preflight = wait
    engine.db.command("DIAGNOSE_PREFLIGHT")
    async with asyncio.timeout(0.1):
        await engine.commands()
        await started.wait()
    assert engine.tasks and not engine.tasks[-1].done()
    engine.tasks[-1].cancel()
    await asyncio.gather(engine.tasks[-1], return_exceptions=True)
    assert not engine.account.sent


async def test_funds_change_during_audit_aborts_preflight(engine):
    prepare(engine)
    engine.account.current = replace(engine.account.current, equity=Decimal("49.44"))
    with engine.account.rate.reserve("preflight", 10):
        assert not await engine.prepare_preflight("funds-changed", sendable=True)
    assert engine.preflight_evidence is None and not engine.account.sent


async def test_audit_failure_and_extra_pagination_never_produce_evidence(engine):
    prepare(engine)

    async def too_many_pages(*args, **kwargs):
        for _ in range(23):
            engine.account.rate.claim("reconcile")
        return engine.account_snapshot

    engine.account.snapshot = too_many_pages
    with engine.account.rate.reserve("preflight", 10):
        assert not await engine.prepare_preflight("long-pagination", sendable=True)
    assert engine.reconcile_failed and engine.history_required
    assert not engine.preflight_evidence and not engine.account.sent
    assert len(engine.account.rate.calls) == 10 and not engine.account.rate.reservations
    assert engine.account.rate.can_claim("exit", 15)


def test_mixed_lease_preserves_actual_rolling_cost_and_releases_unused_send():
    rate = RateBudget()
    rate.claim("reconcile", 25)
    with rate.reserve("preflight", 10):
        assert len(rate.calls) + rate._held() == 35
        for _ in range(8):
            rate.claim("reconcile")
        assert len(rate.calls) == 33 and rate._held() == 2
        assert rate.can_claim("entry", 2) and rate.can_claim("probe_read", 14)
        assert rate.can_claim("exit", 15)
        assert not rate.can_claim("reconcile", 3)
    assert len(rate.calls) == 33 and not rate.reservations


async def test_entry_admission_counts_all_reads_still_to_run(engine):
    prepare(engine)
    engine.account.rate.claim("reconcile", 26)
    assert engine.dispatch_rate_ready()  # send-only check would give false optimism
    assert not engine.entry_rate_ready()  # audit8 + protection envelope14 >47
    engine.audit_scheduler.request_cost = 23
    engine.account.rate.calls.clear()
    engine.account.rate.claim("reconcile", 11)
    assert not engine.entry_rate_ready()


async def test_hung_full_audit_does_not_block_reduce_only_close(engine):
    exposed(engine)
    started = asyncio.Event()
    position_reader = engine.account.snapshot

    async def fast_position(*args, **kwargs):
        return await position_reader(1)

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    engine.account.snapshot = slow
    engine.account.probe_snapshot = slow
    engine.account.position_snapshot = fast_position
    # The accounting worker can hold its own lock and a normal batch; risk does not await it.
    task = asyncio.create_task(Engine.safe_reconcile(engine))
    await started.wait()
    engine.books["lighter"].updated_ns = time.monotonic_ns()
    engine.db.set_flag("close_requested", "1")
    engine.risk_task = asyncio.create_task(engine.risk_loop())
    try:
        async with asyncio.timeout(1):
            while not any(intent.kind == "EXIT" for intent in engine.account.sent):
                await asyncio.sleep(0.01)
    finally:
        await stop_worker(engine)
    assert any(intent.kind == "EXIT" for intent in engine.account.sent)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not engine.account.rate.reservations
