"""Human UI control: all DB/config/accounts are isolated, no signer or exchange."""

import json
import threading
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from lldesk.api import create_app
from lldesk.config import Config, load_config
from lldesk.db import Database
from lldesk.operator_session import OPERATIONS, PROPOSAL


@pytest.fixture
def ui(tmp_path):
    config = Config(
        mode="live_capable",
        account_index=77,
        expected_account_type_code=0,
        expected_account_trading_mode=1,
        expected_margin_mode=0,
        expected_initial_margin_fraction="100",
        live_entry_scope="strategy",
        source_funding_spent_usd="50",
    )
    (tmp_path / "config.json").write_text(config.model_dump_json())
    db = Database(tmp_path / "state/desk.db")
    db.set_flag("strategy_session", json.dumps({"id": str(uuid.uuid4()), "status": "closed"}))
    db.record_flows((("fixture-funding", "49.430756"),))
    db.event(
        "NET_CASH_AUDIT",
        {
            "fingerprint": db.accounting_fingerprint(),
            "equity_after": "49.430756",
            "cash_change_usdc": "0",
        },
    )
    status = {
        "engine_pid": 98765,
        "connected": True,
        "entries_stopped": True,
        "allow_live_orders": False,
        "strategy_session": {"status": "closed"},
        "loaded_session_permission_id": None,
        "diagnostic_observation": {
            "observed_at": time.time(),
            "checks": dict.fromkeys(OPERATIONS, True),
            "technical_ready": False,
            "blockers": ["source_freshness"],
        },
        "account": {
            "equity": "49.430756",
            "components_complete": True,
            "component_ages_s": {"account": 0},
            "position_qty": "0.00000",
            "exchange_active_order_count": 0,
            "other_exchange_position_count": 0,
            "exchange_pending_count": 0,
        },
        "pending_orders": [],
    }
    db.save_status(status)
    app = create_app(tmp_path)
    reads = []

    async def fake_read(config):
        reads.append("GET")
        return SimpleNamespace(
            equity=Decimal("49.430756"), funding_flows=(("fixture-funding", "49.430756"),)
        )

    app.state.operator_session.account_reader = fake_read
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield SimpleNamespace(
            root=tmp_path, db=db, app=app, client=client, status=status, reads=reads
        )
    db.close()


def request(ui, request_id=None, **extra):
    body = {
        "request_id": request_id or str(uuid.uuid4()),
        "proposal": PROPOSAL,
        "consent": True,
    } | extra
    token = ui.client.get("/api/csrf").json()["token"]
    return ui.client.post("/api/session/start", json=body, headers={"X-CSRF-Token": token})


def tables(ui):
    return {
        name: ui.db.conn.execute(f"SELECT * FROM {name}").fetchall()
        for name in ["flags", "commands", "activation_requests", "intents", "fills", "events"]
    }


def test_get_preview_status_report_do_not_construct_writer_or_stage(ui):
    before = tables(ui)
    raw = (ui.root / "config.json").read_bytes()
    with patch("lldesk.api.Database", side_effect=AssertionError("write constructor forbidden")):
        assert ui.client.get("/api/status").status_code == 200
        assert ui.client.get("/api/session/preview").json()["can_request"]
        assert ui.client.get("/api/report").status_code == 200
    assert tables(ui) == before and (ui.root / "config.json").read_bytes() == raw
    assert not (ui.root / "coordination").exists() and not ui.reads


def test_csrf_localorigin_and_consent(ui):
    before = tables(ui)
    body = {"request_id": str(uuid.uuid4()), "proposal": PROPOSAL, "consent": True}
    assert ui.client.post("/api/session/start", json=body).status_code == 403
    token = ui.client.get("/api/csrf").json()["token"]
    assert (
        ui.client.post(
            "/api/session/start",
            json=body,
            headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
        ).status_code
        == 403
    )
    assert request(ui, consent=False).status_code == 409
    assert tables(ui) == before and not ui.reads


def test_explicit_click_queues_load_then_waiting_control_never_orders(ui):
    result = request(ui)
    assert result.status_code == 200
    assert result.json()["state"] == "QUEUED"
    assert ui.db.conn.execute("SELECT kind FROM commands ORDER BY id").fetchall() == [
        ("LOAD_SESSION_PERMISSION",),
        ("START_WHEN_READY",),
    ]
    session = json.loads(ui.db.flag("strategy_session"))
    assert session["status"] == "authorized" and session["started_at"] is None
    assert session["authorization_expires_epoch"] - session["authorized_at"] == 1200
    assert session["max_seconds"] == 1800 and session["max_attempts"] == 3
    assert not ui.db.unresolved() and not ui.db.conn.execute("SELECT * FROM fills").fetchall()
    assert len(ui.reads) == 1 and load_config(ui.root / "config.json").allow_live_orders


def test_same_request_response_loss_idempotent_and_different_click_rejected(ui):
    rid = str(uuid.uuid4())
    a = request(ui, rid)
    before = tables(ui)
    b = request(ui, rid)
    assert b.json() == a.json() and tables(ui) == before
    assert request(ui).status_code == 409 and len(ui.reads) == 1


def test_concurrent_doubleclick_same_uuid_creates_once(ui):
    rid = str(uuid.uuid4())
    results = []

    def click():
        results.append(request(ui, rid).status_code)

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200, 200] and len(ui.reads) == 1
    assert ui.db.conn.execute("SELECT count(*) FROM activation_requests").fetchone()[0] == 1


@pytest.mark.parametrize("state", ["active", "ending", "authorized"])
def test_nonclosed_or_expired_authorization_cannot_rearm(ui, state):
    ui.db.set_flag(
        "strategy_session",
        json.dumps({"status": state, "authorization_expires_epoch": time.time() - 1}),
    )
    assert request(ui).status_code == 409 and not ui.reads
    assert not load_config(ui.root / "config.json").allow_live_orders


def test_stale_account_and_parameter_mismatch_no_staging(ui):
    ui.status["diagnostic_observation"]["checks"]["account_fresh"] = False
    ui.db.save_status(ui.status)
    assert request(ui).status_code == 409 and not ui.reads
    ui.status["diagnostic_observation"]["checks"]["account_fresh"] = True
    ui.db.save_status(ui.status)
    config = load_config(ui.root / "config.json").model_copy(
        update={"max_entry_notional_usd": Decimal("20")}
    )
    (ui.root / "config.json").write_text(config.model_dump_json())
    assert request(ui).status_code == 409 and not ui.reads


def test_account_changed_after_get_recheck_rejects(ui):
    async def changed(config):
        ui.status["account"]["equity"] = "49.000000"
        changed_db = Database(ui.root / "state/desk.db")
        changed_db.save_status(ui.status)
        changed_db.close()
        return SimpleNamespace(
            equity=Decimal("49.430756"), funding_flows=(("fixture-funding", "49.430756"),)
        )

    ui.app.state.operator_session.account_reader = changed
    assert request(ui).status_code == 409
    assert not load_config(ui.root / "config.json").allow_live_orders
    assert json.loads(ui.db.flag("strategy_session"))["status"] == "closed"


def test_partial_config_failure_queues_owned_stop_not_start(ui):
    from lldesk.operator_session import atomic_json

    def broken(path, value):
        if path == ui.root / "config.json":
            raise OSError("fixture partial failure")
        return atomic_json(path, value)

    with patch("lldesk.operator_session.atomic_json", side_effect=broken):
        assert request(ui).status_code == 503
    assert ui.db.conn.execute("SELECT kind FROM commands").fetchall() == [("STOP_ENTRIES",)]
    assert not load_config(ui.root / "config.json").allow_live_orders
    assert ui.client.get("/api/session/preview").json()["request"]["state"] == "FAILED"
    assert (
        ui.client.get("/api/status").json()["strategy_session"]["status"] == "authorized"
    )  # authoritative admission, not older cached closed


def test_sdk_read_failure_no_authorization_and_owner_closed(ui):
    async def broken(config):
        raise RuntimeError("fixture GET failed")

    ui.app.state.operator_session.account_reader = broken
    assert request(ui).status_code == 503
    assert json.loads(ui.db.flag("strategy_session"))["status"] == "closed"
    owner = json.loads((ui.root / "coordination/owner.json").read_text())
    assert owner["status"] == "closed" and owner["cleanup"] == "no_permission_created"


def test_shared_ownership_conflict_no_mutation(ui):
    import fcntl

    coord = ui.root / "coordination"
    coord.mkdir()
    with (coord / "engine.lock").open("a+") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert request(ui).status_code == 409 and not ui.reads
    assert json.loads(ui.db.flag("strategy_session"))["status"] == "closed"


def test_source_binding_change_during_get_rejected(ui):
    async def changed(config):
        ui.app.state.operator_session.source_binding["strategy.py"] = "0" * 64
        return SimpleNamespace(
            equity=Decimal("49.430756"), funding_flows=(("fixture-funding", "49.430756"),)
        )

    ui.app.state.operator_session.account_reader = changed
    assert request(ui).status_code == 409
    assert json.loads(ui.db.flag("strategy_session"))["status"] == "closed"
    assert not load_config(ui.root / "config.json").allow_live_orders


def test_authorization_write_failure_rolls_back_before_config_or_commands(ui):
    from lldesk.session import BoundedSession

    original = BoundedSession.authorize

    def broken(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("injected precommit failure")

    before = tables(ui)
    with patch.object(BoundedSession, "authorize", broken):
        assert request(ui).status_code == 503
    assert tables(ui) == before and not load_config(ui.root / "config.json").allow_live_orders


def test_holder_keeps_real_flock_until_closed_flat_off_proof(ui):
    import fcntl

    result = request(ui).json()
    path = ui.root / "coordination/engine.lock"
    with path.open("a+") as contender:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    session = json.loads(ui.db.flag("strategy_session"))
    session["status"] = "closed"
    ui.db.set_flag("strategy_session", json.dumps(session))
    config = load_config(ui.root / "config.json").model_copy(
        update={"allow_live_orders": False, "session_accept_reported_fees": False}
    )
    (ui.root / "config.json").write_text(config.model_dump_json())
    ui.status["strategy_session"] = session
    ui.status["diagnostic_observation"]["observed_at"] = time.time()
    ui.db.save_status(ui.status)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        ui.status["diagnostic_observation"]["observed_at"] = time.time()
        ui.db.save_status(ui.status)  # isolated fake engine heartbeat, not a stale cached proof
        with path.open("a+") as contender:
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            break
    else:
        pytest.fail("holder did not release after clean proof")
    owner = json.loads((ui.root / "coordination/owner.json").read_text())
    assert owner["status"] == "closed" and owner["cleanup"] == "confirmed"
    assert session["id"] == result["session_id"]


def test_stop_queue_failure_does_not_drop_owned_lock_or_claim_clean(ui):
    import fcntl

    from lldesk.operator_session import atomic_json

    def broken_config(path, value):
        if path == ui.root / "config.json":
            raise OSError("injected config failure")
        return atomic_json(path, value)

    with (
        patch("lldesk.operator_session.atomic_json", side_effect=broken_config),
        patch.object(Database, "command", side_effect=RuntimeError("injected journal failure")),
    ):
        assert request(ui).status_code == 503
    lock = ui.root / "coordination/engine.lock"
    with lock.open("a+") as fd:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    owner = json.loads((ui.root / "coordination/owner.json").read_text())
    assert owner["status"] == "held" and "cleanup" not in owner
    assert json.loads(ui.db.flag("strategy_session"))["status"] == "authorized"


def test_empty_dashboard_load_never_initializes_live_state(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/status").json()["connected"] is False
        assert client.get("/api/session/preview").json()["can_request"] is False
    assert list(tmp_path.iterdir()) == []


def test_monitor_start_failure_retains_kernel_lock_and_requests_stop(ui):
    import fcntl

    real_start = threading.Thread.start

    def fault_start(thread):
        if thread.name == "ui-session-ownership":
            raise RuntimeError("fixture")
        return real_start(thread)

    with patch("lldesk.operator_session.threading.Thread.start", fault_start):
        assert request(ui).status_code == 503
    owner = json.loads((ui.root / "coordination/owner.json").read_text())
    assert owner["status"] == "held" and "cleanup" not in owner
    assert "unconfirmed" in owner["monitor_failure"]
    assert json.loads(ui.db.flag("strategy_session"))["status"] == "authorized"
    assert (
        ui.db.conn.execute("SELECT kind FROM commands ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "STOP_ENTRIES"
    )
    with (ui.root / "coordination/engine.lock").open("a+") as fd:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Restarted API returns the same stored failed request; never stages a new UUID.
    old = json.loads(ui.db.flag("ui_proposal:" + PROPOSAL))
    other = create_app(ui.root).state.operator_session
    assert other.begin(old["request_id"], PROPOSAL, True) == old
    ui.app.state.operator_session.shutdown()
    owner = json.loads((ui.root / "coordination/owner.json").read_text())
    assert owner["cleanup"].startswith("unconfirmed_api_shutdown")
    with (ui.root / "coordination/engine.lock").open("a+") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
