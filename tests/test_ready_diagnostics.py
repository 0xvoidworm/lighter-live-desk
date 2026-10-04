"""Real saved response parsing and deterministic readiness/metadata failures, no I/O."""

import asyncio
import json
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lighter.models.trade import Trade
from test_probe_only import engine as engine_fixture

from lldesk.db import Database
from lldesk.engine import Engine
from lldesk.market import Feeds
from lldesk.models import MarketEvent, Stamp
from lldesk.strategy import DeviationStrategy

engine = engine_fixture


@pytest.mark.parametrize("trade_id", ["33141524453", "33141644233"])
def test_real_trade_response_fees_absent_before_sdk_parsing(trade_id):
    raw = json.loads((Path(__file__).parent / "fixtures" / f"trade-{trade_id}.json").read_text())
    assert "maker_fee" not in raw and "taker_fee" not in raw
    parsed = Trade.from_dict(raw).to_dict()
    assert parsed.get("maker_fee") is None and parsed.get("taker_fee") is None
    assert Trade.from_dict({**raw, "taker_fee": 17}).to_dict()["taker_fee"] == 17


def samples(strategy, times):
    strategy.samples = deque((float(t), Decimal("1"), Decimal("2"), Decimal("2")) for t in times)


def test_coverage_reports_duration_and_count_independently():
    strategy = DeviationStrategy(600)
    samples(strategy, range(401, 1001))
    c = strategy.coverage(1000)
    assert c["total_samples"] == 600 and c["recent_samples"] == 600
    assert c["duration_remaining_s"] == 1 and not c["warmup_complete"]
    assert c["coverage_complete"] and not c["ready"]
    samples(strategy, range(42, 1001, 2))
    c = strategy.coverage(1000)
    assert c["warmup_complete"] and c["recent_samples"] == 301
    assert not c["coverage_complete"] and c["long_gap_count"] == 0


def test_old_gap_blocks_until_it_leaves_unchanged_600_second_window():
    strategy = DeviationStrategy(600)
    samples(strategy, [t for t in range(400, 1001) if not 501 <= t <= 509])
    c = strategy.coverage(1000)
    assert c["warmup_complete"] and c["recent_samples"] >= 480
    assert c["max_sample_gap_s"] == 10 and c["allowed_gap_s"] == 5
    assert c["long_gap_count"] == 1 and c["gap_wait_remaining_s"] == 100
    strategy.samples.extend(
        (float(t), Decimal(1), Decimal(2), Decimal(2)) for t in range(1001, 1101)
    )
    assert strategy.coverage(1100)["long_gap_count"] == 1
    assert strategy.coverage(1100.001)["ready"]


def test_market_channel_disconnect_preserves_public_book_but_still_blocks_sampling(engine):
    book = engine.books["binance"]
    book.binance_snapshot(
        {"lastUpdateId": 100, "bids": [["83000", "1"]], "asks": [["83001", "1"]]}, Stamp()
    )
    book.binance_delta({"U": 100, "u": 101, "b": [], "a": []}, Stamp())
    engine.feeds = SimpleNamespace(binance_public_ok=True, binance_trades_ok=False)
    engine.data_valid = Engine.data_valid.__get__(engine)
    engine.fx = Decimal(1)
    engine.fx_at = time.monotonic()
    samples(engine.strategy, range(10))
    engine.on_market(
        MarketEvent(
            "binance", "disconnect", {"channel": "market", "reason": "ConnectionError"}, Stamp()
        )
    )
    assert book.valid and book.sequence == 101
    assert len(engine.strategy.samples) == 10
    assert engine.strategy.last_interruption["samples_preserved"] == 10
    assert not engine.data_valid()
    engine.feeds.binance_trades_ok = True
    assert engine.data_valid()
    engine.on_market(
        MarketEvent(
            "binance", "disconnect", {"channel": "public", "reason": "ConnectionError"}, Stamp()
        )
    )
    assert not book.valid and book.sequence is None


async def test_retry_backoff_uses_per_channel_consecutive_failures_not_lifetime(monkeypatch):
    feeds = Feeds(1, asyncio.Queue())
    feeds.reconnects["binance"] = 10
    sleep = AsyncMock()
    monkeypatch.setattr("lldesk.market.asyncio.sleep", sleep)
    await feeds.reconnect("binance", "public", ConnectionError())
    await feeds.reconnect("binance", "public", ConnectionError())
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4]
    feeds.channel_started("binance_public")
    feeds.channels["binance_public"]["_started"] = time.monotonic() - 31
    feeds.channel_data("binance_public")
    await feeds.reconnect("binance", "public", ConnectionError())
    await feeds.reconnect("binance", "market", ConnectionError())
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4, 2, 2]
    assert feeds.reconnects["binance"] == 14
    assert feeds.channel_status()["binance_public"]["reconnects"] == 3


def test_fee_lookup_stops_after_three_spaced_checks_and_preserves_unknown(tmp_path, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr("lldesk.db.time.time", lambda: now[0])
    db = Database(tmp_path / "fees.db")
    db.intent(1, "PROBE", "buy", "0.00012", "84556.1")
    db.add_fill("123", 1, "0.00012", "84539.0", None, 1000)
    for _ in range(3):
        assert db.fee_lookup_candidates() == ["123"]
        db.fee_lookup_completed(["123"])
        assert not db.fee_lookup_candidates()
        now[0] += 60
    now[0] += 3600
    assert not db.fee_lookup_candidates() and db.pending_fees() == 1
    assert db.fee_lookup_status()["lookup_paused_count"] == 1
    db.close()
    db = Database(tmp_path / "fees.db")
    assert not db.fee_lookup_candidates() and db.pending_fees() == 1
    db.add_fill("123", 1, "0.00012", "84539.0", "0.000001", 1000)
    assert db.pending_fees() == 0
    db.close()


def report_for(engine):
    engine.db.intent(1, "PROBE", "buy", "0.00012", "84556.1")
    engine.db.add_fill("123", 1, "0.00012", "84539.0", None, 1000)
    return {
        "trade_id": "123",
        "client_id": 1,
        "qty": "0.000120",
        "price": "84539.000000",
        "side": "Open Long",
        "reported_fee_usdc": "0.0000",
        "precision_decimals": 4,
        "source": "official_account_export",
        "account_index": 77,
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("trade_id", "456"),
        ("qty", "0.00013"),
        ("price", "84539.1"),
        ("side", "Close Long"),
        ("client_id", 8),
        ("reported_fee_usdc", "NaN"),
    ],
)
def test_official_fee_report_requires_matching_fill_economics(engine, key, value):
    report = report_for(engine)
    with pytest.raises(ValueError):
        engine.db.record_fee_report("123", {**report, key: value})
    assert engine.db.pending_fees() == 1


def test_reported_fee_and_net_cash_match_do_not_silently_relax_entry_policy(engine):
    report = report_for(engine)
    engine.db.record_fee_report("123", report)
    engine.db.event(
        "NET_CASH_AUDIT",
        {
            "equity_after": "49.430576",
            "cash_change_usdc": "0.000576",
            "fingerprint": engine.db.accounting_fingerprint(),
        },
    )
    assert engine.db.fee_report_status()["reported_fee_total_usdc"] == "0.0000"
    assert engine.db.pending_fees() == 1
    assert engine.db.net_cash_status("49.430576", "0")["status"] == "matched"
    assert any("fee trade belum diketahui" in b for b in engine.live_blockers())
    assert engine.db.net_cash_status("49.430577", "0")["net_difference_usdc"] == "0.000001"
    assert engine.db.net_cash_status("49.430577", "0")["status"] == "unexplained_difference"
    engine.db.record_flows((("deposit:new", "1"),))
    assert engine.db.net_cash_status("50.430576", "0")["status"] == "needs_new_audit"
    assert not engine.account.sent
