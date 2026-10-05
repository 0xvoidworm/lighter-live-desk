"""Installed SDK HTTP transport against localhost; no keys, orders or mainnet."""

import asyncio
import gzip
import json
import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import aiohttp
import lighter
import pytest
from aiohttp import web

from lldesk.market import Book, Feeds, Recorder, websocket_closed
from lldesk.models import MarketEvent, Quote, Stamp
from lldesk.strategy import DeviationStrategy
from lldesk.transport_trace import READ_PATHS, TransportTrace


@pytest.mark.parametrize("path", ["deposit/history", "withdraw/history", "transfer/history"])
async def test_installed_sdk_funding_routes_are_traced(path):
    import inspect

    # Match the installed generated SDK's route, then exercise its real HTTP transport.
    assert f"/api/v1/{path}" in inspect.getsource(lighter.TransactionApi)
    assert f"/api/v1/{path}" in READ_PATHS
    app = web.Application()

    async def handler(request):
        return web.json_response({"code": 200})

    app.router.add_get(f"/api/v1/{path}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = lighter.ApiClient(lighter.Configuration(host=f"http://127.0.0.1:{port}"))
    trace = TransportTrace()
    trace.install(client.rest_client)
    try:
        response = await client.rest_client.request("GET", f"http://127.0.0.1:{port}/api/v1/{path}")
        await response.read()
        assert trace.completed[-1]["path"] == f"/api/v1/{path}"
        assert trace.completed[-1]["outcome"] == "ok"
    finally:
        await client.close()
        await runner.cleanup()


async def test_bootstrap_buffer_does_not_make_old_depth_fresh():
    queue = asyncio.Queue()
    feeds = Feeds(1, queue)
    source = Stamp(exchange_ms=1234, monotonic_ns=time.monotonic_ns() - 10_000_000_000)
    assert await feeds._binance_message(
        {
            "s": "BTCUSDT",
            "e": "depthUpdate",
            "T": 1234,
            "U": 10,
            "u": 11,
            "pu": 9,
            "b": [],
            "a": [],
        },
        stamp=source,
    )
    event = queue.get_nowait()
    book = Book("binance")
    book.binance_snapshot(
        {"lastUpdateId": 10, "bids": [["99", "1"]], "asks": [["101", "1"]]}, Stamp()
    )
    book.binance_delta(event.payload, event.stamp)
    assert event.stamp is source and book.quote().stamp is source
    assert (time.monotonic_ns() - book.updated_ns) / 1_000_000 > 1500


@pytest.mark.parametrize("stall", ["headers", "body"])
async def test_installed_sdk_transport_cancellation_phase_and_redaction(stall):
    release = asyncio.Event()

    async def handler(request):
        if stall == "headers":
            await release.wait()
            return web.json_response({"code": 200})
        response = web.StreamResponse()
        await response.prepare(request)
        await release.wait()
        await response.write(b'{"code":200}')
        return response

    app = web.Application()
    app.router.add_get("/api/v1/account", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = lighter.ApiClient(lighter.Configuration(host=f"http://127.0.0.1:{port}"))
    trace = TransportTrace()
    trace.install(client.rest_client)
    try:
        assert client.rest_client.retry_client is None
        with trace.operation("account_reconciliation", "reconcile", 1), pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                response = await client.rest_client.request(
                    "GET",
                    f"http://127.0.0.1:{port}/api/v1/account?credential=placeholder",
                    headers={"Authorization": "synthetic-placeholder"},
                )
                await response.read()
        result = trace.completed[-1]
        assert result["path"] == "/api/v1/account"
        assert result["reason"] == "account_reconciliation"
        assert result["local_quota_weight"] == 1 and result["http_attempts"] == 1
        assert result["last_phase"] == ("waiting_headers" if stall == "headers" else "reading_body")
        assert not trace.active
        assert "placeholder" not in json.dumps(trace.status())
        assert "credential" not in json.dumps(trace.status())
    finally:
        release.set()
        await client.close()
        await runner.cleanup()


async def test_installed_sdk_local_pool_queue_is_distinct_from_response_wait():
    release = asyncio.Event()
    reached = asyncio.Event()

    async def handler(request):
        reached.set()
        await release.wait()
        return web.json_response({"code": 200})

    app = web.Application()
    app.router.add_get("/api/v1/account", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    config = lighter.Configuration(host=f"http://127.0.0.1:{port}")
    config.connection_pool_maxsize = 1
    client = lighter.ApiClient(config)
    trace = TransportTrace()
    trace.install(client.rest_client)
    url = f"http://127.0.0.1:{port}/api/v1/account"
    first = asyncio.create_task(client.rest_client.request("GET", url))
    try:
        await reached.wait()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await client.rest_client.request("GET", url)
        assert trace.completed[-1]["last_phase"] == "pool_queue"
        release.set()
        response = await first
        await response.read()
        assert trace.completed[-1]["outcome"] == "ok"
        assert not trace.active
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await client.close()
        await runner.cleanup()


async def test_public_http_dns_failure_is_not_mislabeled_tls():
    class Resolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=0):
            raise OSError("synthetic resolver failure")

        async def close(self):
            pass

    trace = TransportTrace()
    connector = aiohttp.TCPConnector(resolver=Resolver())
    async with aiohttp.ClientSession(connector=connector, trace_configs=[trace.config()]) as client:
        with pytest.raises(aiohttp.ClientConnectorError):
            await client.get("https://example.invalid/fapi/v1/depth")
    assert trace.completed[-1]["last_phase"] == "dns"
    assert not trace.active


def test_book_quote_retains_source_stamp_and_duplicate_does_not_refresh():
    book = Book("binance")
    source = Stamp(exchange_ms=1234, receipt_ns=5678, monotonic_ns=9012)
    book.binance_snapshot(
        {"lastUpdateId": 10, "bids": [["99", "1"]], "asks": [["101", "1"]]}, source
    )
    update = Stamp(exchange_ms=1235, receipt_ns=5679, monotonic_ns=9013)
    book.binance_delta({"U": 10, "u": 11, "pu": 9, "b": [], "a": []}, update)
    assert book.quote().stamp is update
    book.binance_delta({"U": 10, "u": 11, "pu": 9, "b": [], "a": []}, Stamp())
    assert book.quote().stamp is update and book.updated_ns == update.monotonic_ns


def test_recorders_never_share_or_append_old_corrupt_gzip(tmp_path):
    corrupt = tmp_path / "market-legacy.jsonl.gz"
    corrupt.write_bytes(b"broken legacy stream")
    first, second = Recorder(tmp_path), Recorder(tmp_path)
    event = MarketEvent("binance", "snapshot", {"lastUpdateId": 1}, Stamp(exchange_ms=123))
    first.write(event)
    second.write(event)
    first.close()
    second.close()
    paths = [p for p in tmp_path.glob("*.gz") if p != corrupt]
    assert len(paths) == 2 and corrupt.read_bytes() == b"broken legacy stream"
    rows = [json.loads(gzip.decompress(p.read_bytes())) for p in paths]
    assert len({r["run_id"] for r in rows}) == 2
    assert all(
        r["exchange_ms"] == 123 and r["monotonic_ns"] == event.stamp.monotonic_ns for r in rows
    )


def test_ws_close_code_and_cause_survive_reconnect_classification():
    error = websocket_closed(
        SimpleNamespace(close_code=1006, exception=lambda: TimeoutError()), "public", "ERROR"
    )
    assert error.ws_close_code == 1006 and error.ws_cause == "TimeoutError"
    assert error.ws_frame_type == "ERROR"


async def test_real_aiohttp_reset_preserves_original_cause(monkeypatch):
    """A local TCP abort reproduces the missing transport evidence; no mainnet."""
    app = web.Application()

    async def handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_str("one received message")
        await asyncio.sleep(0.02)
        request.transport.abort()
        return socket

    app.router.add_get("/public/stream", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/public/stream") as ws:
                assert await ws.receive_str() == "one received message"
                async for _ in ws:
                    pass
                original = ws.exception()
                error = websocket_closed(ws, "public")
                assert original is not None and str(original)
                assert error.__cause__ is original and error.ws_close_code == 1006
                queue = asyncio.Queue()
                feeds = Feeds(0, queue)

                async def no_backoff(_):
                    pass

                monkeypatch.setattr("lldesk.market.asyncio.sleep", no_backoff)
                await feeds.reconnect("binance", "public", error)
                detail = feeds.channels["binance_public"]["last_transport_detail"]
                assert detail["cause_message"] == str(original)
                assert type(original).__name__ in detail["exception_chain"]
                assert detail["frame_type"] is None
    finally:
        await runner.cleanup()


async def test_bootstrap_receive_timeout_does_not_close_live_socket():
    """Exercise the installed aiohttp receive_str timeout, then receive new data."""
    app = web.Application()

    async def handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await asyncio.sleep(1.1)
        await socket.send_str("new book data")
        async for _ in socket:
            pass
        return socket

    app.router.add_get("/public/stream", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(
                f"http://127.0.0.1:{port}/public/stream", heartbeat=20
            ) as ws:
                with pytest.raises(TimeoutError):
                    await ws.receive_str(timeout=1)
                assert not ws.closed
                assert await ws.receive_str(timeout=1) == "new book data"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("side,raw", [("buy", Decimal("-18")), ("sell", Decimal("22"))])
def test_normalized_signal_direction_and_basis_has_no_future_data(monkeypatch, side, raw):
    now = 1000.0
    monkeypatch.setattr("lldesk.strategy.time.monotonic", lambda: now)
    strategy = DeviationStrategy()
    strategy.samples = deque(
        (now - 700 + i, Decimal("2") + Decimal(i % 3) / 100, Decimal("10"), Decimal("10"))
        for i in range(699)
    )
    strategy.samples.append((now + 0.1, Decimal("9999"), Decimal("10"), Decimal("10")))
    strategy.samples.append((now - 0.5, Decimal("8888"), Decimal("10"), Decimal("10")))
    sign = -1 if side == "buy" else 1
    strategy.local_sell_flow = deque(
        [(now - 20, Decimal(sign * 100))] * 3 + [(now - 5, Decimal(sign * 50))] * 3
    )
    fx = Decimal("0.9999")
    comparator = Decimal("10000") / fx
    mid = comparator * (1 + raw / 10000)
    local = Quote(
        mid - Decimal(".001"), mid + Decimal(".001"), Decimal("12.5"), Decimal("12.5"), Stamp()
    )
    remote = Quote(Decimal("9999.999"), Decimal("10000.001"), Decimal("10"), Decimal("10"), Stamp())
    decision = strategy.evaluate(local, remote, True, fx)
    assert abs(decision.deviation_bps - raw) < Decimal("1e-20")
    assert Decimal("2") <= decision.basis_bps <= Decimal("2.01")
    assert decision.side == side
