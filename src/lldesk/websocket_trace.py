"""Passive, bounded WS evidence. No payloads, order imports or additional pings.

aiohttp hooks observe queue dequeue (not wire arrival); websockets process_event
observes parsed protocol frames. A daemon writer owns all telemetry disk I/O.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import inspect
import json
import os
import queue
import threading
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.streams import EofStream
from websockets.asyncio.client import ClientConnection
from websockets.frames import CloseCode, Frame, Opcode

CURRENT_CONNECTION: ContextVar[str | None] = ContextVar("ws_connection", default=None)


class ConnectionEvidence:
    def __init__(self, owner: WebsocketTrace, identity: str, channel: str):
        self.owner, self.id, self.channel = owner, identity, channel
        self.first_close: dict | None = None
        self.frames: Counter = Counter()
        self.last_frames: dict = {}
        self.ping_count = 0
        self.heartbeat_scheduled_ns: int | None = None
        self.heartbeat_resets = 0
        self.handshake_completed_ns: int | None = None

    def event(self, kind: str, **fields: Any) -> None:
        if kind == "handshake_completed":
            self.handshake_completed_ns = time.monotonic_ns()
        self.owner.event(kind, connection_id=self.id, channel=self.channel, **fields)

    def failure(self, exc: BaseException) -> None:
        if self.handshake_completed_ns is None:
            self.begin_close("handshake_failed", exception_type=type(exc).__name__)
        self.event("connection_exception", exception_type=type(exc).__name__)

    def frame(self, layer: str, kind: str, size: int | None = None) -> None:
        started = time.monotonic_ns()
        self.frames[layer + ":" + kind] += 1
        if size is not None:
            self.frames[layer + ":bytes"] += size
        self.last_frames[layer] = {"kind": kind, "monotonic_ns": started, "wall_ns": time.time_ns()}
        self.owner.hook_ns += time.monotonic_ns() - started
        if kind in {"PING", "PONG", "CLOSE"}:
            self.event("control_frame", layer=layer, frame_type=kind, size=size)
        if kind == "CLOSE":
            self.begin_close("remote_close_frame", layer=layer)

    def begin_close(self, cause: str, **fields: Any) -> None:
        first = self.first_close is None
        detail = {"cause": cause, "monotonic_ns": time.monotonic_ns(), **fields}
        if first:
            self.first_close = detail
        self.event("close_initiated" if first else "close_followup", **detail)

    @asynccontextmanager
    async def scope(self):
        token = CURRENT_CONNECTION.set(self.id)
        try:
            yield
        except asyncio.CancelledError:
            self.begin_close("task_cancellation")
            raise
        except Exception as exc:
            self.begin_close("application_exception", exception_type=type(exc).__name__)
            raise
        finally:
            self.begin_close("application_context_exit")
            CURRENT_CONNECTION.reset(token)

    def summary(self) -> dict:
        return {
            "connection_id": self.id,
            "channel": self.channel,
            "first_close": self.first_close,
            "frames": dict(self.frames),
            "last_frames": dict(self.last_frames),
            "heartbeat_scheduled_ns": self.heartbeat_scheduled_ns,
            "heartbeat_resets": self.heartbeat_resets,
            "handshake_completed_ns": self.handshake_completed_ns,
        }


class WebsocketTrace:
    def __init__(self, directory: Path, run_id: str, *, capacity: int = 512):
        self.directory, self.run_id = directory, run_id
        self.queue: queue.Queue = queue.Queue(maxsize=capacity)
        self.connections: dict[str, ConnectionEvidence] = {}
        self.recent: deque = deque(maxlen=32)
        self.sequence = self.connection_sequence = 0
        self.dropped = self.written = self.bytes_written = self.hook_ns = 0
        self.peak_queue = self.max_write_ns = self.max_queue_age_ns = self.max_loop_lag_ns = 0
        self.write_errors = 0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.context_provider: Any = None
        self.clock_failure: Any = None
        self.writer_loop: asyncio.AbstractEventLoop | None = None

    def connect(self, channel: str, url: str) -> ConnectionEvidence:
        self.connection_sequence += 1
        identity = f"{self.run_id}:{self.connection_sequence}"
        previous = next(
            (c.id for c in reversed(self.connections.values()) if c.channel == channel), None
        )
        connection = ConnectionEvidence(self, identity, channel)
        self.connections[identity] = connection
        # Closed history remains in the file; in-memory summaries are bounded.
        if len(self.connections) > 32:
            oldest = next(iter(self.connections))
            del self.connections[oldest]
        connection.event("handshake_started", url=url, previous_connection_id=previous)
        return connection

    def event(self, kind: str, **fields: Any) -> None:
        started = time.monotonic_ns()
        self.sequence += 1
        row = {
            "event_id": f"{self.run_id}:event:{self.sequence}",
            "kind": kind,
            "wall_ns": time.time_ns(),
            "monotonic_ns": started,
            "run_id": self.run_id,
            "pid": os.getpid(),
            **fields,
        }
        if kind in {"heartbeat_callback", "pong_timeout_callback", "close_initiated", "disconnect"}:
            if row.get("scheduled_ns") is not None:
                row["callback_lag_ms"] = max(0, started - row["scheduled_ns"]) / 1e6
            if self.context_provider is not None:
                try:
                    row["context"] = self.context_provider()
                except Exception as exc:
                    row["context"] = {"unavailable": type(exc).__name__}
        self.recent.append(row)
        try:
            self.queue.put_nowait(row)
            self.peak_queue = max(self.peak_queue, self.queue.qsize())
        except queue.Full:
            self.dropped += 1
            if kind == "source_clock":
                raise RuntimeError("bounded clock evidence queue overflow") from None
        self.hook_ns += time.monotonic_ns() - started

    def status(self) -> dict:
        return {
            "events_emitted": self.sequence,
            "events_written": self.written,
            "events_dropped": self.dropped,
            "write_errors": self.write_errors,
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            "peak_queue_size": self.peak_queue,
            "bytes_written": self.bytes_written,
            "hook_total_ms": self.hook_ns / 1e6,
            "max_write_ms": self.max_write_ns / 1e6,
            "max_queue_age_ms": self.max_queue_age_ns / 1e6,
            "max_loop_lag_ms": self.max_loop_lag_ns / 1e6,
            "connections": [c.summary() for c in self.connections.values()],
            "recent": [
                {k: v for k, v in row.items() if k not in {"context", "telemetry"}}
                for row in self.recent
            ],
        }

    def _write(self, handle, row: dict) -> None:
        started = time.monotonic_ns()
        self.max_queue_age_ns = max(self.max_queue_age_ns, started - row["monotonic_ns"])
        encoded = json.dumps(row, separators=(",", ":")) + "\n"
        handle.write(encoded)
        handle.flush()  # At most sparse heartbeat/pulse events; never on the event loop.
        self.max_write_ns = max(self.max_write_ns, time.monotonic_ns() - started)
        self.bytes_written += len(encoded.encode())
        self.written += 1

    def _writer(self) -> None:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with gzip.open(self.directory / f"transport-{self.run_id}.jsonl.gz", "xt") as handle:
                while not self.stop_event.is_set() or not self.queue.empty():
                    try:
                        row = self.queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    try:
                        self._write(handle, row)
                    except Exception:
                        self.write_errors += 1
                        self.dropped += 1
                        if row["kind"] == "source_clock":
                            self._clock_write_failed()
                    finally:
                        self.queue.task_done()
        except Exception:
            self.write_errors += 1
            self._clock_write_failed()

    def _clock_write_failed(self) -> None:
        if self.clock_failure is not None and self.writer_loop is not None:
            self.writer_loop.call_soon_threadsafe(self.clock_failure)

    async def run(self, context) -> None:
        self.writer_loop = asyncio.get_running_loop()
        self.thread = threading.Thread(target=self._writer, name="ws-telemetry", daemon=True)
        self.thread.start()
        due, pulse = time.monotonic() + 0.5, time.monotonic() + 5
        try:
            while True:
                await asyncio.sleep(max(0, due - time.monotonic()))
                actual = time.monotonic()
                lag_ns = max(0, round((actual - due) * 1e9))
                self.max_loop_lag_ns = max(self.max_loop_lag_ns, lag_ns)
                if lag_ns > 25_000_000:
                    self.event(
                        "loop_timer_lag",
                        scheduled_ns=round(due * 1e9),
                        lag_ms=lag_ns / 1e6,
                        context=context(),
                    )
                if actual >= pulse:
                    self.event("pulse", telemetry=self.status(), context=context())
                    pulse = actual + 5
                due += 0.5
                if due <= actual:
                    due = actual + 0.5  # No catch-up burst; no influence on engine scheduling.
        finally:
            self.stop_event.set()
            await asyncio.to_thread(self.thread.join, 2)


class ObservedReader:
    def __init__(self, reader, evidence: ConnectionEvidence):
        self.reader, self.evidence = reader, evidence

    def __getattr__(self, key):
        return getattr(self.reader, key)

    async def read(self):
        try:
            frame = await self.reader.read()
        except EofStream:
            self.evidence.begin_close("transport_eof", layer="aiohttp_reader")
            raise
        except Exception as exc:
            self.evidence.begin_close("reader_exception", exception_type=type(exc).__name__)
            raise
        size = (
            len(frame.data.encode())
            if isinstance(frame.data, str)
            else len(frame.data)
            if isinstance(frame.data, (bytes, bytearray))
            else None
        )
        self.evidence.frame("library_dequeue", frame.type.name, size)
        return frame


class ObservedWriter:
    def __init__(self, writer, evidence: ConnectionEvidence):
        self.writer, self.evidence = writer, evidence

    def __getattr__(self, key):
        return getattr(self.writer, key)

    async def send_frame(self, *args, **kwargs):
        opcode = args[1] if len(args) > 1 else kwargs.get("opcode")
        if opcode in {aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG}:
            self.evidence.event("control_send_started", frame_type=opcode.name)
        try:
            result = await self.writer.send_frame(*args, **kwargs)
        except Exception as exc:
            self.evidence.begin_close("writer_exception", exception_type=type(exc).__name__)
            raise
        if opcode in {aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG}:
            self.evidence.event("control_send_completed", frame_type=opcode.name)
        return result

    async def close(self, *args, **kwargs):
        self.evidence.event("close_frame_send_started")
        try:
            return await self.writer.close(*args, **kwargs)
        except Exception as exc:
            self.evidence.begin_close("cleanup_writer_exception", exception_type=type(exc).__name__)
            raise


class ObservedAiohttpSocket(aiohttp.ClientWebSocketResponse):
    def __init__(self, *args, evidence: ConnectionEvidence, **kwargs):
        self.evidence = evidence
        super().__init__(*args, **kwargs)
        self._reader = ObservedReader(self._reader, evidence)  # type: ignore[assignment]
        self._writer = ObservedWriter(self._writer, evidence)  # type: ignore[assignment]

    def _on_data_received(self):
        # A chunk is not a parsed frame, and neither freshens a market timestamp.
        self.evidence.frame("library_transport", "CHUNK")
        return super()._on_data_received()

    def _reset_heartbeat(self):
        result = super()._reset_heartbeat()
        self.evidence.heartbeat_scheduled_ns = round(self._heartbeat_when * 1e9)
        self.evidence.heartbeat_resets += 1
        if self.evidence.heartbeat_resets == 1:
            self.evidence.event(
                "heartbeat_schedule", scheduled_ns=self.evidence.heartbeat_scheduled_ns
            )
        return result

    def _send_heartbeat(self):
        callback = self._heartbeat_cb
        prior_pong = self._pong_response_cb
        self.evidence.event(
            "heartbeat_callback",
            scheduled_ns=round(callback.when() * 1e9) if callback else None,
            heartbeat_due_ns=round(self._heartbeat_when * 1e9),
        )
        result = super()._send_heartbeat()
        if self._pong_response_cb is not None and self._pong_response_cb is not prior_pong:
            self.evidence.event(
                "pong_deadline", scheduled_ns=round(self._pong_response_cb.when() * 1e9)
            )
        return result

    def _pong_not_received(self):
        callback = self._pong_response_cb
        self.evidence.event(
            "pong_timeout_callback", scheduled_ns=round(callback.when() * 1e9) if callback else None
        )
        return super()._pong_not_received()

    def _handle_ping_pong_exception(self, exc):
        self.evidence.begin_close("library_heartbeat", exception_type=type(exc).__name__)
        return super()._handle_ping_pong_exception(exc)

    async def receive(self, *args, **kwargs):
        try:
            return await super().receive(*args, **kwargs)
        except asyncio.CancelledError:
            self.evidence.begin_close("task_cancellation")
            raise

    async def close(self, *args, **kwargs):
        self.evidence.begin_close("application_close")
        return await super().close(*args, **kwargs)


def observed_timeout(delay: float, evidence: ConnectionEvidence) -> asyncio.Timeout:
    timeout = asyncio.timeout(delay)
    original_callback = timeout._on_timeout  # type: ignore[attr-defined]
    when = timeout.when()
    evidence.event("pong_deadline", scheduled_ns=round(when * 1e9) if when is not None else None)

    def callback():
        evidence.event(
            "pong_timeout_callback", scheduled_ns=round(when * 1e9) if when is not None else None
        )
        original_callback()  # Native cancellation/uncancel/TimeoutError behavior, once.

    timeout._on_timeout = callback  # type: ignore[attr-defined]
    return timeout


# The keepalive template is tied to the installed implementation, checked before
# choosing it. Unknown versions retain the inherited behavior and mark missing hooks.
KEEPALIVE_SOURCE_SHA256 = "c3fafe63b4b8b6d8a6200b5e2961d6dee9e30b4e5c8e1efdd7098b4a80a773c3"
ACTUAL_KEEPALIVE_SOURCE_SHA256 = hashlib.sha256(
    inspect.getsource(ClientConnection.keepalive).encode()
).hexdigest()  # Startup only, never disk I/O on the heartbeat path.


class ObservedLighterSocket(ClientConnection):
    def __init__(self, *args, evidence: ConnectionEvidence, **kwargs):
        self.evidence = evidence
        super().__init__(*args, **kwargs)

    def process_event(self, event):
        if isinstance(event, Frame):
            self.evidence.frame("library_protocol", event.opcode.name, len(event.data))
            if event.opcode is Opcode.PONG:
                self.evidence.event(
                    "pong_observed", matched=bytes(event.data) in self.pending_pings
                )
        return super().process_event(event)

    def eof_received(self):
        self.evidence.begin_close("transport_eof", layer="websockets_protocol")
        return super().eof_received()

    def connection_lost(self, exc):
        self.evidence.begin_close(
            "transport_lost", exception_type=type(exc).__name__ if exc else None
        )
        return super().connection_lost(exc)

    async def close(self, *args, **kwargs):
        self.evidence.begin_close("application_close")
        return await super().close(*args, **kwargs)

    async def ping(self, *args, **kwargs):
        self.evidence.ping_count += 1
        self.evidence.event(
            "control_send_started", frame_type="PING", ping_id=self.evidence.ping_count
        )
        result = await super().ping(*args, **kwargs)
        self.evidence.event(
            "control_send_completed", frame_type="PING", ping_id=self.evidence.ping_count
        )
        return result

    async def keepalive(self) -> None:
        digest = ACTUAL_KEEPALIVE_SOURCE_SHA256
        if digest != KEEPALIVE_SOURCE_SHA256:
            self.evidence.event("heartbeat_hooks_unavailable", library_source_sha256=digest)
            return await super().keepalive()
        # Same sleep, latency compensation, ping, timeout and fail path as websockets
        # 16.1.1. Only observation hooks/native Timeout callback are inserted, no extra ping.
        assert self.ping_interval is not None
        latency = 0.0
        try:
            while True:
                due = self.loop.time() + self.ping_interval - latency
                self.evidence.event("heartbeat_schedule", scheduled_ns=round(due * 1e9))
                await asyncio.sleep(self.ping_interval - latency)
                self.evidence.event("heartbeat_callback", scheduled_ns=round(due * 1e9))
                pong_received = await self.ping()
                if self.debug:
                    self.logger.debug("% sent keepalive ping")
                if self.ping_timeout is not None:
                    try:
                        async with observed_timeout(self.ping_timeout, self.evidence):
                            latency = await pong_received
                            if self.debug:
                                self.logger.debug("% received keepalive pong")
                    except TimeoutError:
                        if self.debug:
                            self.logger.debug("- timed out waiting for keepalive pong")
                        self.evidence.begin_close(
                            "library_heartbeat", reason="keepalive_ping_timeout"
                        )
                        async with self.send_context():
                            self.protocol.fail(CloseCode.INTERNAL_ERROR, "keepalive ping timeout")
                        raise AssertionError(  # noqa: B904 -- preserve library exception chain
                            "send_context() should wait for connection_lost(), "
                            "which cancels keepalive()"
                        )
        except Exception:
            self.logger.error("keepalive ping failed", exc_info=True)
