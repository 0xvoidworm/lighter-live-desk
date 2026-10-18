"""Public data only; real stale timestamps, mock transport, no account/signing/service calls."""

import asyncio
import json
from collections import deque
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from test_probe_only import engine as engine_fixture

from lldesk.feed_liveness import StalePublicDetector
from lldesk.market import Book, DefiniteSourceStale, Feeds, GapError
from lldesk.models import MarketEvent, Quote, Stamp
from lldesk.source_time import BinanceSourceClock, ClockEvidence
from lldesk.strategy import DeviationStrategy

engine = engine_fixture
original_timeout = asyncio.timeout


@pytest.fixture
def real():
    return json.loads((Path(__file__).parent / "fixtures/stale_public_real.json").read_text())


def clock_for(real):
    e = real["clock"]
    clock = BinanceSourceClock(1500)
    clock.evidence = ClockEvidence(
        Stamp(receipt_ns=e["received_ns"], monotonic_ns=e["received_monotonic_ns"]),
        e["offset_low_ms"],
        e["offset_high_ms"],
        e["server_ms"],
        e["rtt_ms"],
        e["host"],
    )
    return clock


def stamp(frame):
    return Stamp(
        exchange_ms=frame["exchange_ms"],
        receipt_ns=frame["receipt_ns"],
        monotonic_ns=frame["monotonic_ns"],
        exchange_event_ms=frame["exchange_event_ms"],
    )


def assessment(clock, frame):
    return clock.assess(stamp(frame), now_ns=frame["monotonic_ns"], wall_ns=frame["receipt_ns"])


def definitely_old(real):
    clock = clock_for(real)
    return next(
        assessment(clock, f)
        for f in real["frames"]
        if assessment(clock, f).get("source_age_low_ms", 0) >= 1500
    )


def test_boundary_distinct_ordered_generation_and_cooldown(real):
    src = definitely_old(real) | {"source_age_low_ms": 1500}
    d = StalePublicDetector()
    assert not d.observe(src, "a", 1, 10, 1, 1500)
    assert not d.observe(src, "a", 1, 10, 2, 1500)
    assert not d.observe(src, "a", 2, 9, 2, 1500)  # out-of-order is not confidence
    assert d.observe(src, "a", 3, 11, 3, 1500)
    assert not d.observe(src, "a", 4, 12, 100, 1500)  # once per generation
    assert not d.observe(src, "b", 5, 13, 4, 1500)
    assert not d.observe(src, "b", 6, 14, 5, 1500)
    assert d.observe(src, "b", 7, 15, 63, 1500)


@pytest.mark.parametrize(
    "fault", ["future", "expired", "unsync", "missing", "high_only", "T_missing"]
)
def test_unproven_or_not_definitely_old_never_reconnects(real, fault):
    src = definitely_old(real)
    if fault == "future":
        src = src | {"reason": "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN"}
    if fault == "expired":
        src = src | {"clock_evidence_age_s": 121}
    if fault == "unsync":
        src = src | {"latest_proof": {"valid": False}}
    if fault == "missing":
        src = {}
    if fault == "high_only":
        src = src | {"source_age_low_ms": 1499.99}
    if fault == "T_missing":
        src = src | {"transaction_ms": None}
    d = StalePublicDetector()
    for n in range(20):
        assert not d.observe(src, "a", n, n, n * 10, 1500)


class Socket:
    def __init__(self, hangs=False):
        self.closes = 0
        self.hangs = hangs

    async def close(self):
        self.closes += 1
        if self.hangs:
            await asyncio.Event().wait()


async def test_actual_ordered_stale_stream_baseline_vs_candidate_and_generation(engine, real):
    clock = clock_for(real)
    first = real["frames"][0]
    # Baseline reader/book: all sequence-correct frames accepted, no transport recovery.
    baseline = Book(
        "binance",
        bids={Decimal("83000"): Decimal(2)},
        asks={Decimal("83000.1"): Decimal(2)},
        sequence=first["payload"]["pu"],
        valid=True,
        bridged=True,
    )
    for f in real["frames"]:
        baseline.binance_delta(f["payload"], stamp(f))
    assert baseline.valid and baseline.bridged
    assert not assessment(clock, real["frames"][-1])["passed"]
    before_samples = engine.strategy.samples
    before_session = engine.session.read()
    before_deadline = engine.session_end
    socket = Socket()
    engine.config = engine.config.model_copy(update={"allow_live_orders": False})
    engine.feeds.channels["binance_public"]["connection_id"] = "old"
    engine.feeds.public_socket = socket
    book = engine.books["binance"]
    book.sequence, book.bridged = first["payload"]["pu"], True
    for f in real["frames"]:
        event = MarketEvent("binance", "depthUpdate", f["payload"], stamp(f), connection_id="old")
        engine.on_market(event)
        engine._last_data_guard = {"source_freshness": assessment(clock, f)}
        engine.monitor_public_source()
        await asyncio.sleep(0)
        assert event.stamp == stamp(f)  # no restamping
    await engine.public_recovery_task
    assert socket.closes == 1 and not book.valid and not engine.feeds.binance_public_ok
    assert engine.strategy.samples is before_samples and engine.session.read() == before_session
    assert engine.session_end == before_deadline and not engine.account.sent
    fresh = real["fresh_frame"]
    engine.feeds.channels["binance_public"]["connection_id"] = "new"
    engine.feeds.binance_public_ok = True
    # An old REST snapshot alone cannot be eligible; bridge is mandatory.
    old_snapshot = {
        "lastUpdateId": fresh["payload"]["U"],
        "bids": [["83000", "2"]],
        "asks": [["83000.1", "2"]],
    }
    engine.on_market(
        MarketEvent(
            "binance", "snapshot", old_snapshot, stamp(real["frames"][0]), connection_id="new"
        )
    )
    assert not book.bridged
    engine.on_market(
        MarketEvent("binance", "depthUpdate", fresh["payload"], stamp(fresh), connection_id="new")
    )
    assert book.bridged and assessment(clock, fresh)["passed"]
    retained = (book.sequence, book.stamp, dict(book.bids))
    engine.on_market(
        MarketEvent(
            "binance",
            "depthUpdate",
            real["frames"][-1]["payload"],
            stamp(real["frames"][-1]),
            connection_id="old",
        )
    )
    engine.on_market(
        MarketEvent(
            "binance", "disconnect", {"channel": "public"}, stamp(first), connection_id="old"
        )
    )
    assert (book.sequence, book.stamp, book.bids) == retained and book.valid
    assert engine.market_processing["binance:depthUpdate"]["old_public_generation"] > 0


async def test_close_timeout_stays_blocked_without_retry_or_rearm(engine, real):
    socket = Socket(hangs=True)
    feeds = engine.feeds
    feeds.channels["binance_public"]["connection_id"] = "a"
    feeds.public_socket = socket
    short = asyncio.timeout(0.01)
    with patch("lldesk.market.asyncio.timeout", return_value=short):
        with pytest.raises(TimeoutError):
            await feeds.recover_public_source("a")
    assert feeds.public_recovery_blocked_id == "a" and not feeds.binance_public_ok
    assert socket.closes == 1
    assert not await feeds.recover_public_source("different") and socket.closes == 1
    feeds.channels["binance_public"]["connection_id"] = "b"
    feeds.public_socket = Socket()
    assert await feeds.recover_public_source("b")
    assert feeds.public_socket.closes == 1
    assert not engine.account.sent


async def test_reader_reconnect_uses_existing_bounded_public_backoff_only():
    feeds = Feeds(1, asyncio.Queue())
    feeds.binance_trades_ok = True
    feeds.channels["lighter"]["connected"] = True
    delays = []

    async def sleep(delay):
        delays.append(delay)

    with patch("lldesk.market.asyncio.sleep", side_effect=sleep):
        for _ in range(6):
            await feeds.reconnect("binance", "public", DefiniteSourceStale("old"))
    assert delays == [2, 4, 8, 16, 30, 30]
    assert feeds.binance_trades_ok and feeds.channels["lighter"]["connected"]
    assert feeds.queue.qsize() == 6


async def test_actual_reader_close_backoff_and_new_generation_lifecycle():
    feeds = Feeds(1, asyncio.Queue())
    made = []
    delays = []

    class ReaderSocket(Socket):
        def __init__(self):
            super().__init__()
            self.closed_event = asyncio.Event()

        async def receive_str(self, timeout):
            await asyncio.sleep(0.001)
            return json.dumps(
                {"e": "depthUpdate", "U": 100, "u": 101, "pu": 99, "b": [], "a": [], "T": 1}
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed_event.wait()
            raise StopAsyncIteration

        async def close(self):
            await super().close()
            self.closed_event.set()

    class Client:
        @asynccontextmanager
        async def ws_connect(self, *args, **kwargs):
            ws = ReaderSocket()
            made.append(ws)
            yield ws

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield Client()

    async def snapshot(client):
        return {"lastUpdateId": 100, "bids": [["1", "1"]], "asks": [["2", "1"]]}

    actual_reconnect = feeds.reconnect

    async def reconnect(*args):
        async def no_wait(delay):
            delays.append(delay)

        with patch("lldesk.market.asyncio.sleep", side_effect=no_wait):
            await actual_reconnect(*args)

    with (
        patch("lldesk.market.binance_session", session),
        patch.object(feeds, "_binance_snapshot", snapshot),
        patch.object(feeds, "reconnect", reconnect),
    ):
        task = asyncio.create_task(feeds._binance_public())
        try:
            async with original_timeout(1):
                while not feeds.binance_public_ok:
                    await asyncio.sleep(0.001)
                old = feeds.channels["binance_public"]["connection_id"]
                assert await feeds.recover_public_source(old)
                while len(made) < 2 or not feeds.binance_public_ok:
                    await asyncio.sleep(0.001)
            assert made[0].closes == 1 and made[1].closes == 0
            assert delays == [2]
            assert feeds.public_recovery_reason is None
            assert feeds.channels["binance_public"]["connection_id"] != old
            events = []
            while not feeds.queue.empty():
                events.append(feeds.queue.get_nowait())
            assert any(e.kind == "disconnect" and e.connection_id == old for e in events)
            assert len({e.connection_id for e in events if e.kind == "snapshot"}) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_engine_close_failure_latches_generation_then_new_generation_can_resume(engine, real):
    first = real["frames"][0]
    engine.feeds.channels["binance_public"]["connection_id"] = "failed"
    engine.feeds.public_socket = Socket(hangs=True)
    engine.books["binance"].sequence = first["payload"]["pu"]
    engine.books["binance"].bridged = True
    before = (engine.session.read(), engine.session_end, engine.strategy.samples)
    source = definitely_old(real)
    with patch("lldesk.market.asyncio.timeout", side_effect=lambda _: original_timeout(0.01)):
        for f in real["frames"]:
            engine.on_market(
                MarketEvent(
                    "binance", "depthUpdate", f["payload"], stamp(f), connection_id="failed"
                )
            )
            engine._last_data_guard = {"source_freshness": source}
            engine.monitor_public_source()
            await asyncio.sleep(0)
        await engine.public_recovery_task
    task = engine.public_recovery_task
    for _ in range(10):
        engine.monitor_public_source()
    assert engine.public_recovery_task is task
    assert engine.feeds.public_socket.closes == 1
    assert not engine.books["binance"].valid and not engine.feeds.binance_public_ok
    assert engine.feeds.public_recovery_blocked_id == "failed"
    assert (engine.session.read(), engine.session_end, engine.strategy.samples) == before
    assert not engine.account.sent
    # A successful *later* reader connection is a new generation, not a retry of this task.
    engine.feeds.channels["binance_public"]["connection_id"] = "later"
    f = real["fresh_frame"]
    engine.on_market(
        MarketEvent(
            "binance",
            "snapshot",
            {
                "lastUpdateId": f["payload"]["U"],
                "bids": [["83000", "2"]],
                "asks": [["83000.1", "2"]],
            },
            stamp(f),
            connection_id="later",
        )
    )
    assert not engine.books["binance"].bridged
    engine.on_market(
        MarketEvent("binance", "depthUpdate", f["payload"], stamp(f), connection_id="later")
    )
    assert engine.books["binance"].bridged
    assert assessment(clock_for(real), f)["passed"]


def test_stale_final_delta_and_missing_bridge_remain_ineligible(real):
    f = real["frames"][-1]
    book = Book("binance")
    snap = {
        "lastUpdateId": f["payload"]["U"] - 1,
        "bids": [["83000", "2"]],
        "asks": [["83000.1", "2"]],
    }
    book.binance_snapshot(snap, stamp(f))
    assert not book.bridged
    with pytest.raises(GapError):
        book.binance_delta(f["payload"], stamp(f))
    assert not book.valid
    snap["lastUpdateId"] = f["payload"]["U"]
    book.binance_snapshot(snap, stamp(f))
    book.binance_delta(f["payload"], stamp(f))
    assert book.bridged and not assessment(clock_for(real), f)["passed"]


@pytest.mark.parametrize("resync_duration", [2.4, 7.0])
def test_recovery_timeline_coverage_and_basis_without_history_reset(resync_duration):
    strategy = DeviationStrategy()
    strategy.samples = deque(
        [(1000 + n, Decimal(n % 9), Decimal(2), Decimal(2)) for n in range(601)]
    )
    strategy.last_sample_at = 1600
    before = strategy.samples
    # Last validt0; first definiteold1.5s, second1.6s; duration includes2sbackoff.
    now = 1600 + 1.6 + resync_duration
    q = Quote(Decimal("83000"), Decimal("83000.1"), Decimal(2), Decimal(2), Stamp())
    with patch("lldesk.strategy.time.monotonic", return_value=now):
        decision = strategy.evaluate(q, q, True, Decimal(1))
    assert strategy.samples is before
    if resync_duration < 3.4:
        assert strategy.coverage(now)["ready"] and decision.basis_bps is not None
    else:
        assert not strategy.coverage(now)["ready"] and decision.basis_bps is None
        # No synthetic gap filling: each actual subsequent valid second restores rolling coverage.
        for t in range(int(now) + 1, int(now) + 602):
            with patch("lldesk.strategy.time.monotonic", return_value=float(t)):
                decision = strategy.evaluate(q, q, True, Decimal(1))
        assert strategy.coverage(float(t))["ready"] and decision.basis_bps is not None
