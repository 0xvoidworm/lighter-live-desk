from __future__ import annotations

import asyncio
import sqlite3
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from lldesk.account import AccountSnapshot, LighterAccount, RateBudget
from lldesk.api import create_app
from lldesk.config import Config
from lldesk.db import Database
from lldesk.engine import Engine
from lldesk.market import Book, Feeds, GapError
from lldesk.models import EngineState, OrderIntent, Quote, Stamp
from lldesk.risk import EquityBaseline, size_entry
from lldesk.strategy import Decision, DeviationStrategy


def test_binance_duplicate_bridge_and_gap() -> None:
    book = Book("binance")
    book.binance_snapshot(
        {"lastUpdateId": 100, "bids": [["100", "2"]], "asks": [["101", "1"]]}, Stamp()
    )
    book.binance_delta(
        {"U": 99, "u": 102, "pu": 98, "b": [["100", "0"], ["99", "3"]], "a": []}, Stamp()
    )
    assert book.bridged and book.sequence == 102 and Decimal(100) not in book.bids
    book.binance_delta({"U": 99, "u": 102, "pu": 98, "b": [], "a": []}, Stamp())
    assert book.sequence == 102
    with pytest.raises(GapError):
        book.binance_delta({"U": 103, "u": 103, "pu": 101, "b": [], "a": []}, Stamp())
    assert not book.valid and book.gaps == 1


def test_lighter_nonce_gap_ignores_noncontinuous_offset() -> None:
    book = Book("lighter")
    book.lighter_snapshot(
        {
            "order_book": {
                "nonce": 200,
                "offset": 10,
                "bids": [{"price": "100", "size": "1"}],
                "asks": [{"price": "101", "size": "1"}],
            }
        },
        Stamp(),
    )
    book.lighter_delta(
        {
            "order_book": {
                "begin_nonce": 200,
                "nonce": 210,
                "offset": 999999,
                "bids": [],
                "asks": [],
            }
        },
        Stamp(),
    )
    assert book.valid and book.sequence == 210
    with pytest.raises(GapError):
        book.lighter_delta(
            {"order_book": {"begin_nonce": 209, "nonce": 220, "bids": [], "asks": []}}, Stamp()
        )
    assert not book.valid


@pytest.mark.asyncio
async def test_shared_market_queue_overflow_invalidates_both_feeds() -> None:
    queue = asyncio.Queue(maxsize=2)
    feeds = Feeds(1, queue)
    await feeds.emit("binance", "depthUpdate", {"u": 1})
    await feeds.emit("lighter", "order_book", {"nonce": 1})
    with pytest.raises(GapError, match="resync"):
        await feeds.emit("lighter", "order_book", {"nonce": 2})
    affected = {queue.get_nowait().venue, queue.get_nowait().venue}
    assert affected == {"binance", "lighter"}


def test_entry_size_does_not_expand_to_market_minimum() -> None:
    cfg = Config()
    order = size_entry(
        cfg,
        Decimal(50),
        Decimal(0),
        Decimal(0),
        Decimal("83000"),
        "buy",
        Decimal("0.00007"),
        Decimal(10),
        5,
        1,
        Decimal(35),
        Decimal(0),
    )
    assert order.notional <= 25 and order.planned_risk <= Decimal("0.50")
    assert order.quantity % Decimal("0.00001") == 0
    with pytest.raises(ValueError, match="minimum"):
        size_entry(
            cfg,
            Decimal(20),
            Decimal(0),
            Decimal(0),
            Decimal("83000"),
            "buy",
            Decimal("0.00007"),
            Decimal(20),
            5,
            1,
            Decimal(500),
            Decimal(0),
        )
    with pytest.raises(ValueError, match="capacity"):
        size_entry(
            cfg,
            Decimal(50),
            Decimal(50),
            Decimal(0),
            Decimal("83000"),
            "buy",
            Decimal("0.00007"),
            Decimal(10),
            5,
            1,
            Decimal(35),
            Decimal(0),
        )


def test_equity_loss_survives_flow_and_day_change() -> None:
    baseline = EquityBaseline(Decimal(50), Decimal(0), Decimal(46), Decimal(0), "2026-09-30")
    assert baseline.total_loss(Decimal(45), Decimal(0)) == 5
    assert baseline.daily_loss(Decimal(40), Decimal(0)) == 6
    # A new deposit cannot hide a prior loss.
    assert baseline.total_loss(Decimal(55), Decimal(10)) == 5
    assert baseline.check(Config(), Decimal(40), Decimal(0)) == ["DAILY_LOSS"]


def test_funding_flow_ledger_survives_restart_and_deduplicates(tmp_path) -> None:
    path = tmp_path / "state" / "desk.db"
    db = Database(path)
    assert db.record_flows((("deposit:1", "50"),)) == "50"
    db.close()
    db = Database(path)
    assert db.record_flows((("deposit:1", "50"), ("withdraw:2", "-5"))) == "45"
    baseline = EquityBaseline(Decimal(50), Decimal(50), Decimal(50), Decimal(50), "day")
    assert baseline.total_loss(Decimal(45), Decimal(45)) == 0
    assert db.record_flows((("deposit:3", "10"),)) == "55"
    assert baseline.total_loss(Decimal(50), Decimal(55)) == 5
    with pytest.raises(ValueError, match="identity changed"):
        db.record_flows((("deposit:1", "49"),))
    db.close()


def test_daily_equity_stop_is_persistent_after_restart(tmp_path) -> None:
    def account(equity: str) -> AccountSnapshot:
        return AccountSnapshot(
            Decimal(equity),
            Decimal(equity),
            0,
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            (),
            (),
            (),
            time.time(),
        )

    first = Engine(Config(), tmp_path)
    first._update_baseline(account("50"))
    first._update_baseline(account("44"))
    assert first.entries_stopped and first.db.flag("daily_stop_day")
    first.db.close()
    first.recorder.close()
    resumed = Engine(Config(), tmp_path)
    assert any("harian" in reason for reason in resumed.live_blockers())
    resumed.db.close()
    resumed.recorder.close()


def test_durable_intent_deduplicated_fill_and_consistent_backup(tmp_path) -> None:
    path = tmp_path / "state" / "desk.db"
    db = Database(path)
    db.intent(1, "ENTRY", "buy", "0.0002", "83000")
    db.close()  # crash/restart boundary after intent commit
    db = Database(path)
    assert db.unresolved()[0]["state"] == "INTENT"
    db.intent(2, "ENTRY", "buy", "0.0002", "83000")
    db.order_state(2, "SENT", "tx-2")
    db.close()  # crash after send, before the fill is stored
    db = Database(path)
    assert [x["state"] for x in db.unresolved()] == ["INTENT", "SENT"]
    assert db.add_fill("trade-10", 1, "0.0001", "83000", "0", 123)
    assert not db.add_fill("trade-10", 1, "0.0001", "83000", "0", 123)
    target = tmp_path / "copy.db"
    db.backup(target)
    db.close()
    with sqlite3.connect(target) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_second_engine_cannot_take_order_lock(tmp_path) -> None:
    a, b = Engine(Config(), tmp_path), Engine(Config(), tmp_path)
    try:
        a.exclusive()
        with pytest.raises(RuntimeError, match="engine lain"):
            b.exclusive()
    finally:
        if a.lock_file:
            a.lock_file.close()
        a.db.close()
        b.db.close()
        a.recorder.close()
        b.recorder.close()


def test_rate_budget_preserves_exit_and_reconcile_capacity() -> None:
    budget = RateBudget()
    for _ in range(35):
        budget.claim("entry")
    with pytest.raises(RuntimeError):
        budget.claim("entry")
    for _ in range(20):
        budget.claim("exit")
    with pytest.raises(RuntimeError):
        budget.claim("reconcile")


def test_strategy_needs_real_coverage_and_variation(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("lldesk.strategy.time.monotonic", lambda: clock[0])
    strategy = DeviationStrategy(600)
    lighter = Quote(Decimal(100), Decimal("100.1"), Decimal(1), Decimal(1), Stamp())
    binance = Quote(Decimal(100), Decimal("100.1"), Decimal(1), Decimal(1), Stamp())
    for second in range(1, 603):
        clock[0] = float(second)
        decision = strategy.evaluate(lighter, binance, True, Decimal(1))
    assert decision.side is None and "VARIASI_NOL" in decision.reasons
    clock[0] = 1000
    decision = strategy.evaluate(lighter, binance, True, Decimal(1))
    assert decision.side is None and "COVERAGE_DATA_KURANG" in decision.reasons


def test_api_rejects_cross_site_and_requires_csrf(tmp_path) -> None:
    db = Database(tmp_path / "state" / "desk.db")
    db.save_status({"state": "READY"})
    db.close()
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/command/STOP_ENTRIES").status_code == 403
        token = client.get("/api/csrf").json()["token"]
        assert (
            client.post(
                "/api/command/STOP_ENTRIES",
                headers={"origin": "https://evil.example", "x-csrf-token": token},
            ).status_code
            == 403
        )
        ok = client.post("/api/command/STOP_ENTRIES", headers={"x-csrf-token": token})
        assert ok.status_code == 200 and ok.json()["state"] == "QUEUED"


def test_dashboard_api_does_not_expose_local_secret_file(tmp_path) -> None:
    sentinel = "TEST_KEY_NEVER_EXPOSE_92f3"
    (tmp_path / "secrets.json").write_text(
        '{"api_private_key":"' + sentinel + '","api_key_index":1}'
    )
    db = Database(tmp_path / "state" / "desk.db")
    db.save_status({"state": "READY", "reason": "test fixture"})
    db.close()
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        assert sentinel not in client.get("/api/status").text
        assert sentinel not in client.get("/api/report").text


@pytest.mark.asyncio
async def test_timeout_after_durable_send_freezes_entries(tmp_path, monkeypatch) -> None:
    engine = Engine(
        Config(
            mode="live_capable",
            account_index=77,
            expected_account_type_code=0,
            allow_live_orders=True,
        ),
        tmp_path,
    )
    engine.state = "LIVE"
    engine.session_end = time.monotonic() + 1800
    engine.market = {
        "market_id": 1,
        "min_base_amount": "0.00007",
        "min_quote_amount": "10",
        "size_decimals": 5,
        "price_decimals": 1,
    }
    engine.books["lighter"].lighter_snapshot(
        {
            "order_book": {
                "nonce": 1,
                "bids": [{"price": "83000", "size": "2"}],
                "asks": [{"price": "83000.1", "size": "2"}],
            }
        },
        Stamp(),
    )
    engine.account_snapshot = AccountSnapshot(
        Decimal(50),
        Decimal(50),
        0,
        Decimal(0),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        (),
        (),
        (),
        time.time(),
    )

    class LostAck:
        calls = 0

        async def send_ioc(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("network lost after possible accept")

    lost = LostAck()
    engine.account = lost  # type: ignore[assignment]
    monkeypatch.setattr(engine, "data_valid", lambda: True)
    engine.last_decision = Decision("buy", Decimal(30), Decimal(0), Decimal(1), ())
    await engine.enter("buy")
    assert engine.state == EngineState.PAUSED and engine.entries_stopped
    assert engine.db.unresolved()[0]["state"] == "UNKNOWN"
    engine.state = EngineState.LIVE  # even an incorrect retry request cannot duplicate it
    await engine.enter("buy")
    assert lost.calls == 1
    engine.db.close()
    engine.recorder.close()


@pytest.mark.asyncio
async def test_journal_failure_prevents_send(tmp_path, monkeypatch) -> None:
    engine = Engine(
        Config(
            mode="live_capable",
            account_index=77,
            expected_account_type_code=0,
            allow_live_orders=True,
        ),
        tmp_path,
    )
    engine.state = "LIVE"
    engine.session_end = time.monotonic() + 1800
    engine.market = {
        "market_id": 1,
        "min_base_amount": "0.00007",
        "min_quote_amount": "10",
        "size_decimals": 5,
        "price_decimals": 1,
    }
    engine.books["lighter"].lighter_snapshot(
        {
            "order_book": {
                "nonce": 1,
                "bids": [{"price": "83000", "size": "2"}],
                "asks": [{"price": "83000.1", "size": "2"}],
            }
        },
        Stamp(),
    )
    engine.account_snapshot = AccountSnapshot(
        Decimal(50),
        Decimal(50),
        0,
        Decimal(0),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        (),
        (),
        (),
        time.time(),
    )

    class NeverSend:
        called = False

        async def send_ioc(self, *args, **kwargs):
            self.called = True

    account = NeverSend()
    engine.account = account  # type: ignore[assignment]
    monkeypatch.setattr(engine, "data_valid", lambda: True)
    engine.last_decision = Decision("buy", Decimal(30), Decimal(0), Decimal(1), ())
    monkeypatch.setattr(
        engine.db,
        "intent",
        lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )
    await engine.enter("buy")
    assert engine.state == EngineState.PAUSED and not account.called
    engine.db.close()
    engine.recorder.close()


@pytest.mark.asyncio
async def test_partial_fill_then_cancel_still_protects_real_position(tmp_path) -> None:
    engine = Engine(Config(), tmp_path)
    engine.market = {"market_id": 1, "size_decimals": 5, "price_decimals": 1}
    engine.db.intent(1, "ENTRY", "buy", "0.0002", "83000")
    engine.db.order_state(1, "CANCEL_REQUESTED")

    class Exchange:
        async def snapshot(self, market_id):
            return AccountSnapshot(
                Decimal(50),
                Decimal(40),
                0,
                Decimal("0.0001"),
                Decimal(83000),
                Decimal(0),
                Decimal(0),
                Decimal(0),
                (),
                (),
                (),
                time.time(),
            )

        async def order_statuses(self, ids):
            return {1: {"status": "canceled", "filled_base_amount": "0.0001"}}

        async def send_stop(self, *args):
            return "stop-tx"

    engine.account = Exchange()  # type: ignore[assignment]
    await engine.reconcile()
    assert (
        engine.db.conn.execute("SELECT state FROM intents WHERE client_id=1").fetchone()[0]
        == "CANCELED"
    )
    assert engine.protection_id == 2
    assert engine.db.unresolved()[0]["kind"] == "STOP"
    engine.db.close()
    engine.recorder.close()


@pytest.mark.asyncio
async def test_unowned_position_pauses_without_sending_order(tmp_path) -> None:
    engine = Engine(Config(), tmp_path)
    engine.market = {"market_id": 1, "size_decimals": 5, "price_decimals": 1}

    class ManualPosition:
        called = False

        async def snapshot(self, market_id):
            return AccountSnapshot(
                Decimal(20),
                Decimal(10),
                0,
                Decimal("0.0001"),
                Decimal(83000),
                Decimal(0),
                Decimal(0),
                Decimal(0),
                (),
                (),
                (),
                time.time(),
            )

        async def order_statuses(self, ids):
            return {}

        async def send_stop(self, *args):
            self.called = True

    adapter = ManualPosition()
    engine.account = adapter  # type: ignore[assignment]
    await engine.reconcile()
    assert engine.manual_exposure and engine.state == "PAUSED"
    assert engine.entries_stopped and not adapter.called
    engine.db.close()
    engine.recorder.close()


@pytest.mark.asyncio
async def test_exit_is_reduce_only_and_duplicate_close_is_not_sent(tmp_path) -> None:
    live_cfg = Config(mode="live_capable", account_index=77)
    adapter = LighterAccount(live_cfg)

    class Response:
        code = 200
        tx_hash = "tx"

    class Signer:
        ORDER_TYPE_LIMIT = 0
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
        calls = []

        async def create_order(self, **kwargs):
            self.calls.append(kwargs)
            return None, Response(), None

        async def create_sl_order(self, **kwargs):
            self.calls.append(kwargs)
            return None, Response(), None

    adapter.signer = Signer()
    intent = OrderIntent(2, "EXIT", "sell", Decimal("0.0001"), Decimal(83000), Stamp())
    await adapter.send_ioc(intent, 1, 5, 1)
    assert adapter.signer.calls[0]["reduce_only"] is True
    assert adapter.signer.calls[0]["base_amount"] == 10
    stop = OrderIntent(3, "STOP", "sell", Decimal("0.0001"), Decimal(82500), Stamp())
    await adapter.send_stop(stop, 1, 5, 1, Decimal(82750))
    assert adapter.signer.calls[1]["reduce_only"] is True

    engine = Engine(Config(), tmp_path)
    engine.market = {"market_id": 1, "size_decimals": 5, "price_decimals": 1}
    engine.account_snapshot = AccountSnapshot(
        Decimal(50),
        Decimal(40),
        0,
        Decimal("0.0001"),
        Decimal(83000),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        (),
        (),
        (),
        time.time(),
    )
    engine.books["lighter"].lighter_snapshot(
        {
            "order_book": {
                "nonce": 1,
                "bids": [{"price": "83000", "size": "2"}],
                "asks": [{"price": "83000.1", "size": "2"}],
            }
        },
        Stamp(),
    )

    class Sender:
        calls = 0

        async def send_ioc(self, *args):
            self.calls += 1
            return "exit-tx"

    sender = Sender()
    engine.account = sender  # type: ignore[assignment]
    assert "belum dikonfirmasi" in await engine.close_position()
    assert "sudah ada" in await engine.close_position()
    assert sender.calls == 1 and engine.exit_id is not None
    engine.db.close()
    engine.recorder.close()


@pytest.mark.asyncio
async def test_stale_comparator_blocks_entry_but_position_timeout_exits(tmp_path) -> None:
    engine = Engine(Config(), tmp_path)
    engine.market = {"market_id": 1, "size_decimals": 5, "price_decimals": 1}
    engine.account_snapshot = AccountSnapshot(
        Decimal(48),
        Decimal(38),
        0,
        Decimal("0.0001"),
        Decimal(83000),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        (),
        (),
        (),
        time.time(),
    )
    engine.books["lighter"].lighter_snapshot(
        {
            "order_book": {
                "nonce": 1,
                "bids": [{"price": "83000", "size": "2"}],
                "asks": [{"price": "83000.1", "size": "2"}],
            }
        },
        Stamp(),
    )
    engine.position_opened_at = time.monotonic() - 181

    class ExitSender:
        calls = 0

        async def send_ioc(self, *args):
            self.calls += 1
            return "exit-tx"

    sender = ExitSender()
    engine.account = sender  # type: ignore[assignment]
    assert not engine.data_valid()  # Binance comparator is disconnected
    await engine.manage_position()
    assert sender.calls == 1 and engine.exit_id is not None
    engine.db.close()
    engine.recorder.close()


@pytest.mark.asyncio
async def test_cancel_uses_reconciled_exchange_order_index() -> None:
    adapter = LighterAccount(Config(mode="live_capable", account_index=77))

    class Response:
        code = 200
        tx_hash = "cancel-tx"

    class Signer:
        sent_index = None

        async def cancel_order(self, *, market_index, order_index):
            self.sent_index = order_index
            return None, Response(), None

    signer = Signer()
    adapter.signer = signer
    with pytest.raises(ValueError, match="exchange order index"):
        await adapter.cancel(1, 0)
    await adapter.cancel(1, 998)
    assert signer.sent_index == 998
