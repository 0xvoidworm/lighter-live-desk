"""Activation admission: account 77, fake exchange, production guards, no signer/network."""

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_bounded_session import authorize
from test_probe_only import engine as engine_fixture

from lldesk.api import create_app
from lldesk.config import load_config
from lldesk.engine import Engine
from lldesk.models import EngineState, Stamp
from lldesk.source_time import ClockEvidence

engine = engine_fixture


def source_quote(engine, *, future=False):
    stamp = Stamp(exchange_ms=time.time_ns() // 1_000_000 - (0 if future else 300))
    engine.books["binance"].stamp = stamp
    engine.books["binance"].updated_ns = stamp.monotonic_ns
    engine.fx_at = time.monotonic()


def prepared(engine):
    authorize(engine, active=False)
    record = engine.session.read()
    record["id"] = str(uuid.uuid4())
    engine.session.write(record)
    engine.loaded_session_permission_id = record["id"]
    del engine.data_valid
    del engine.source_freshness
    assert engine.data_valid.__func__ is Engine.data_valid
    anchor = Stamp()
    engine.source_clock.evidence = ClockEvidence(
        anchor, -50, 50, anchor.receipt_ns // 1_000_000, 100, {"synchronized": True}
    )
    source_quote(engine)
    return record


def binding(record):
    return {
        "request_id": str(uuid.uuid4()),
        "session_id": record["id"],
        "account_index": record["account_index"],
        "parameter_hash": record["parameter_hash"],
        "authorization_expires_epoch": record["authorization_expires_epoch"],
    }


def request(engine, record=None):
    value = engine.activation.submit(binding(record or prepared(engine)))
    engine.accept_activation_request(value["command_id"])
    return value


def tick(engine):
    engine.activation_next_tick = 0
    engine.activation_tick()


async def audit(engine):
    tick(engine)
    assert engine.activation_audit_task is not None
    await engine.activation_audit_task
    assert engine.preflight_evidence is not None


def no_entry(engine):
    assert engine.entries_stopped and not engine.entry_tasks and not engine.account.sent
    assert not engine.db.unresolved() and engine.session.read()["attempts"] == []
    assert engine.session.read()["started_at"] is None


async def test_ready_display_then_clock_rejects_request_processing(engine):
    initial = prepared(engine)
    assert engine.data_valid() and not engine.live_blockers()
    source_quote(engine, future=True)
    value = request(engine, initial)
    samples = engine.strategy.samples
    tick(engine)
    engine.decide()
    no_entry(engine)
    assert engine.activation.read(value["request_id"])["status"] == "WAITING"
    assessment = engine.activation.read(value["request_id"])["assessment"]
    assert (
        assessment["data_guard"]["source_freshness"]["reason"]
        == "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN"
    )
    assert not assessment["data_guard"]["passed"]
    assert assessment["preflight"]["executed"] is False
    assert not engine.account.rate.calls
    assert engine.strategy.samples is samples
    assert (
        engine.session.read()["authorization_expires_epoch"]
        == initial["authorization_expires_epoch"]
    )


async def test_temporary_block_then_atomic_once_activation_and_new_entry_audit(engine):
    initial = prepared(engine)
    source_quote(engine, future=True)
    value = request(engine, initial)
    tick(engine)
    no_entry(engine)
    source_quote(engine)
    await audit(engine)
    old_proof = engine.preflight_evidence
    no_entry(engine)
    tick(engine)
    current = engine.session.read()
    assert current["status"] == "active" and engine.state == EngineState.LIVE
    assert current["deadline_epoch"] - current["started_at"] == 1800
    assert current["attempts"] == [] and not old_proof.sendable
    stored = engine.activation.read(value["request_id"])
    assert stored["status"] == "ACTIVATED" and stored["assessment"]["preflight"]["passed"]
    assert stored["assessment"]["command_id"] == value["command_id"]
    assert stored["assessment"]["quotes"]["binance"]["source_ms"] is not None
    assert engine.activation.submit({k: value[k] for k in binding(initial)}) == stored
    tick(engine)
    assert engine.session.read() == current
    await engine.enter("buy")  # Fake order transport; real fresh preflight/dispatch path.
    assert len(engine.account.sent) == 1
    assert engine.last_preflight_diagnostic["attempt"] != old_proof.attempt
    assert engine.last_preflight_diagnostic["sendable"]
    assert engine.preflight_evidence is None  # Consumed at dispatch, never reusable.
    assert len(engine.session.read()["attempts"]) == 1


@pytest.mark.parametrize("kind", ["STOP_ENTRIES", "CANCEL_ENTRIES", "CLOSE_POSITION"])
async def test_control_cancellation_closes_unused_request_and_never_reactivates(engine, kind):
    value = request(engine)
    engine.db.command(kind)
    await engine.commands()
    assert engine.activation.read(value["request_id"])["status"] == "CANCELLED"
    transition = engine.activation.read(value["request_id"])["control_transition"]
    assert transition["readiness_skipped"]
    assert transition["assessment_role"] == "last_readiness_only"
    assert transition["command_id"] == value["command_id"]
    assert engine.session.read()["status"] == "closed"
    assert not engine.config.allow_live_orders and engine.loaded_session_permission_id is None
    tick(engine)
    assert not engine.account.sent and engine.session.read()["attempts"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "expiry",
        "mono_expiry",
        "revoke",
        "orders_off",
        "parameters",
        "identity",
        "incident",
        "journal",
        "halted",
    ],
)
async def test_terminal_binding_and_incident_fail_closed(engine, fault):
    value = request(engine)
    if fault == "expiry":
        current = engine.session.read()
        current["authorization_expires_epoch"] = time.time() - 1
        engine.session.write(current)
    elif fault == "mono_expiry":
        engine.activation_deadline = time.monotonic() - 1
    elif fault == "revoke":
        engine.session.revoke("external explicit revocation")
    elif fault == "orders_off":
        off = engine.config.model_copy(update={"allow_live_orders": False})
        (engine.root / "config.json").write_text(off.model_dump_json())
    elif fault == "parameters":
        engine.config = engine.config.model_copy(update={"max_hold_seconds": 170})
    elif fault == "identity":
        engine.config = engine.config.model_copy(update={"account_index": 88})
    elif fault == "incident":
        engine.db.set_flag("account_incident_pending", "1")
    elif fault == "journal":
        engine.journal_ok = False
    else:
        engine.state = EngineState.HALTED
    tick(engine)
    assert engine.activation.read(value["request_id"])["status"] == "REJECTED"
    assert engine.session.read()["status"] == "closed" and not engine.config.allow_live_orders
    assert not engine.account.sent and engine.session.read()["attempts"] == []


async def test_one_pending_idempotency_and_conflicting_binding(engine):
    initial = prepared(engine)
    b = binding(initial)
    first = engine.activation.submit(b)
    assert engine.activation.submit(b) == first
    with pytest.raises(ValueError, match="different authorization"):
        engine.activation.submit(dict(b, account_index=88))
    with pytest.raises(ValueError, match="one pending"):
        engine.activation.submit(binding(initial))
    assert (
        engine.db.conn.execute(
            "SELECT COUNT(*) FROM commands WHERE kind='START_WHEN_READY'"
        ).fetchone()[0]
        == 1
    )
    engine.accept_activation_request(first["command_id"])
    deadline = engine.activation_deadline
    engine.accept_activation_request(first["command_id"])
    assert engine.activation_deadline == deadline
    assert "diblok" in engine.start_session()
    no_entry(engine)


async def test_expired_preflight_and_budget_do_not_authorize_or_poll_each_tick(engine):
    value = request(engine)
    await audit(engine)
    engine.preflight_evidence = replace(engine.preflight_evidence, expires=time.monotonic() - 1)
    tick(engine)
    no_entry(engine)
    assert engine.activation_audit_task is None
    engine.account.rate.claim("reconcile", cost=35)
    for _ in range(40):
        tick(engine)
    assert engine.activation.read(value["request_id"])["status"] == "WAITING"
    assert not engine.account.rate.reservations and not engine.activation_audit_task
    no_entry(engine)


@pytest.mark.parametrize("queued", [True, False])
async def test_restart_cancels_pending_request_without_permission_or_deadline_restore(
    engine, queued
):
    initial = prepared(engine)
    value = engine.activation.submit(binding(initial))
    if not queued:
        engine.accept_activation_request(value["command_id"])
    restarted = Engine(engine.config, engine.root)
    try:
        restarted.exclusive()
        restarted.abort_restored_activation()
        assert restarted.pending_activation_id is None and restarted.entries_stopped
        assert (
            restarted.loaded_session_permission_id is None
            and not restarted.config.allow_live_orders
        )
        assert restarted.activation.read(value["request_id"])["status"] == "CANCELLED"
        assert restarted.session.read()["status"] == "closed"
        assert (
            restarted.session.read()["authorization_expires_epoch"]
            == initial["authorization_expires_epoch"]
        )
        assert not restarted.session.read()["attempts"]
    finally:
        restarted.lock_file.close()
        restarted.recorder.close()
        restarted.db.close()


async def test_crash_before_commit_rolls_back_activation_and_request(engine, monkeypatch):
    value = request(engine)
    await audit(engine)
    original = engine.db.event

    def crash(kind, body):
        if kind == "STRATEGY_SESSION_STARTED":
            raise RuntimeError("simulated crash before commit")
        return original(kind, body)

    monkeypatch.setattr(engine.db, "event", crash)
    with pytest.raises(RuntimeError, match="before commit"):
        tick(engine)
    assert engine.session.read()["status"] == "authorized"
    assert not engine.db.flag("session_used:" + value["session_id"])
    assert engine.activation.read(value["request_id"])["status"] == "WAITING"
    no_entry(engine)


async def test_crash_after_commit_recovery_keeps_deadline_and_does_not_activate_twice(
    engine, monkeypatch
):
    value = request(engine)
    await audit(engine)
    monkeypatch.setattr(
        engine, "adopt_active_session", Mock(side_effect=RuntimeError("after commit"))
    )
    with pytest.raises(RuntimeError, match="after commit"):
        tick(engine)
    current = engine.session.read()
    assert (
        current["status"] == "active"
        and engine.activation.read(value["request_id"])["status"] == "ACTIVATED"
    )
    restarted = Engine(engine.config, engine.root)
    try:
        assert restarted.session.read()["status"] == "ending"
        assert restarted.session.read()["deadline_epoch"] == current["deadline_epoch"]
        assert restarted.entries_stopped and restarted.loaded_session_permission_id is None
        assert restarted.session.read()["attempts"] == []
        assert (
            restarted.db.conn.execute(
                "SELECT count(*) FROM events WHERE kind='STRATEGY_SESSION_STARTED'"
            ).fetchone()[0]
            == 1
        )
    finally:
        restarted.recorder.close()
        restarted.db.close()


async def test_cancel_queued_at_transaction_boundary_wins(engine, monkeypatch):
    value = request(engine)
    await audit(engine)
    original = engine.session.activate

    def queued_cancel(**kwargs):
        engine.db.command("STOP_ENTRIES")
        return original(**kwargs)

    monkeypatch.setattr(engine.session, "activate", queued_cancel)
    tick(engine)
    assert engine.activation.read(value["request_id"])["status"] == "REJECTED"
    assert engine.session.read()["status"] == "closed"
    assert not engine.account.sent


async def test_clock_change_at_atomic_boundary_defers_without_second_request(engine, monkeypatch):
    value = request(engine)
    await audit(engine)
    original = engine.session.activate

    def changed_clock(**kwargs):
        source_quote(engine, future=True)
        return original(**kwargs)

    monkeypatch.setattr(engine.session, "activate", changed_clock)
    tick(engine)
    no_entry(engine)
    assert engine.activation.read(value["request_id"])["status"] == "WAITING"
    source_quote(engine)
    monkeypatch.setattr(engine.session, "activate", original)
    tick(engine)
    assert engine.activation.read(value["request_id"])["status"] == "ACTIVATED"


async def test_real_data_valid_rejection_not_replaced_by_later_passed_assessment(engine):
    prepared(engine)
    engine.source_freshness = Mock(
        side_effect=[
            {
                "passed": False,
                "reason": "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN",
                "selected_proof_id": "first",
            },
            {
                "passed": True,
                "reason": "SOURCE_FRESH_WITH_CLOCK_BOUND",
                "selected_proof_id": "second",
            },
        ]
    )
    assert "diblok" in engine.start_session(command_id=987)
    body = json.loads(
        engine.db.conn.execute(
            "SELECT body FROM events WHERE kind='ACTIVATION_CONTROL_DECISION' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    )
    assert engine.data_valid.__func__ is Engine.data_valid
    assert body["data_guard"]["passed"] is False
    assert body["data_guard"]["source_freshness"]["selected_proof_id"] == "first"
    assert body["source_freshness"]["passed"] is True
    assert body["passed"] is False and body["result"] == "REJECTED"


async def test_skipped_checks_and_journal_volume(engine):
    value = request(engine)
    engine.last_decision = replace(engine.last_decision, basis_bps=None)
    for _ in range(100):
        tick(engine)
    stored = engine.activation.read(value["request_id"])
    assert not stored["assessment"]["data_guard_executed"]
    assert stored["assessment"]["data_guard"] is None
    assert stored["assessment"]["preflight"]["executed"] is False
    assert (
        engine.db.conn.execute(
            "SELECT count(*) FROM events WHERE kind='ACTIVATION_CONTROL_DECISION'"
        ).fetchone()[0]
        == 1
    )
    no_entry(engine)


async def test_api_ambiguous_response_same_id_reconciles_journal_without_new_command(engine):
    initial = prepared(engine)
    engine.db.save_status({"state": "READY"})
    b = binding(initial)
    with TestClient(create_app(engine.root), base_url="http://127.0.0.1") as client:
        headers = {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}
        assert client.post("/api/command/START_WHEN_READY", headers=headers).status_code == 422
        first = client.post("/api/command/START_WHEN_READY", headers=headers, json=b).json()
        # Simulate losing response: reconcile GET by the SAME client-persisted ID.
        found = client.get("/api/activation/" + b["request_id"]).json()
        repeated = client.post("/api/command/START_WHEN_READY", headers=headers, json=b).json()
        assert found["command_id"] == first["command_id"] == repeated["command_id"]
        assert repeated["state"] == "QUEUED" and engine.session.read()["status"] == "authorized"
        assert (
            client.post(
                "/api/command/START_WHEN_READY", headers=headers, json=dict(b, account_index=88)
            ).status_code
            == 409
        )
        assert (
            engine.db.conn.execute(
                "SELECT count(*) FROM commands WHERE kind='START_WHEN_READY'"
            ).fetchone()[0]
            == 1
        )
    assert not engine.account.sent


async def test_waiting_cancel_releases_audit_reservation(engine):
    value = request(engine)
    gate = asyncio.Event()

    async def slow(*, reserved=False):
        await gate.wait()

    engine.safe_reconcile = slow
    tick(engine)
    task = engine.activation_audit_task
    await asyncio.sleep(0)
    assert engine.preflight_in_progress
    engine.revoke_session_permission("stop during audit")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not engine.account.rate.reservations and not engine.preflight_in_progress
    assert engine.activation.read(value["request_id"])["status"] == "CANCELLED"
    assert not engine.account.sent


async def test_unrelated_active_session_is_not_changed_by_invalid_request(engine):
    current = prepared(engine)
    assert engine.start_session() == "sesi live dimulai"
    before = engine.session.read()
    bad = dict(current, id=str(uuid.uuid4()))
    value = engine.activation.submit(binding(bad))
    engine.accept_activation_request(value["command_id"])
    assert engine.activation.read(value["request_id"])["status"] == "REJECTED"
    assert engine.session.read() == before and not engine.entries_stopped
    assert engine.pending_activation_id is None and not engine.account.sent


async def test_terminal_config_change_preserves_user_edits_when_disarming(engine):
    value = request(engine)
    changed = engine.config.model_copy(update={"max_hold_seconds": 170})
    (engine.root / "config.json").write_text(changed.model_dump_json())
    tick(engine)
    assert engine.activation.read(value["request_id"])["status"] == "REJECTED"
    disk = load_config(engine.root / "config.json")
    assert disk.max_hold_seconds == 170 and not disk.allow_live_orders
    assert engine.session.read()["status"] == "closed"


async def test_incident_only_at_commit_boundary_is_terminal_not_forgotten(engine, monkeypatch):
    value = request(engine)
    await audit(engine)
    original = engine.session.activate

    def incident(**kwargs):
        engine.db.set_flag("account_incident_pending", "1")
        return original(**kwargs)

    monkeypatch.setattr(engine.session, "activate", incident)
    tick(engine)
    assert engine.activation.read(value["request_id"])["status"] == "REJECTED"
    engine.db.set_flag("account_incident_pending", "0")
    tick(engine)
    assert engine.session.read()["status"] == "closed" and engine.entries_stopped
    assert not engine.account.sent


async def test_actual_sqlite_lock_wait_cannot_make_expired_preflight_pass(engine, monkeypatch):
    value = request(engine)
    await audit(engine)
    original = engine.session.activate
    locked = threading.Event()

    def lock():
        db = sqlite3.connect(engine.root / "state/desk.db", isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(0.12)
            db.rollback()
        finally:
            db.close()

    def after_wait(**kwargs):
        engine.preflight_evidence = replace(
            engine.preflight_evidence, expires=time.monotonic() + 0.05
        )
        thread = threading.Thread(target=lock)
        thread.start()
        assert locked.wait(1)
        try:
            return original(**kwargs)
        finally:
            thread.join(1)
            assert not thread.is_alive()

    monkeypatch.setattr(engine.session, "activate", after_wait)
    tick(engine)
    stored = engine.activation.read(value["request_id"])
    assert stored["status"] == "WAITING" and engine.session.read()["status"] == "authorized"
    no_entry(engine)


async def test_missing_fee_and_cash_difference_remain_terminal(engine):
    value = request(engine)
    engine.db.set_flag("fee_report:1001", "{}")
    engine.account_snapshot = replace(
        engine.account_snapshot, equity=engine.account_snapshot.equity + 1
    )
    tick(engine)
    assert engine.activation.read(value["request_id"])["status"] == "REJECTED"
    assert engine.session.read()["status"] == "closed" and not engine.account.sent


async def test_closed_permission_cannot_be_reused_even_with_same_request_id(engine):
    value = request(engine)
    engine.revoke_session_permission("explicit cancellation")
    repeated = engine.activation.submit({k: value[k] for k in binding(engine.session.read())})
    assert repeated["status"] == "CANCELLED" and repeated["command_id"] == value["command_id"]
    engine.accept_activation_request(value["command_id"])
    tick(engine)
    assert engine.session.read()["status"] == "closed" and not engine.account.sent
    assert "diblok" in engine.start_session()
