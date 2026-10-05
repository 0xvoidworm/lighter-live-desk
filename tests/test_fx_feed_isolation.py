"""Real localhost HTTP header delay, fake books/account, no exchange/order calls."""

import asyncio
import json
import time
from types import SimpleNamespace

from aiohttp import web
from test_probe_only import engine as engine_fixture

from lldesk import fx, market
from lldesk.engine import Engine
from lldesk.market import Feeds
from lldesk.models import EngineState, Stamp

engine = engine_fixture


async def test_delayed_fx_headers_do_not_hold_books_account_or_risk_worker(engine, monkeypatch):
    pending, release = asyncio.Event(), asyncio.Event()
    handlers = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                if b"exchangeInfo" in request:
                    payload = {
                        "symbols": [
                            {
                                "symbol": "USDCUSDT",
                                "baseAsset": "USDC",
                                "quoteAsset": "USDT",
                                "status": "TRADING",
                            }
                        ],
                        "rateLimits": [
                            {
                                "rateLimitType": "REQUEST_WEIGHT",
                                "interval": "MINUTE",
                                "intervalNum": 1,
                                "limit": 6000,
                            }
                        ],
                    }
                else:
                    pending.set()
                    await release.wait()  # Withhold actual HTTP headers past native 2s deadline.
                    payload = {
                        "symbol": "USDCUSDT",
                        "bidPrice": "1",
                        "askPrice": "1.00001",
                        "bidQty": "100",
                        "askQty": "100",
                    }
                body = json.dumps(payload).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(fx, "FX_INFO", f"http://127.0.0.1:{port}/exchangeInfo")
    monkeypatch.setattr(fx, "FX_BOOK", f"http://127.0.0.1:{port}/bookTicker")
    engine.config = engine.config.model_copy(update={"allow_live_orders": False})
    engine.state = EngineState.READY
    engine.feeds = SimpleNamespace(binance_public_ok=True, binance_trades_ok=True)
    engine.data_valid = Engine.data_valid.__get__(engine)
    engine.fx_at = time.monotonic() - 16
    engine.books["binance"].bridged = True
    engine.audit_scheduler.next_due = time.monotonic() + 100
    engine.state_scheduler.next_due = time.monotonic() + 0.1
    state_reads, risk_ticks = [], []
    original_state = engine.account.state_snapshot
    original_tick = engine.session_tick

    async def state(*args, **kwargs):
        state_reads.append(time.monotonic())
        return await original_state(*args, **kwargs)

    def tick(*args, **kwargs):
        if kwargs.get("risk_only"):
            risk_ticks.append(time.monotonic())
        return original_tick(*args, **kwargs)

    engine.account.state_snapshot = state
    engine.session_tick = tick
    feeder = Feeds(1, engine.queue)

    async def books():
        nonce = 1
        while True:
            stamp = Stamp()
            await feeder.emit(
                "binance",
                "depthUpdate",
                {
                    "U": nonce,
                    "u": nonce + 1,
                    "pu": nonce,
                    "b": [["83000", "2"]],
                    "a": [["83000.1", "2"]],
                },
                stamp=stamp,
            )
            await feeder.emit(
                "lighter",
                "order_book",
                {
                    "order_book": {
                        "begin_nonce": nonce,
                        "nonce": nonce + 1,
                        "bids": [{"price": "83000", "size": "2"}],
                        "asks": [{"price": "83000.1", "size": "2"}],
                    }
                },
                stamp=stamp,
            )
            nonce += 1
            await asyncio.sleep(0.02)

    tasks = [
        asyncio.create_task(engine.fx_loop()),
        asyncio.create_task(engine.loop()),
        asyncio.create_task(books()),
    ]
    try:
        await asyncio.wait_for(pending.wait(), 2)
        assert not engine.data_valid()  # Old FX cannot be refreshed by arriving book frames.
        await asyncio.sleep(0.4)
        assert all(
            engine.market_processing[k]["applied"] >= 10
            for k in ("binance:depthUpdate", "lighter:order_book")
        )
        assert state_reads and len(risk_ticks) >= 5
        assert engine.fx_monitor.active["last_phase"] == "http11.receive_response_headers"
        await asyncio.sleep(1.8)
        assert engine.fx_monitor.counts["failures"] == 1 and engine.fx is None
        assert not engine.data_valid()
        assert all(
            engine.market_processing[k]["applied"] >= 50
            for k in ("binance:depthUpdate", "lighter:order_book")
        )
        assert all(
            engine.market_processing[k]["max_queue_wait_ms"] < 200
            for k in ("binance:depthUpdate", "lighter:order_book")
        )
        assert len(risk_ticks) >= 20 and not engine.account.sent and not engine.db.unresolved()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        release.set()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        server.close()
        await server.wait_closed()
        engine.recorder.close()


async def test_processing_telemetry_does_not_refresh_duplicate_or_old_book(engine):
    feeder = Feeds(1, engine.queue)
    before = engine.books["lighter"].stamp
    received = Stamp()
    await feeder.emit(
        "lighter",
        "order_book",
        {
            "order_book": {
                "nonce": 1,
                "begin_nonce": 0,
                "bids": [],
                "asks": [],
            }
        },
        stamp=received,
    )
    event = engine.queue.get_nowait()
    engine.on_market(event)
    assert engine.books["lighter"].stamp is before
    record = engine.market_processing["lighter:order_book"]
    assert record["duplicate_or_old_sequence"] == 1
    assert record["receipt_monotonic_ns"] == received.monotonic_ns
    assert record["quote_monotonic_ns"] == before.monotonic_ns


async def test_delayed_http_headers_with_real_localhost_websocket_receivers(engine, monkeypatch):
    """Exercise aiohttp Binance + websockets Lighter parsing, enqueue and consumption."""
    pending, release = asyncio.Event(), asyncio.Event()

    async def metadata(request):
        return web.json_response(
            {
                "symbols": [
                    {
                        "symbol": "USDCUSDT",
                        "baseAsset": "USDC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    }
                ],
                "rateLimits": [
                    {
                        "rateLimitType": "REQUEST_WEIGHT",
                        "interval": "MINUTE",
                        "intervalNum": 1,
                        "limit": 6000,
                    }
                ],
            }
        )

    async def quote(request):
        pending.set()
        await release.wait()
        return web.json_response(
            {
                "symbol": "USDCUSDT",
                "bidPrice": "1",
                "askPrice": "1.00001",
                "bidQty": "100",
                "askQty": "100",
            }
        )

    async def snapshot(request):
        return web.json_response(
            {"lastUpdateId": 1, "bids": [["83000", "2"]], "asks": [["83000.1", "2"]]}
        )

    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        nonce = 1
        try:
            if request.path == "/lighter":
                for _ in range(3):
                    await ws.receive_json()
            while not ws.closed:
                source_time = time.time_ns() // 1_000_000
                if request.path == "/public":
                    data = {
                        "e": "depthUpdate",
                        "s": "BTCUSDT",
                        "U": nonce,
                        "u": nonce + 1,
                        "pu": nonce,
                        "b": [["83000", "2"]],
                        "a": [["83000.1", "2"]],
                        "T": source_time,
                    }
                elif request.path == "/trades":
                    data = {
                        "e": "aggTrade",
                        "s": "BTCUSDT",
                        "p": "83000",
                        "q": "0.1",
                        "T": source_time,
                    }
                else:
                    data = {
                        "channel": "order_book:1",
                        "timestamp": source_time,
                        "order_book": {
                            "nonce": nonce,
                            "begin_nonce": nonce - 1,
                            "bids": [{"price": "83000", "size": "2"}],
                            "asks": [{"price": "83000.1", "size": "2"}],
                        },
                    }
                await ws.send_json(data)
                nonce += 1
                await asyncio.sleep(0.02)
        except ConnectionError:
            pass
        finally:
            await ws.close()
        return ws

    app = web.Application()
    app.add_routes(
        [
            web.get("/exchangeInfo", metadata),
            web.get("/bookTicker", quote),
            web.get("/depth", snapshot),
            web.get("/public", stream),
            web.get("/trades", stream),
            web.get("/lighter", stream),
        ]
    )
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    origin = f"127.0.0.1:{port}"
    monkeypatch.setattr(fx, "FX_INFO", "http://" + origin + "/exchangeInfo")
    monkeypatch.setattr(fx, "FX_BOOK", "http://" + origin + "/bookTicker")
    monkeypatch.setattr(market, "BINANCE_DEPTH", "http://" + origin + "/depth")
    monkeypatch.setattr(market, "BINANCE_PUBLIC_WS", "ws://" + origin + "/public")
    monkeypatch.setattr(market, "BINANCE_MARKET_WS", "ws://" + origin + "/trades")
    monkeypatch.setattr(market, "LIGHTER_WS", "ws://" + origin + "/lighter")
    engine.config = engine.config.model_copy(update={"allow_live_orders": False})
    engine.fx_at = time.monotonic() - 16
    engine.state = EngineState.READY
    for book in engine.books.values():
        book.invalidate()
    engine.feeds = Feeds(1, engine.queue)
    engine.data_valid = Engine.data_valid.__get__(engine)
    engine.audit_scheduler.next_due = time.monotonic() + 100
    engine.state_scheduler.next_due = time.monotonic() + 100
    tasks = [
        asyncio.create_task(engine.fx_loop()),
        asyncio.create_task(engine.loop()),
        asyncio.create_task(engine.feeds.binance()),
        asyncio.create_task(engine.feeds.lighter()),
    ]
    try:
        await asyncio.wait_for(pending.wait(), 2)
        await asyncio.sleep(0.6)
        assert all(
            engine.market_processing[k]["applied"] >= 10
            for k in ("binance:depthUpdate", "lighter:order_book")
        )
        assert not engine.data_valid() and engine.fx_monitor.active is not None
        await asyncio.sleep(1.6)
        assert engine.fx_monitor.counts["failures"] == 1 and engine.fx is None
        assert all(
            engine.market_processing[k]["applied"] >= 50
            for k in ("binance:depthUpdate", "lighter:order_book")
        )
        assert engine.feeds.reconnects == {"binance": 0, "lighter": 0}
        assert not engine.account.sent and not engine.db.unresolved()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        release.set()
        await runner.cleanup()
        engine.recorder.close()
