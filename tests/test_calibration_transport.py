"""Real localhost HTTP transport only: no exchange, signer, or order imports."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

import lldesk.source_time as source
from lldesk.clock_trace import calibration_request, clock_trace, transport_record
from lldesk.models import Stamp
from lldesk.network import BinanceResolver, binance_session

HOST = {"synchronized": True, "host_error_ms": 1}


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_get("/time", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/time"
    finally:
        await runner.cleanup()


async def get(client, url):
    attempt = {"transport": transport_record()}
    async with client.get(url, trace_request_ctx=attempt) as response:
        await response.read()
    return attempt["transport"]


@pytest.mark.asyncio
async def test_real_connection_reused_across_actual_production_cadence(monkeypatch):
    transports, doh_transports = [], []

    async def handle(request):
        if "name" in request.query:
            doh_transports.append(request.transport)
            return web.json_response(
                {"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8", "TTL": 7}]}
            )
        transports.append(request.transport)
        return web.json_response({"serverTime": source.time.time_ns() // 1_000_000})

    async with server(handle) as url:
        monkeypatch.setattr("lldesk.network.DNS_PROVIDERS", (url,))
        resolver = BinanceResolver("doh", keepalive_timeout=source.CALIBRATION_KEEPALIVE_S)
        async with binance_session(
            trace_configs=[clock_trace()], keepalive_timeout=source.CALIBRATION_KEEPALIVE_S
        ) as client:
            assert client.connector._keepalive_timeout == 75
            try:
                first = await get(client, url)
                await resolver.lookup("fapi.binance.com")
                # Real elapsed time, not mocked _get(): baseline 15s cannot retain this.
                await asyncio.sleep(source.CLOCK_INTERVAL_S + 0.1)
                second = await get(client, url)
                _, ttl = await resolver.lookup("fapi.binance.com")
                assert ttl == 7
            finally:
                await resolver.close()
    assert transports[0] is transports[1]
    assert doh_transports[0] is doh_transports[1]
    assert first["connection_reused"] is False and second["connection_reused"] is True
    assert second["http_send_attempts"] == second["connection_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal_close", "stale_retry"])
async def test_server_closure_and_library_retry_are_observed(mode):
    calls = []

    async def handle(request):
        calls.append(request.transport)
        response = web.json_response({"serverTime": source.time.time_ns() // 1_000_000})
        if mode == "normal_close":
            response.force_close()
        elif len(calls) == 2:
            request.transport.abort()  # GET was received, but no response headers.
        return response

    async with server(handle) as url:
        async with binance_session(trace_configs=[clock_trace()], keepalive_timeout=75) as client:
            await get(client, url)
            second = await get(client, url)
    if mode == "stale_retry":
        assert len(calls) == 3
        assert second["http_send_attempts"] == second["connection_attempts"] == 2
        assert [e["phase"] for e in second["events"]].count("request_start") == 1
    else:
        assert len(calls) == 2 and calls[0] is not calls[1]
        assert second["http_send_attempts"] == 1
    assert second["connection_reused"] is False


@pytest.mark.asyncio
async def test_other_sessions_and_doh_ttl_keep_their_defaults(monkeypatch):
    async with binance_session() as client:
        assert client.connector._keepalive_timeout == 15

    async def handle(request):
        return web.json_response(
            {"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8", "TTL": 7}]}
        )

    async with server(handle) as url:
        monkeypatch.setattr("lldesk.network.DNS_PROVIDERS", (url,))
        for keepalive, expected in [(None, 15), (75, 75)]:
            resolver = BinanceResolver("doh", keepalive_timeout=keepalive)
            parent = {}
            token = calibration_request.set(parent) if keepalive else None
            try:
                _, ttl = await resolver.lookup("fapi.binance.com")
                assert ttl == 7
                assert resolver.client.connector._keepalive_timeout == expected
                if keepalive:
                    assert parent["resolver_http_attempts"][0]["http_attempts"] == 1
                    assert parent["resolver_http_attempts"][0]["http_retries"] == 0
            finally:
                if token is not None:
                    calibration_request.reset(token)
                await resolver.close()
            assert resolver.client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error", "slow_rtt", "timeout", "cancel"])
async def test_failed_or_cancelled_calibration_preserves_original_proof(monkeypatch, mode):
    calls = 0
    second_request = asyncio.Event()
    captures = []
    ready = asyncio.Event()

    async def handle(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            second_request.set()
            if mode == "slow_rtt":
                await asyncio.sleep(0.45)
            elif mode in {"timeout", "cancel"}:
                await asyncio.sleep(2.05)
        return web.json_response(
            {"serverTime": source.time.time_ns() // 1_000_000},
            status=500 if calls > 1 and mode == "error" else 200,
        )

    def record(row):
        captures.append(row)
        if len(captures) == 2:
            ready.set()

    async with server(handle) as url:
        monkeypatch.setattr(source, "TIME_URL", url)
        monkeypatch.setattr(source, "chrony_proof", lambda: HOST)
        monkeypatch.setattr(source, "CLOCK_INTERVAL_S", 0.05)  # isolated test only
        clock = source.BinanceSourceClock(1500)
        worker = asyncio.create_task(clock.run("system", record))
        try:
            await asyncio.wait_for(second_request.wait(), 2)
            original = clock.evidence
            assert original is not None
            if mode == "cancel":
                worker.cancel()
            await asyncio.wait_for(ready.wait(), 3)
            if mode != "cancel":
                worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        assert clock.evidence is original
        attempt = captures[1]["attempt"]
        expected = {"slow_rtt": "clock_unverified", "cancel": "cancelled"}.get(mode, "failed")
        assert attempt["outcome"] == expected
        assert attempt["http_attempts"] == 1 and attempt["http_retries"] == 0
        assert clock.request_weight == 2
        assert source.CLOCK_TTL_S == 120
        stamp = original.received
        result = clock.assess(
            Stamp(
                receipt_ns=stamp.receipt_ns,
                monotonic_ns=stamp.monotonic_ns,
                exchange_ms=original.server_ms,
            ),
            now_ns=stamp.monotonic_ns + 121_000_000_000,
            wall_ns=stamp.receipt_ns + 121_000_000_000,
        )
        assert result["reason"] == "SOURCE_CLOCK_EVIDENCE_EXPIRED"


@pytest.mark.asyncio
async def test_worker_budgets_automatic_retry_without_second_calibration(monkeypatch):
    calls = 0
    ready = asyncio.Event()
    captures = []

    async def handle(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            request.transport.abort()
        return web.json_response({"serverTime": source.time.time_ns() // 1_000_000})

    def record(row):
        captures.append(row)
        if len(captures) == 2:
            ready.set()

    async with server(handle) as url:
        monkeypatch.setattr(source, "TIME_URL", url)
        monkeypatch.setattr(source, "chrony_proof", lambda: HOST)
        monkeypatch.setattr(source, "CLOCK_INTERVAL_S", 0.05)
        clock = source.BinanceSourceClock(1500)
        worker = asyncio.create_task(clock.run("system", record))
        try:
            await asyncio.wait_for(ready.wait(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    assert clock.requests == 2 and calls == 3
    assert clock.request_weight == 3
    assert captures[1]["attempt"]["http_attempts"] == 2
    assert captures[1]["attempt"]["http_retries"] == 1
    assert captures[1]["attempt"]["weight"] == 2
