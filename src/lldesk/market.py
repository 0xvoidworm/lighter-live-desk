from __future__ import annotations

import asyncio
import gzip
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import websockets

from .models import MarketEvent, Quote, Stamp

BINANCE_PUBLIC_WS = (
    "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/btcusdt@depth@100ms"
)
BINANCE_MARKET_WS = "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade"
BINANCE_DEPTH = "https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000"
LIGHTER_WS = "wss://mainnet.zklighter.elliot.ai/stream"
LIGHTER_MARKETS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?filter=perp"
FX_BOOK = "https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=USDCUSDT"


class GapError(Exception):
    pass


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


@dataclass
class Book:
    venue: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    sequence: int | None = None
    valid: bool = False
    updated_ns: int = 0
    gaps: int = 0
    bridged: bool = False

    def invalidate(self) -> None:
        self.valid = False
        self.sequence = None
        self.bridged = False
        self.bids.clear()
        self.asks.clear()

    def _levels(self, bids: list[Any], asks: list[Any]) -> None:
        for side, updates in ((self.bids, bids), (self.asks, asks)):
            for item in updates:
                price, size = (item["price"], item["size"]) if isinstance(item, dict) else item[:2]
                p, q = dec(price), dec(size)
                if q <= 0:
                    side.pop(p, None)
                else:
                    side[p] = q

    def binance_snapshot(self, data: dict[str, Any], stamp: Stamp) -> None:
        self.invalidate()
        self._levels(data["bids"], data["asks"])
        self.sequence = int(data["lastUpdateId"])
        self.valid = bool(self.bids and self.asks)
        self.updated_ns = stamp.monotonic_ns

    def binance_delta(self, data: dict[str, Any], stamp: Stamp) -> None:
        if self.sequence is None:
            raise GapError("Binance delta before snapshot")
        u, first = int(data["u"]), int(data["U"])
        if u < self.sequence or (self.bridged and u <= self.sequence):
            return
        if first > self.sequence + (1 if self.bridged else 0):
            self.gaps += 1
            self.invalidate()
            raise GapError("Binance sequence gap")
        prev = data.get("pu")
        if prev is not None and self.bridged and int(prev) != self.sequence:
            self.gaps += 1
            self.invalidate()
            raise GapError("Binance pu gap")
        self._levels(data.get("b", []), data.get("a", []))
        self.sequence = u
        self.bridged = True
        self.valid = bool(self.bids and self.asks)
        self.updated_ns = stamp.monotonic_ns

    def lighter_snapshot(self, data: dict[str, Any], stamp: Stamp) -> None:
        body = data.get("order_book", data)
        self.invalidate()
        self._levels(body.get("bids", []), body.get("asks", []))
        self.sequence = int(body["nonce"])
        self.valid = bool(self.bids and self.asks)
        self.updated_ns = stamp.monotonic_ns

    def lighter_delta(self, data: dict[str, Any], stamp: Stamp) -> None:
        body = data.get("order_book", data)
        if self.sequence is None:
            raise GapError("Lighter delta before snapshot")
        nonce = int(body["nonce"])
        if nonce <= self.sequence:
            return
        if int(body["begin_nonce"]) != self.sequence:
            self.gaps += 1
            self.invalidate()
            raise GapError("Lighter nonce gap")
        self._levels(body.get("bids", []), body.get("asks", []))
        self.sequence = nonce
        self.valid = bool(self.bids and self.asks)
        self.updated_ns = stamp.monotonic_ns

    def quote(self) -> Quote | None:
        if not self.valid or not self.bids or not self.asks:
            return None
        bid, ask = max(self.bids), min(self.asks)
        if bid <= 0 or ask <= bid:
            return None
        return Quote(bid, ask, self.bids[bid], self.asks[ask], Stamp())

    def price_for(self, side: str, quantity: Decimal) -> Decimal | None:
        levels = self.asks if side == "buy" else self.bids
        remaining = quantity
        paid = Decimal(0)
        for price in sorted(levels, reverse=(side == "sell")):
            take = min(remaining, levels[price])
            paid += price * take
            remaining -= take
            if remaining <= 0:
                return paid / quantity
        return None


async def market_metadata() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(LIGHTER_MARKETS)
        response.raise_for_status()
        data = response.json()
    matches = [
        x
        for x in data["order_book_details"]
        if x.get("symbol") == "BTC"
        and x.get("market_type") == "perp"
        and x.get("status") == "active"
    ]
    if len(matches) != 1:
        raise ValueError("active BTC perpetual market is not unique")
    return matches[0]


class Recorder:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = directory
        self.hour = ""
        self.handle: Any = None

    def write(self, event: MarketEvent) -> None:
        hour = time.strftime("%Y%m%d-%H", time.gmtime())
        if hour != self.hour:
            if self.handle:
                self.handle.close()
            self.handle = gzip.open(self.directory / f"market-{hour}.jsonl.gz", "at")
            self.hour = hour
        self.handle.write(
            json.dumps(
                {
                    "venue": event.venue,
                    "kind": event.kind,
                    "payload": event.payload,
                    "receipt_ns": event.stamp.receipt_ns,
                }
            )
            + "\n"
        )

    def close(self) -> None:
        if self.handle:
            self.handle.close()


class Feeds:
    def __init__(self, market_id: int, queue: asyncio.Queue[MarketEvent]):
        self.market_id = market_id
        self.queue = queue
        self.reconnects = {"binance": 0, "lighter": 0}
        self.binance_public_ok = False
        self.binance_trades_ok = False

    async def emit(
        self, venue: str, kind: str, payload: dict[str, Any], exchange_ms: int | None = None
    ) -> None:
        event = MarketEvent(venue, kind, payload, Stamp(exchange_ms=exchange_ms))
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            # Shared queue lost both venues' ordering. Invalidate both books.
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            for affected in ("binance", "lighter"):
                self.queue.put_nowait(
                    MarketEvent(affected, "disconnect", {"reason": "queue overflow"}, Stamp())
                )
            raise GapError(f"{venue} queue overflow; resync required") from exc

    async def binance(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._binance_public())
            group.create_task(self._binance_trades())

    async def _binance_public(self) -> None:
        while True:
            try:
                async with websockets.connect(
                    BINANCE_PUBLIC_WS, ping_interval=20, max_queue=256, open_timeout=10
                ) as ws:
                    self.binance_public_ok = True
                    buffered: list[dict[str, Any]] = []
                    snap_task = asyncio.create_task(self._binance_snapshot())
                    try:
                        while not snap_task.done():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1)
                            except TimeoutError:
                                continue
                            msg = json.loads(raw)
                            data = msg.get("data", msg)
                            buffered.append(data)
                            if len(buffered) > 2048:
                                raise GapError("Binance bootstrap buffer overflow")
                        await self.emit("binance", "snapshot", await snap_task)
                    finally:
                        if not snap_task.done():
                            snap_task.cancel()
                    for data in buffered:
                        await self._binance_message(data)
                    async for raw in ws:
                        message = json.loads(raw)
                        await self._binance_message(message.get("data", message))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.binance_public_ok = False
                self.reconnects["binance"] += 1
                await self.emit("binance", "disconnect", {"reason": type(exc).__name__})
                await asyncio.sleep(min(2 ** min(self.reconnects["binance"], 5), 30))

    async def _binance_trades(self) -> None:
        while True:
            try:
                async with websockets.connect(
                    BINANCE_MARKET_WS, ping_interval=20, max_queue=256, open_timeout=10
                ) as ws:
                    self.binance_trades_ok = True
                    async for raw in ws:
                        message = json.loads(raw)
                        await self._binance_message(message.get("data", message))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.binance_trades_ok = False
                self.reconnects["binance"] += 1
                await self.emit("binance", "disconnect", {"reason": type(exc).__name__})
                await asyncio.sleep(min(2 ** min(self.reconnects["binance"], 5), 30))

    async def _binance_snapshot(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(BINANCE_DEPTH)
            response.raise_for_status()
            return response.json()

    async def _binance_message(self, data: dict[str, Any]) -> None:
        if data.get("s") != "BTCUSDT" or data.get("st", 1) != 1:
            return
        kind = data.get("e")
        if kind in {"depthUpdate", "bookTicker", "aggTrade"}:
            await self.emit("binance", kind, data, data.get("T") or data.get("E"))

    async def lighter(self) -> None:
        while True:
            try:
                async with websockets.connect(
                    LIGHTER_WS, ping_interval=30, max_queue=256, open_timeout=10
                ) as ws:
                    for channel in (
                        f"order_book/{self.market_id}",
                        f"trade/{self.market_id}",
                        f"ticker/{self.market_id}",
                    ):
                        await ws.send(json.dumps({"type": "subscribe", "channel": channel}))
                    async for raw in ws:
                        data = json.loads(raw)
                        channel = str(data.get("channel", ""))
                        if channel.startswith(("order_book:", "trade:", "ticker:")):
                            await self.emit(
                                "lighter", channel.split(":")[0], data, data.get("timestamp")
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reconnects["lighter"] += 1
                await self.emit("lighter", "disconnect", {"reason": type(exc).__name__})
                await asyncio.sleep(min(2 ** min(self.reconnects["lighter"], 5), 30))
