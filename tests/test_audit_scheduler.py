"""Real timing replay plus fail-closed atomic scheduling, without network/orders."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_probe_only import engine as engine_fixture
from test_probe_rate_budget import adapter as adapter_fixture
from test_protection_priority import exposed, stop_worker

from lldesk.account import RateBudget
from lldesk.audit_scheduler import AuditScheduler
from lldesk.engine import Engine

engine = engine_fixture
adapter = adapter_fixture


def test_real_eight_get_trace_has_freshness_margin_but_not_unlimited_entry_capacity(monkeypatch):
    rows = json.loads(
        (Path(__file__).parent / "fixtures/audit-eight-get-trace-20261005.json").read_text()
    )["records"]
    now = 100.0
    monkeypatch.setattr("lldesk.account.time.monotonic", lambda: now)
    rate = RateBudget()
    scheduler = AuditScheduler()
    previous_account = None
    previous_success = None
    peak = 0
    for i in range(90):
        now = max(now, scheduler.next_due)
        while not rate.can_claim("reconcile", 8):
            now = rate.calls[0] + 60.000001
        row = rows[i % len(rows)]
        start = now
        with rate.reserve("reconcile", 8):
            ordering = range(8) if i == 0 else [2, 3, 4, 5, 6, 1, 0, 7]
            for index in ordering:
                rate.claim("reconcile")
                peak = max(peak, len(rate.calls) + rate._held())
                now += row["request_durations_s"][index]
                if index == 0:
                    account = now
            now = max(now, start + row["duration_s"])
        if previous_account is not None:
            assert now - previous_account < 14.7
            assert now - previous_success < 15
        previous_account, previous_success = account, now
        scheduler.completed(now, now - account, now - start, 8)
    assert peak <= 40
    assert scheduler.duration_bound == 2
    # Five eight-read batches still overlap at some phases of any <15s cadence.
    assert peak > 33


async def test_cancelled_batch_releases_only_unconsumed_capacity():
    rate = RateBudget()
    rate.claim("reconcile", 32)
    started = asyncio.Event()

    async def batch():
        with rate.reserve("reconcile", 8):
            rate.claim("reconcile", 3)
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(batch())
    await started.wait()
    assert len(rate.calls) == 35 and rate._held() == 5
    assert not rate.can_claim("reconcile")
    # Urgent reads still leave the eight-slot transaction band; no normal work
    # can allocate another batch while the first is in flight.
    rate.claim("probe_read", 7)
    rate.claim("exit", 8)
    assert len(rate.calls) + rate._held() == 55
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not rate.reservations and len(rate.calls) == 50


def test_nested_batch_cannot_reuse_its_own_reserved_slots():
    rate = RateBudget()
    rate.claim("reconcile", 32)
    with rate.reserve("reconcile", 8):
        with pytest.raises(RuntimeError, match="reserve exhausted"):
            with rate.reserve("reconcile", 8):
                pytest.fail("nested allocation exceeded cap")
        for _ in range(8):
            rate.claim("reconcile")
            assert len(rate.calls) + rate._held() == 40
    assert len(rate.calls) == 40 and not rate.reservations


def test_claims_in_progress_retry_and_exit_count_at_exact_rolling_boundary(monkeypatch):
    now = 100.0
    monkeypatch.setattr("lldesk.account.time.monotonic", lambda: now)
    rate = RateBudget()
    rate.claim("reconcile", 33)
    # 14 is the combined envelope: entry nonce/send2 + priority reads12.
    assert rate.can_claim("entry", 2) and rate.can_claim("probe_read", 14)
    rate.claim("entry", 2)
    rate.claim("probe_read", 12)
    rate.claim("exit", 8)
    assert len(rate.calls) == 55
    with pytest.raises(RuntimeError):
        rate.claim("exit")  # An additional retry is not silently free.
    now = 159.999
    assert not rate.can_claim("exit")
    now = 160
    assert rate.can_claim("exit", 55) and not rate.calls


async def test_full_histories_are_repeated_but_account_timestamp_is_late(adapter, monkeypatch):
    value, history_calls = adapter
    now = 100.0
    monkeypatch.setattr("lldesk.account.time.time", lambda: now)
    monkeypatch.setattr("lldesk.account.time.monotonic", lambda: now)
    original = value.funding_history

    async def history(*args, **kwargs):
        nonlocal now
        now += 1
        return await original(*args, **kwargs)

    value.funding_history = history
    first = await value.snapshot(1)
    assert first.received_at == 100 and now == 101
    second = await value.snapshot(1)
    assert second.received_at == now == 102
    assert second.received_monotonic == 102
    assert history_calls == ["deposit", "deposit", "withdraw", "withdraw", "transfer"] * 2
    assert len(value.rate.calls) == 14  # snapshot7 plus separate trades1 per full cycle


async def test_changed_l1_identity_cannot_publish_cached_address_audit(adapter):
    value, _ = adapter
    await value.snapshot(1)
    original = value.account_api.account

    async def changed(**kwargs):
        response = await original(**kwargs)
        response.accounts[0].l1_address = "different-public-fixture-address"
        return response

    value.account_api.account = changed
    with pytest.raises(RuntimeError, match="identity changed"):
        await value.snapshot(1)
    assert value._funding_l1_address is None


async def test_two_audit_callers_share_one_network_operation(engine):
    engine.safe_reconcile = Engine.safe_reconcile.__get__(engine)
    started, finish = asyncio.Event(), asyncio.Event()

    async def reconcile():
        started.set()
        await finish.wait()

    engine.reconcile = AsyncMock(side_effect=reconcile)
    first = asyncio.create_task(engine.safe_reconcile())
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(engine.safe_reconcile())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(first, second)
    assert engine.reconcile.await_count == 1
    assert not engine.account.rate.reservations


async def test_slow_or_incomplete_bookkeeping_never_refreshes_audit(engine):
    engine.safe_reconcile = Engine.safe_reconcile.__get__(engine)
    prior = engine.last_reconcile_ok
    engine.account_snapshot = engine.account_snapshot.__class__(
        **{**engine.account_snapshot.__dict__, "received_at": time.time() - 14.45}
    )

    async def slow():
        await asyncio.sleep(1)

    engine.reconcile = AsyncMock(side_effect=slow)
    await engine.safe_reconcile()
    assert engine.reconcile_failed and engine.last_reconcile_ok == prior
    assert not engine.account.rate.reservations
    assert engine.entries_stopped and not engine.account.sent


async def test_risk_stop_does_not_wait_for_reserved_hung_audit(engine):
    engine.safe_reconcile = Engine.safe_reconcile.__get__(engine)
    exposed(engine)
    engine.account.rate.claim("reconcile", 32)
    started, release, protected = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await release.wait()

    async def stop(*args):
        assert engine.reconcile_lock.locked()
        engine.account.rate.claim("exit", 2)
        protected.set()
        return "isolated stop ACK"

    engine.reconcile = AsyncMock(side_effect=slow)
    engine.account.send_stop = AsyncMock(side_effect=stop)
    engine.reconcile_task = asyncio.create_task(engine.safe_reconcile())
    await asyncio.wait_for(started.wait(), 1)
    engine.risk_task = asyncio.create_task(engine.risk_loop())
    try:
        await asyncio.wait_for(protected.wait(), 1)
        assert (
            engine.account.send_stop.call_args.args[0].quantity
            == engine.account.current.position_qty
        )
        assert not engine.account.sent and engine.db.pending_fees() == 1
    finally:
        await stop_worker(engine)
        release.set()
        await engine.reconcile_task
    assert not engine.account.rate.reservations


async def test_cancelling_shared_waiter_does_not_cancel_audit_owner(engine):
    engine.safe_reconcile = Engine.safe_reconcile.__get__(engine)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await release.wait()

    engine.reconcile = AsyncMock(side_effect=slow)
    owner = asyncio.create_task(engine.safe_reconcile())
    await asyncio.wait_for(started.wait(), 1)
    waiter = asyncio.create_task(engine.safe_reconcile())
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert not owner.done() and engine.account.rate._held() == 8
    release.set()
    await owner
    assert engine.reconcile.await_count == 1 and not engine.account.rate.reservations


def test_long_pagination_changes_cost_not_audit_completeness_or_freshness():
    scheduler = AuditScheduler()
    scheduler.completed(100, 0.1, 6, 23)
    assert scheduler.request_cost == 23 and scheduler.next_due == 108.4
    rate = RateBudget()
    rate.claim("reconcile", 18)
    assert not rate.can_claim("reconcile", scheduler.request_cost)
    # 23-read full audits at a <15s cadence cannot fit a 40 normal cap.
    # Skip/blocked is honest; no checkpoint may pretend the tail was fetched.


def test_deadline_uses_monotonic_completion_and_never_resets_duration_high_water():
    scheduler = AuditScheduler()
    scheduler.completed(100, 0.1, 3, 8)
    scheduler.completed(200, 0.1, 0.2, 8)
    assert scheduler.duration_bound == 3 and scheduler.next_due == 211.4
    assert scheduler.timeout(14.6) == 0.05
