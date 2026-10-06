"""Production library hooks with fake transport only; no sockets/signers/broadcast."""

import ast
import asyncio
import hashlib
import inspect
import json
import textwrap
import threading
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import pytest
from aiohttp.client_ws import ClientWSTimeout
from aiohttp.http import WSMessage
from aiohttp.streams import EofStream
from test_probe_only import engine as engine_fixture
from test_source_time import restore_real_guards
from websockets.asyncio.client import ClientConnection
from websockets.client import ClientProtocol
from websockets.frames import Frame, Opcode
from websockets.uri import parse_uri

from lldesk.market import Feeds
from lldesk.models import EngineState
from lldesk.websocket_trace import (
    KEEPALIVE_SOURCE_SHA256,
    ObservedAiohttpSocket,
    ObservedLighterSocket,
    WebsocketTrace,
    observed_timeout,
)

engine = engine_fixture


@pytest.fixture
def telemetry(tmp_path):
    return WebsocketTrace(tmp_path, "fake-run", capacity=16)


class Reader:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def read(self):
        value = await self.queue.get()
        if isinstance(value, Exception):
            raise value
        return value

    def feed_data(self, value, size=0):
        self.queue.put_nowait(value)


class Writer:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def send_frame(self, message, opcode, *args, **kwargs):
        self.calls.append(opcode)

    async def close(self, *args):
        if self.error:
            raise self.error


def socket(telemetry, *, error=None):
    evidence = telemetry.connect("binance_public", "wss://example.invalid/public")
    reader, writer = Reader(), Writer(error)
    response = SimpleNamespace(connection=None, close=Mock())
    ws = ObservedAiohttpSocket(
        reader,
        writer,
        None,
        response,
        ClientWSTimeout(ws_close=0.001),
        True,
        True,
        asyncio.get_running_loop(),
        heartbeat=20,
        evidence=evidence,
    )
    return ws, evidence, reader, writer


async def test_real_aiohttp_eof_precedes_cleanup_error(telemetry):
    ws, evidence, reader, writer = socket(
        telemetry, error=aiohttp.ClientConnectionResetError("Cannot write to closing transport")
    )
    reader.feed_data(EofStream())
    result = await ws.receive()
    assert result.type == aiohttp.WSMsgType.CLOSED
    assert evidence.first_close["cause"] == "transport_eof"
    assert isinstance(ws.exception(), aiohttp.ClientConnectionResetError)
    kinds = [r.get("cause") for r in telemetry.recent if r["kind"].startswith("close_")]
    assert kinds.index("transport_eof") < kinds.index("cleanup_writer_exception")
    assert writer.calls == []


async def test_autoping_hides_pong_from_app_but_hook_observes_library_dequeue(telemetry):
    ws, evidence, reader, writer = socket(telemetry)
    reader.feed_data(WSMessage(aiohttp.WSMsgType.PONG, b"fixture-secret", ""))
    reader.feed_data(WSMessage(aiohttp.WSMsgType.TEXT, "quote", ""))
    result = await ws.receive()
    assert result.data == "quote" and ws._autoping
    assert evidence.frames["library_dequeue:PONG"] == 1
    assert evidence.frames["application:PONG"] == 0
    assert "fixture-secret" not in json.dumps(telemetry.status())
    ws._cancel_heartbeat()


async def test_real_heartbeat_callback_early_reschedules_and_late_sends_one_ping(telemetry):
    ws, evidence, reader, writer = socket(telemetry)
    ws._heartbeat_when = ws._loop.time() + 20
    ws._send_heartbeat()
    assert writer.calls == []  # Native early callback retains its reschedule branch.
    ws._cancel_heartbeat()
    ws._heartbeat_when = ws._loop.time() - 0.02
    ws._heartbeat_cb = ws._loop.call_at(ws._heartbeat_when, lambda: None)
    ws._send_heartbeat()
    assert writer.calls == [aiohttp.WSMsgType.PING]
    callbacks = [r for r in telemetry.recent if r["kind"] == "heartbeat_callback"]
    assert callbacks[-1]["callback_lag_ms"] >= 15
    assert any(r["kind"] == "pong_deadline" for r in telemetry.recent)
    ws._cancel_heartbeat()


async def test_real_pong_timeout_callback_distinct_from_delayed_timer(telemetry):
    ws, evidence, _, _ = socket(telemetry)
    ws._pong_response_cb = ws._loop.call_at(ws._loop.time() - 0.02, lambda: None)
    ws._pong_not_received()
    assert evidence.first_close["cause"] == "library_heartbeat"
    assert isinstance(ws.exception(), aiohttp.ServerTimeoutError)
    event = next(r for r in telemetry.recent if r["kind"] == "pong_timeout_callback")
    assert event["callback_lag_ms"] >= 15


async def test_cancel_and_normal_context_exit_preserve_first_cause(telemetry):
    ws, evidence, _, _ = socket(telemetry)
    task = asyncio.create_task(ws.receive())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ws.close()
    assert evidence.first_close["cause"] == "task_cancellation"
    other = telemetry.connect("lighter_account", "wss://example.invalid/stream")
    async with other.scope():
        other.begin_close("application_auth_rotation")
    assert other.first_close["cause"] == "application_auth_rotation"


async def test_websockets_protocol_pong_and_close_observed_before_app(telemetry):
    evidence = telemetry.connect("lighter", "wss://example.invalid/stream")
    ws = ObservedLighterSocket(
        ClientProtocol(parse_uri("wss://example.invalid")), evidence=evidence
    )
    ws.response = SimpleNamespace()  # Handshake complete; fake assembler, no network.
    ws.recv_messages = SimpleNamespace(put=Mock())
    pong = asyncio.get_running_loop().create_future()
    ws.pending_pings[b"opaque-ping"] = (pong, ws.loop.time())
    ws.process_event(Frame(Opcode.PONG, b"opaque-ping"))
    assert pong.done() and not ws.pending_pings
    assert any(r.get("matched") for r in telemetry.recent if r["kind"] == "pong_observed")
    ws.process_event(Frame(Opcode.CLOSE, b"\x03\xe8"))
    assert evidence.first_close["cause"] == "remote_close_frame"
    assert "opaque-ping" not in json.dumps(telemetry.status())


async def test_native_timeout_hook_preserves_cancel_conversion_and_deadline(telemetry):
    evidence = telemetry.connect("lighter", "wss://example.invalid/stream")
    with pytest.raises(TimeoutError):
        async with observed_timeout(0.001, evidence):
            await asyncio.sleep(1)
    assert asyncio.current_task().cancelling() == 0
    events = [r for r in telemetry.recent if r["kind"] == "pong_timeout_callback"]
    assert len(events) == 1 and events[0]["callback_lag_ms"] >= 0


def test_keepalive_control_flow_matches_installed_library_after_removing_observation():
    source = inspect.getsource(ClientConnection.keepalive)
    assert hashlib.sha256(source.encode()).hexdigest() == KEEPALIVE_SOURCE_SHA256

    class StripHooks(ast.NodeTransformer):
        def visit_ExceptHandler(self, node):
            assert asyncio.TimeoutError is TimeoutError
            if ast.unparse(node.type) == "asyncio.TimeoutError":
                node.type = ast.Name(id="TimeoutError", ctx=ast.Load())
            return self.generic_visit(node)

        def visit_Expr(self, node):
            if isinstance(node.value, ast.Constant):
                return None  # Docstring.
            if isinstance(node.value, ast.Call) and ast.unparse(node.value.func).startswith(
                "self.evidence."
            ):
                return None
            return self.generic_visit(node)

        def visit_Assign(self, node):
            if any(isinstance(t, ast.Name) and t.id == "due" for t in node.targets):
                return None
            return self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "observed_timeout":
                node.func.id = "asyncio_timeout"
                node.args = node.args[:1]
            return self.generic_visit(node)

    def behavior(source):
        function = ast.parse(textwrap.dedent(source)).body[0]
        # Drop only the hash/fallback wrapper before the original initial assertion.
        first = next(i for i, n in enumerate(function.body) if isinstance(n, ast.Assert))
        return ast.dump(StripHooks().visit(ast.Module(body=function.body[first:], type_ignores=[])))

    assert behavior(source) == behavior(inspect.getsource(ObservedLighterSocket.keepalive))


async def test_lighter_keepalive_timeout_uses_original_fail_and_cancellation(
    telemetry, monkeypatch
):
    evidence = telemetry.connect("lighter", "wss://example.invalid/stream")
    ws = ObservedLighterSocket(
        ClientProtocol(parse_uri("wss://example.invalid")),
        ping_interval=0.001,
        ping_timeout=0.001,
        evidence=evidence,
    )
    pings = []

    async def ping():
        pings.append(1)
        return asyncio.get_running_loop().create_future()

    @asynccontextmanager
    async def context():
        yield
        raise asyncio.CancelledError  # Original keepalive waits for connection_lost.

    fail = Mock()
    monkeypatch.setattr(ws, "ping", ping)
    monkeypatch.setattr(ws, "send_context", context)
    monkeypatch.setattr(ws.protocol, "fail", fail)
    with pytest.raises(asyncio.CancelledError):
        await ws.keepalive()
    assert len(pings) == 1
    assert fail.call_args.args == (1011, "keepalive ping timeout")
    assert evidence.first_close["cause"] == "library_heartbeat"
    assert any(r["kind"] == "pong_timeout_callback" for r in telemetry.recent)


async def test_recovery_events_link_snapshot_bridge_guard_and_coverage(engine):
    restore_real_guards(engine)
    engine.config.allow_live_orders = False
    engine.state = EngineState.HALTED
    engine.feeds = Feeds(1, engine.queue, telemetry=engine.ws_telemetry)
    engine.feeds.binance_public_ok = engine.feeds.binance_trades_ok = True
    connection = engine.feeds.connection("binance_public", "wss://example.invalid/public")
    engine.status()  # Coverage initially false; doesn't add samples.
    async with connection.scope():
        await engine.feeds.emit(
            "binance",
            "snapshot",
            {
                "lastUpdateId": 100,
                "bids": [["83000", "2"]],
                "asks": [["83000.1", "2"]],
            },
            time.time_ns() // 1_000_000 - 200,
        )
        engine.on_market(await engine.queue.get())
        await engine.feeds._binance_message(
            {
                "e": "depthUpdate",
                "s": "BTCUSDT",
                "U": 99,
                "u": 102,
                "pu": 98,
                "T": time.time_ns() // 1_000_000 - 200,
                "b": [],
                "a": [],
            }
        )
        engine.on_market(await engine.queue.get())
        now = time.monotonic()
        engine.strategy.samples.extend((now - 600 + i, Decimal(0)) for i in range(600))
        engine.decide()
        engine.status()
    events = list(engine.ws_telemetry.recent)
    stages = [r["kind"] for r in events]
    assert stages.index("snapshot_applied") < stages.index("depth_bridged")
    assert stages.index("depth_bridged") < stages.index("receipt_source_valid")
    coverage = [r for r in events if r["kind"] == "coverage_observed"]
    assert not coverage[0]["coverage"]["ready"] and coverage[-1]["coverage"]["ready"]
    assert all(
        r["connection_id"] == connection.id
        for r in events
        if r["kind"] in {"snapshot_applied", "depth_bridged"}
    )
    assert engine.last_decision_inputs["data_guard"]["passed"]
    assert engine.account.sent == []


async def test_slow_writer_and_overflow_do_not_hold_event_loop(telemetry, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    original = telemetry._write

    def slow(handle, row):
        started.set()
        gate.wait(1)
        original(handle, row)

    monkeypatch.setattr(telemetry, "_write", slow)
    task = asyncio.create_task(telemetry.run(lambda: {}))
    telemetry.event("test")
    await asyncio.to_thread(started.wait, 0.5)
    assert started.is_set()
    for _ in range(50):
        telemetry.event("test")
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    assert not gate.is_set() and telemetry.dropped > 0
    assert telemetry.queue.qsize() <= telemetry.queue.maxsize
    gate.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_frame_volume_coalesced_and_pulse_history_does_not_recurse(telemetry):
    evidence = telemetry.connect("binance_public", "wss://example.invalid/public")
    count = telemetry.sequence
    for _ in range(1000):
        evidence.frame("library_dequeue", "TEXT", 100)
    assert telemetry.sequence == count
    assert evidence.frames["library_dequeue:TEXT"] == 1000
    for _ in range(20):
        telemetry.event("pulse", telemetry=telemetry.status())
    assert all("telemetry" not in r for r in telemetry.status()["recent"])
    assert len(json.dumps(telemetry.status())) < 20000
