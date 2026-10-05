"""Buffered socket bursts must give the ready market consumer a turn."""

import asyncio
from contextlib import suppress

from lldesk.market import Feeds
from lldesk.models import Stamp


async def test_buffered_burst_does_not_overflow_a_ready_consumer():
    queue = asyncio.Queue(maxsize=2048)
    feeds = Feeds(1, queue)
    consumed = []
    original = Stamp(receipt_ns=1, monotonic_ns=1, exchange_ms=1)

    async def consume():
        while True:
            event = await queue.get()
            consumed.append(event)
            queue.task_done()

    consumer = asyncio.create_task(consume())
    try:
        # emit and an already-buffered websocket read need not suspend in asyncio.
        # A ready consumer should keep up; no queue enlargement or stale stamping.
        for i in range(4096):
            await feeds.emit("binance", "ticker", {"sequence": i}, stamp=original)
        await asyncio.wait_for(queue.join(), timeout=1)
        assert [e.payload["sequence"] for e in consumed] == list(range(4096))
        assert all(e.stamp is original for e in consumed)
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer
