"""Depth-only production transport, snapshot bridge and recovery; no network."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import aiohttp
import pytest

from lldesk import market
from lldesk.market import Book, Feeds, GapError
from lldesk.models import Stamp


def delta(first, last, previous, size="3"):
    return {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "U": first,
        "u": last,
        "pu": previous,
        "T": time.time_ns() // 1_000_000 - 100,
        "b": [["100", size]],
        "a": [],
    }


async def test_depth_only_transport_buffers_bootstrap_and_recovers_remote_close(monkeypatch):
    queue = asyncio.Queue()
    feeds = Feeds(1, queue)
    connections = []
    gates = [asyncio.Event(), asyncio.Event()]

    class Socket:
        close_code = 1001

        def __init__(self, index):
            self.index = index

        def exception(self):
            return None

        async def receive_str(self, timeout):
            # The depth arrives while REST bootstrap is pending. Preserve that
            # receipt stamp through buffering; no ticker is ever emitted.
            gates[self.index].set()
            await asyncio.sleep(0)
            return json.dumps(
                {
                    "data": delta(
                        99 if self.index == 0 else 199,
                        102 if self.index == 0 else 202,
                        98 if self.index == 0 else 198,
                    )
                }
            )

        async def __aiter__(self):
            if self.index == 0:
                for update in [
                    delta(99, 102, 98, "9"),
                    delta(99, 101, 98, "8"),
                    delta(110, 120, 102, "4"),
                ]:
                    yield SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT, data=json.dumps({"data": update})
                    )
                yield SimpleNamespace(type=aiohttp.WSMsgType.CLOSE)
            else:
                await asyncio.Event().wait()

    class Client:
        def __init__(self, index):
            self.index = index

        @asynccontextmanager
        async def ws_connect(self, url, heartbeat):
            assert url == market.BINANCE_PUBLIC_WS
            assert url.endswith("?streams=btcusdt@depth@100ms") and heartbeat == 20
            connections.append(url)
            yield Socket(self.index)

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield Client(len(connections))

    async def snapshot(client):
        await gates[client.index].wait()
        return {
            "lastUpdateId": 100 if client.index == 0 else 200,
            "bids": [["100", "2"]],
            "asks": [["101", "2"]],
        }

    monkeypatch.setattr(market, "binance_session", session)
    monkeypatch.setattr(feeds, "_binance_snapshot", snapshot)
    task = asyncio.create_task(feeds._binance_public())
    book = Book("binance")
    kinds = []
    duplicate_stamp = None
    try:
        while not (book.bridged and book.sequence == 202):
            event = await asyncio.wait_for(queue.get(), 4)
            kinds.append(event.kind)
            if event.kind == "snapshot":
                book.binance_snapshot(event.payload, event.stamp)
                assert not book.bridged  # snapshot alone cannot admit entry
            elif event.kind == "depthUpdate":
                prior = book.stamp
                book.binance_delta(event.payload, event.stamp)
                if event.payload["u"] in {101, 102} and prior is duplicate_stamp:
                    assert book.stamp is prior and book.bids[Decimal(100)] == 3
                if event.payload["u"] == 102 and duplicate_stamp is None:
                    duplicate_stamp = book.stamp
            else:
                assert event.kind == "disconnect"
                assert event.payload["transport_detail"]["close_code"] == 1001
                assert event.payload["retry_delay_s"] == 2  # unchanged real backoff
                book.invalidate()
        assert len(connections) == 2 and feeds.channels["binance_public"]["reconnects"] == 1
        assert kinds.count("snapshot") == 2 and "bookTicker" not in kinds
        assert book.valid and book.bids[Decimal(100)] == 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("bad", [delta(103, 104, 101), delta(103, 104, None)])
def test_sequence_gap_requires_new_snapshot_and_bridge(bad):
    book = Book("binance")
    snapshot = {"lastUpdateId": 100, "bids": [["100", "2"]], "asks": [["101", "2"]]}
    book.binance_snapshot(snapshot, Stamp())
    book.binance_delta(delta(99, 102, 98), Stamp())
    with pytest.raises(GapError, match="pu gap"):
        book.binance_delta(bad, Stamp())
    assert not book.valid and book.sequence is None
    with pytest.raises(GapError, match="before snapshot"):
        book.binance_delta(delta(103, 104, 102), Stamp())
    book.binance_snapshot(snapshot | {"lastUpdateId": 104}, Stamp())
    assert not book.bridged
    book.binance_delta(delta(104, 104, 102), Stamp())
    book.binance_delta(delta(110, 120, 104), Stamp())
    assert book.valid and book.bridged and book.sequence == 120
