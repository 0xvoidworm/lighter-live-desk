from __future__ import annotations

import asyncio
import gzip
import json
import os
import time
import traceback
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import websockets

from .models import MarketEvent, Quote, Stamp
from .network import DnsMode, binance_session
from .transport_trace import TransportTrace
from .websocket_trace import (
    CURRENT_CONNECTION,
    ObservedAiohttpSocket,
    ObservedLighterSocket,
    WebsocketTrace,
)

BINANCE_PUBLIC_WS = "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms"
BINANCE_MARKET_WS = "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade"
BINANCE_DEPTH = "https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000"
LIGHTER_WS = "wss://mainnet.zklighter.elliot.ai/stream"
LIGHTER_MARKETS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?filter=perp"


class GapError(Exception):
    pass


def websocket_closed(ws: Any, channel: str, frame_type: str | None = None) -> ConnectionError:
    error = ConnectionError(f"Binance {channel} websocket closed")
    error.ws_close_code = ws.close_code  # type: ignore[attr-defined]
    error.ws_frame_type = frame_type  # type: ignore[attr-defined]
    cause = ws.exception()
    error.ws_cause = type(cause).__name__ if cause else None  # type: ignore[attr-defined]
    # 1006 is an abnormal-closure classification, not evidence of a remote close.
    # Keep the original transport exception instead of discarding its message/stack.
    error.__cause__ = cause
    return error


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
    stamp: Stamp = field(default_factory=lambda: Stamp(receipt_ns=0, monotonic_ns=0))

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
        self.stamp = stamp

    def binance_delta(self, data: dict[str, Any], stamp: Stamp) -> None:
        if self.sequence is None:
            raise GapError("Binance delta before snapshot")
        u, first = int(data["u"]), int(data["U"])
        if u < self.sequence or (self.bridged and u <= self.sequence):
            return
        if not self.bridged and first > self.sequence:
            self.gaps += 1
            self.invalidate()
            raise GapError("Binance sequence gap")
        prev = data.get("pu")
        if self.bridged and (prev is None or int(prev) != self.sequence):
            self.gaps += 1
            self.invalidate()
            raise GapError("Binance pu gap")
        self._levels(data.get("b", []), data.get("a", []))
        self.sequence = u
        self.bridged = True
        self.valid = bool(self.bids and self.asks)
        self.updated_ns = stamp.monotonic_ns
        self.stamp = stamp

    def lighter_snapshot(self, data: dict[str, Any], stamp: Stamp) -> None:
        body = data.get("order_book", data)
        self.invalidate()
        self._levels(body.get("bids", []), body.get("asks", []))
        self.sequence = int(body["nonce"])
        self.valid = bool(self.bids and self.asks)
        self.updated_ns = stamp.monotonic_ns
        self.stamp = stamp

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
        self.stamp = stamp

    def quote(self) -> Quote | None:
        if not self.valid or not self.bids or not self.asks:
            return None
        bid, ask = max(self.bids), min(self.asks)
        if bid <= 0 or ask <= bid:
            return None
        return Quote(bid, ask, self.bids[bid], self.asks[ask], self.stamp)

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
        self.run_id = uuid.uuid4().hex
        self.segment = 0
        self.last_flush = time.monotonic()

    def write(self, event: MarketEvent) -> None:
        hour = time.strftime("%Y%m%d-%H", time.gmtime())
        if hour != self.hour:
            if self.handle:
                self.handle.close()
            self.segment += 1
            # Never append a new deflate stream after an unclean older process exit.
            name = f"market-{hour}-{self.run_id}-{self.segment:04d}.jsonl.gz"
            self.handle = gzip.open(self.directory / name, "xt")
            self.hour = hour
        self.handle.write(
            json.dumps(
                {
                    "venue": event.venue,
                    "kind": event.kind,
                    "payload": event.payload,
                    "receipt_ns": event.stamp.receipt_ns,
                    "exchange_ms": event.stamp.exchange_ms,
                    "exchange_event_ms": event.stamp.exchange_event_ms,
                    "monotonic_ns": event.stamp.monotonic_ns,
                    "queued_monotonic_ns": event.queued_monotonic_ns,
                    "recorded_ns": time.time_ns(),
                    "recorded_monotonic_ns": time.monotonic_ns(),
                    "run_id": self.run_id,
                    "pid": os.getpid(),
                    "connection_id": event.connection_id,
                }
            )
            + "\n"
        )
        if time.monotonic() - self.last_flush >= 1:
            self.handle.flush()
            self.last_flush = time.monotonic()

    def close(self) -> None:
        if self.handle:
            self.handle.close()


class Feeds:
    def __init__(
        self,
        market_id: int,
        queue: asyncio.Queue[MarketEvent],
        dns_mode: DnsMode = "system",
        telemetry: WebsocketTrace | None = None,
    ):
        self.telemetry = telemetry
        self.market_id = market_id
        self.queue = queue
        self._emitted_since_yield = 0
        self._emit_yield_every = min(64, max(1, queue.maxsize // 4)) if queue.maxsize else 64
        self.dns_mode = dns_mode
        self.reconnects = {"binance": 0, "lighter": 0}
        self.binance_public_ok = False
        self.binance_trades_ok = False
        self.transport_trace = TransportTrace()
        self.channels: dict[str, dict[str, Any]] = {
            name: {
                "connected": False,
                "reconnects": 0,
                "consecutive_failures": 0,
                "retry_delay_s": 0,
                "last_error": None,
                "last_disconnect_at": None,
                "last_transport_detail": None,
                "_started": None,
                "_last_frame": None,
            }
            for name in ("binance_public", "binance_market", "lighter")
        }

    def channel_started(self, name: str) -> None:
        self.channels[name].update(connected=True, retry_delay_s=0, _started=time.monotonic())

    def connection(self, name: str, url: str):
        connection = self.telemetry.connect(name, url) if self.telemetry else None
        self.channels[name]["connection_id"] = connection.id if connection else None
        return connection

    def channel_data(self, name: str) -> None:
        now = time.monotonic()
        state = self.channels[name]
        state["_last_frame"] = now
        if state["_started"] is not None and now - state["_started"] >= 30:
            # Retry delay follows consecutive failures, not lifetime reconnect totals.
            state["consecutive_failures"] = 0

    async def reconnect(self, venue: str, channel: str, exc: Exception) -> None:
        name = f"binance_{channel}" if venue == "binance" else "lighter"
        state = self.channels[name]
        state["connected"] = False
        state["reconnects"] += 1
        state["consecutive_failures"] += 1
        delay = min(2 ** min(state["consecutive_failures"], 5), 30)
        state.update(
            retry_delay_s=delay, last_error=type(exc).__name__, last_disconnect_at=time.time()
        )
        detail = {
            "close_code": getattr(exc, "ws_close_code", None),
            "frame_type": getattr(exc, "ws_frame_type", None),
            "cause": getattr(exc, "ws_cause", None),
            "exception_message": str(exc),
            "cause_message": str(exc.__cause__) if exc.__cause__ is not None else None,
            "exception_chain": "".join(traceback.format_exception(exc)),
        }
        state["last_transport_detail"] = detail
        if self.telemetry:
            self.telemetry.event(
                "disconnect",
                connection_id=state.get("connection_id"),
                channel=name,
                exception_type=type(exc).__name__,
                retry_delay_s=delay,
            )
        self.reconnects[venue] += 1
        await self.emit(
            venue,
            "disconnect",
            {
                "reason": type(exc).__name__,
                "channel": channel,
                "retry_delay_s": delay,
                "consecutive_failures": state["consecutive_failures"],
                "transport_detail": detail,
            },
        )
        await asyncio.sleep(delay)

    def channel_status(self) -> dict:
        now = time.monotonic()
        return {
            name: {
                **{k: v for k, v in s.items() if not k.startswith("_")},
                "last_frame_age_ms": round((now - s["_last_frame"]) * 1000)
                if s["_last_frame"] is not None
                else None,
            }
            for name, s in self.channels.items()
        }

    async def emit(
        self,
        venue: str,
        kind: str,
        payload: dict[str, Any],
        exchange_ms: int | None = None,
        *,
        stamp: Stamp | None = None,
    ) -> None:
        stamp = stamp or Stamp(exchange_ms=exchange_ms)
        if stamp.exchange_ms is None and exchange_ms is not None:
            stamp = replace(stamp, exchange_ms=exchange_ms)
        connection_id = CURRENT_CONNECTION.get()
        if kind == "disconnect":
            name = "lighter" if venue == "lighter" else "binance_" + str(payload.get("channel"))
            connection_id = self.channels.get(name, {}).get("connection_id")
        event = MarketEvent(venue, kind, payload, stamp, time.monotonic_ns(), connection_id)
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
        self._emitted_since_yield += 1
        if self._emitted_since_yield >= self._emit_yield_every:
            # Buffered socket reads and put_nowait need not suspend. Give the
            # consumer (and protection/exit tasks) a turn without restamping data.
            self._emitted_since_yield = 0
            await asyncio.sleep(0)

    async def binance(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._binance_public())
            group.create_task(self._binance_trades())

    async def _binance_public(self) -> None:
        while True:
            connection = self.connection("binance_public", BINANCE_PUBLIC_WS)
            observed: dict[str, Any] = (
                {"ws_response_class": partial(ObservedAiohttpSocket, evidence=connection)}
                if connection
                else {}
            )
            try:
                async with (
                    binance_session(
                        self.dns_mode, trace_configs=[self.transport_trace.config()], **observed
                    ) as client,
                    client.ws_connect(BINANCE_PUBLIC_WS, heartbeat=20) as ws,
                    connection.scope() if connection else nullcontext(),
                ):
                    if connection:
                        connection.handshake_completed(ws, "aiohttp")
                    self.channel_started("binance_public")
                    buffered: list[tuple[dict[str, Any], Stamp]] = []
                    snap_task = asyncio.create_task(self._binance_snapshot(client))
                    try:
                        while not snap_task.done():
                            try:
                                raw = await ws.receive_str(timeout=1)
                            except TimeoutError:
                                continue
                            received = Stamp()
                            if connection:
                                connection.frame("application", "TEXT", len(raw.encode()))
                            msg = json.loads(raw)
                            data = msg.get("data", msg)
                            buffered.append(
                                (
                                    data,
                                    replace(
                                        received,
                                        exchange_ms=data.get("T"),
                                        exchange_event_ms=data.get("E"),
                                    ),
                                )
                            )
                            if len(buffered) > 2048:
                                raise GapError("Binance bootstrap buffer overflow")
                        snapshot = await snap_task
                        await self.emit(
                            "binance",
                            "snapshot",
                            snapshot,
                            snapshot.get("T"),
                            stamp=replace(Stamp(), exchange_event_ms=snapshot.get("E")),
                        )
                    finally:
                        if not snap_task.done():
                            snap_task.cancel()
                        await asyncio.gather(snap_task, return_exceptions=True)
                    for data, stamp in buffered:
                        if await self._binance_message(data, stamp=stamp):
                            self.channel_data("binance_public")
                    self.binance_public_ok = True
                    async for frame in ws:
                        received = Stamp()
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            raise websocket_closed(ws, "public", frame.type.name)
                        message = json.loads(frame.data)
                        if connection:
                            connection.frame("application", "TEXT", len(frame.data))
                        if await self._binance_message(
                            message.get("data", message), stamp=received
                        ):
                            self.channel_data("binance_public")
                    raise websocket_closed(ws, "public")
            except asyncio.CancelledError:
                if connection:
                    connection.begin_close("task_cancellation")
                self.binance_public_ok = False
                self.channels["binance_public"]["connected"] = False
                raise
            except Exception as exc:
                if connection:
                    connection.failure(exc)
                self.binance_public_ok = False
                await self.reconnect("binance", "public", exc)

    async def _binance_trades(self) -> None:
        while True:
            connection = self.connection("binance_market", BINANCE_MARKET_WS)
            observed: dict[str, Any] = (
                {"ws_response_class": partial(ObservedAiohttpSocket, evidence=connection)}
                if connection
                else {}
            )
            try:
                async with (
                    binance_session(
                        self.dns_mode, trace_configs=[self.transport_trace.config()], **observed
                    ) as client,
                    client.ws_connect(BINANCE_MARKET_WS, heartbeat=20) as ws,
                    connection.scope() if connection else nullcontext(),
                ):
                    if connection:
                        connection.handshake_completed(ws, "aiohttp")
                    self.channel_started("binance_market")
                    self.binance_trades_ok = True
                    async for frame in ws:
                        received = Stamp()
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            raise websocket_closed(ws, "market", frame.type.name)
                        message = json.loads(frame.data)
                        if connection:
                            connection.frame("application", "TEXT", len(frame.data))
                        if await self._binance_message(
                            message.get("data", message), stamp=received
                        ):
                            self.channel_data("binance_market")
                    raise websocket_closed(ws, "market")
            except asyncio.CancelledError:
                if connection:
                    connection.begin_close("task_cancellation")
                self.binance_trades_ok = False
                self.channels["binance_market"]["connected"] = False
                raise
            except Exception as exc:
                if connection:
                    connection.failure(exc)
                self.binance_trades_ok = False
                await self.reconnect("binance", "market", exc)

    async def _binance_snapshot(self, client: aiohttp.ClientSession) -> dict[str, Any]:
        async with client.get(BINANCE_DEPTH) as response:
            response.raise_for_status()
            return await response.json()

    async def _binance_message(self, data: dict[str, Any], *, stamp: Stamp | None = None) -> bool:
        if data.get("s") != "BTCUSDT" or data.get("st", 1) != 1:
            return False
        kind = data.get("e")
        if kind in {"depthUpdate", "bookTicker", "aggTrade"}:
            stamp = stamp or Stamp()
            if stamp.exchange_ms != data.get("T") or stamp.exchange_event_ms != data.get("E"):
                stamp = replace(stamp, exchange_ms=data.get("T"), exchange_event_ms=data.get("E"))
            await self.emit("binance", kind, data, data.get("T"), stamp=stamp)
            return True
        return False

    async def lighter(self) -> None:
        while True:
            connection = self.connection("lighter", LIGHTER_WS)
            observed: dict[str, Any] = (
                {"create_connection": partial(ObservedLighterSocket, evidence=connection)}
                if connection
                else {}
            )
            try:
                async with (
                    websockets.connect(
                        LIGHTER_WS, ping_interval=30, max_queue=256, open_timeout=10, **observed
                    ) as ws,
                    connection.scope() if connection else nullcontext(),
                ):
                    if connection:
                        connection.handshake_completed(ws, "websockets")
                    self.channel_started("lighter")
                    for channel in (
                        f"order_book/{self.market_id}",
                        f"trade/{self.market_id}",
                        f"ticker/{self.market_id}",
                    ):
                        await ws.send(json.dumps({"type": "subscribe", "channel": channel}))
                    if connection:
                        connection.event("subscriptions_sent", subscription_count=3)
                    async for raw in ws:
                        received = Stamp()
                        if connection:
                            connection.frame("application", "TEXT", len(raw))
                        data = json.loads(raw)
                        channel = str(data.get("channel", ""))
                        if channel.startswith(("order_book:", "trade:", "ticker:")):
                            self.channel_data("lighter")
                            await self.emit(
                                "lighter",
                                channel.split(":")[0],
                                data,
                                data.get("timestamp"),
                                stamp=received,
                            )
                    raise ConnectionError("Lighter stream ended")
            except asyncio.CancelledError:
                if connection:
                    connection.begin_close("task_cancellation")
                self.channels["lighter"]["connected"] = False
                raise
            except Exception as exc:
                if connection:
                    connection.failure(exc)
                await self.reconnect("lighter", "ws", exc)
