"""Unused-session revocation must preserve the ledger and retire its UUID."""

import importlib.util
import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from lldesk.config import Config, load_config
from lldesk.db import Database
from lldesk.engine import Engine
from lldesk.session import BoundedSession

spec = importlib.util.spec_from_file_location(
    "session_supervisor", Path(__file__).parents[1] / "scripts/supervise_bounded_session.py"
)
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


def test_unused_authorization_is_irrevocably_closed_without_touching_fee(tmp_path, monkeypatch):
    config = Config(
        mode="live_capable",
        account_index=751745,
        expected_account_type_code=0,
        expected_account_trading_mode=1,
        expected_margin_mode=0,
        expected_initial_margin_fraction=Decimal("100"),
        allow_live_orders=True,
        live_entry_scope="strategy",
        session_accept_reported_fees=True,
    )
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    db = Database(tmp_path / "state/desk.db")
    db.set_flag("session_used:old-session", "1")
    db.conn.execute(
        "INSERT INTO fills VALUES(?,?,?,?,?,?,?)",
        ("historical", 1, "0.00012", "86000", None, 123456, time.time()),
    )
    original = BoundedSession(db).authorize("unused", config, "49.430756")
    calls = []
    owner = Engine(config, tmp_path)
    monkeypatch.setattr(
        supervisor, "get_status", lambda: {"strategy_session": owner.session.read()}
    )

    def process(kind):
        calls.append(kind)
        owner.revoke_session_permission("entry dihentikan melalui perintah")

    monkeypatch.setattr(supervisor, "command", process)
    supervisor.revoke_unused("unused", "coverage cannot recover before preparation deadline")
    current = json.loads(db.flag("strategy_session"))
    assert current["status"] == "closed" and current["started_at"] is None
    assert current["attempts"] == [] and current["deadline_epoch"] is None
    assert current["initial_fingerprint"] == original["initial_fingerprint"]
    assert db.flag("session_used:unused") == db.flag("session_used:old-session") == "1"
    assert db.conn.execute("SELECT fee FROM fills").fetchone()[0] is None
    assert not load_config(path).allow_live_orders
    assert not load_config(path).session_accept_reported_fees
    assert calls == ["STOP_ENTRIES"]
    with pytest.raises(ValueError, match="already used"):
        BoundedSession(db).authorize("unused", config, "49.430756")
    db.close()
    owner.db.close()


@pytest.mark.parametrize("kind", ["PROBE_ONCE", "CLOSE_POSITION", "CANCEL_ENTRIES"])
def test_supervisor_has_no_direct_probe_or_order_command(kind):
    with pytest.raises(AssertionError):
        supervisor.command(kind)
