"""Control admission uses isolated account 77 and fake order transport only."""

import time
from collections import deque
from dataclasses import replace
from decimal import Decimal

import pytest
from test_bounded_session import authorize
from test_probe_only import engine as engine_fixture

from lldesk.models import EngineState

engine = engine_fixture


def prepared_off(engine):
    authorize(engine, active=False)
    candidate = engine.config
    engine.config = candidate.model_copy(
        update={
            "allow_live_orders": False,
            "session_accept_reported_fees": False,
        }
    )
    engine.loaded_session_permission_id = None
    engine.state = EngineState.IDLE
    return candidate


async def test_load_preserves_coverage_account_stamps_and_does_not_activate(engine):
    prepared_off(engine)
    now = time.monotonic()
    samples = deque([(now - 700 + n, Decimal(n), Decimal(2), Decimal(2)) for n in range(600)])
    engine.strategy.samples = samples
    engine.strategy.last_sample_at = now
    initial = engine.session.read()
    received = engine.account_snapshot.received_monotonic
    assert "permission dimuat" in engine.load_session_permission()
    assert engine.strategy.samples is samples and len(samples) == 600
    assert engine.strategy.last_sample_at == now
    assert engine.account_snapshot.received_monotonic == received
    assert engine.session.read() == initial
    assert engine.entries_stopped and engine.state == EngineState.READY
    engine.decide()  # Fixture has a valid BUY signal; prepare cannot dispatch it.
    assert not engine.entry_tasks and not engine.account.sent and not engine.db.unresolved()
    assert "sudah dimuat" in engine.load_session_permission()
    assert engine.session.read() == initial and engine.session_end == 0


async def test_explicit_start_after_reload_uses_fake_order_transport(engine):
    prepared_off(engine)
    assert "izin sesi belum dimuat" in engine.start_session()
    engine.load_session_permission()
    assert engine.start_session() == "sesi live dimulai"
    before = engine.session.read()
    await engine.enter("buy")
    assert len(engine.account.sent) == 1 and engine.account.sent[0].kind == "ENTRY"
    after = engine.session.read()
    assert after["deadline_epoch"] == before["deadline_epoch"]
    assert len(after["attempts"]) == 1
    assert "permission diblok" in engine.load_session_permission()
    assert engine.session.read() == after


@pytest.mark.parametrize(
    "invalid", ["expired", "revoked", "revoked_zero", "closed", "legacy_no_expiry"]
)
async def test_expired_revoked_closed_or_legacy_authorization_cannot_load(engine, invalid):
    candidate = prepared_off(engine)
    record = engine.session.read()
    if invalid == "expired":
        record["authorization_expires_epoch"] = time.time() - 1
    elif invalid == "revoked":
        record["revoked_at"] = time.time()
    elif invalid == "revoked_zero":
        record["revoked_at"] = 0
    elif invalid == "closed":
        record.update(status="closed", ended_at=time.time())
    else:
        record.pop("authorization_expires_epoch")
    engine.session.write(record)
    (engine.root / "config.json").write_text(candidate.model_dump_json())
    assert "permission diblok" in engine.load_session_permission()
    assert "diblok" in engine.start_session()
    assert not engine.config.allow_live_orders and not engine.account.sent
    assert engine.session.read()["attempts"] == []


async def test_expiry_after_load_is_revoked_without_discarding_samples(engine):
    prepared_off(engine)
    engine.load_session_permission()
    samples = engine.strategy.samples
    record = engine.session.read()
    record["authorization_expires_epoch"] = time.time() - 1
    engine.session.write(record)
    engine.session_tick()
    assert engine.session.read()["status"] == "closed"
    assert engine.db.flag("session_used:one-off-test") == "1"
    assert not engine.config.allow_live_orders and engine.loaded_session_permission_id is None
    assert engine.strategy.samples is samples and not engine.account.sent


async def test_reload_off_revokes_active_session_without_resetting_deadline_or_quota(engine):
    authorize(engine)
    client = engine.db.next_client_id()
    engine.db.intent(client, "ENTRY", "buy", "0.00013", "83000.1")
    assert engine.session.consume(client)
    before = engine.session.read()
    mono_deadline = engine.session_end
    off = engine.config.model_copy(update={"allow_live_orders": False})
    (engine.root / "config.json").write_text(off.model_dump_json())
    assert "dicabut" in engine.load_session_permission()
    after = engine.session.read()
    assert after["status"] == "ending"  # Risk/cleanup owns completion, not reload.
    assert after["attempts"] == before["attempts"]
    assert after["deadline_epoch"] == before["deadline_epoch"]
    assert engine.session_end == mono_deadline
    assert engine.db.flag("close_requested") == "1"
    assert not engine.config.allow_live_orders
    assert not engine.config.session_accept_reported_fees


@pytest.mark.parametrize("change", ["risk", "identity", "equity", "stale", "fee"])
async def test_reload_cannot_change_risk_or_bypass_accounting_guards(engine, change):
    candidate = prepared_off(engine)
    if change == "risk":
        candidate = candidate.model_copy(update={"max_hold_seconds": 170})
    elif change == "identity":
        candidate = candidate.model_copy(update={"account_index": 78})
    elif change == "equity":
        engine.account_snapshot = replace(engine.account_snapshot, equity=Decimal("49.42"))
    elif change == "stale":
        engine.account_snapshot = replace(
            engine.account_snapshot,
            received_monotonic=time.monotonic() - 16,
        )
    else:
        engine.db.set_flag("fee_report:1001", "{}")
    (engine.root / "config.json").write_text(candidate.model_dump_json())
    assert "permission diblok" in engine.load_session_permission()
    assert not engine.config.allow_live_orders and not engine.account.sent


async def test_activation_rechecks_record_identity_inside_transaction(engine):
    authorize(engine, active=False)
    record = engine.session.read()
    with pytest.raises(ValueError, match="changed before activation"):
        engine.session.activate(
            expected_id="different-uuid", expected_hash=record["parameter_hash"]
        )
    assert engine.session.read() == record
    assert not engine.db.flag("session_used:one-off-test") and not engine.account.sent


async def test_real_restart_does_not_restore_loaded_permission(engine):
    from lldesk.engine import Engine

    authorize(engine, active=False)
    restarted = Engine(engine.config, engine.root)
    try:
        assert restarted.loaded_session_permission_id is None
        assert restarted.entries_stopped and restarted.session_end == 0
        assert restarted.session.read()["status"] == "authorized"
        assert "diblok" in restarted.start_session()
        assert restarted.session.read()["attempts"] == []
    finally:
        restarted.db.close()
