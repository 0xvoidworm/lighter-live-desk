"""Safe phase timing for the installed SDK's GET transport; no headers/bodies/query."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import aiohttp

READ_PATHS = {
    "/api/v1/account",
    "/api/v1/accountActiveOrders",
    "/api/v1/trades",
    "/api/v1/deposit/history",
    "/api/v1/withdraw/history",
    "/api/v1/transfer/history",
    "/api/v1/export",
    "/fapi/v1/depth",
    "/public/stream",
    "/market/stream",
}


class TransportTrace:
    def __init__(self) -> None:
        self.completed: deque[dict] = deque(maxlen=256)
        self.active: dict[int, dict] = {}
        self.current: ContextVar[dict | None] = ContextVar("account_http_trace", default=None)
        self.read_context: ContextVar[dict | None] = ContextVar("read_context", default=None)
        self.sequence = 0

    @contextmanager
    def operation(self, reason: str, priority: str, quota_weight: int = 1):
        token = self.read_context.set(
            {"reason": reason, "quota_priority": priority, "local_quota_weight": quota_weight}
        )
        try:
            yield
        finally:
            self.read_context.reset(token)

    def pagination(self, path: str, *, count: int, cursor: str | None, next_cursor: str | None):
        # Only counts/booleans, never cursor values or response contents.
        task = id(asyncio.current_task())
        for record in reversed(self.completed):
            if record["task"] == task and record["path"] == path:
                record["pagination"] = {
                    "items": count,
                    "requested_cursor": bool(cursor),
                    "returned_cursor": bool(next_cursor),
                    "cursor_echo": bool(cursor and cursor == next_cursor),
                }
                break

    @staticmethod
    def phase(record: dict, name: str) -> None:
        now = time.monotonic()
        previous = record.get("phase")
        if previous:
            timings = record["phase_ms"]
            timings[previous] = timings.get(previous, 0.0) + (now - record["phase_at"]) * 1000
        record.update(phase=name, phase_at=now)

    def finish(self, record: dict, outcome: str, error: BaseException | None = None) -> None:
        if record["id"] not in self.active:
            return
        failed_phase = record["phase"]
        self.phase(record, "finished")
        result = {k: record[k] for k in ("id", "task", "host", "path", "started_at", "phase_ms")}
        result.update(
            **record["read_context"],
            http_attempts=record["http_attempts"],
            outcome=outcome,
            last_phase=failed_phase,
            elapsed_ms=round((time.monotonic() - record["started_mono"]) * 1000, 3),
            error=type(error).__name__ if error is not None else None,
            status=record.get("status"),
            finished_at=time.time(),
        )
        self.completed.append(result)
        del self.active[record["id"]]

    def config(self) -> aiohttp.TraceConfig:
        owner = self

        class Context(SimpleNamespace):
            def __init__(self, *, trace_request_ctx: Any = None) -> None:
                super().__init__(record=owner.current.get())

        config = aiohttp.TraceConfig(trace_config_ctx_factory=Context)

        async def start(session: Any, ctx: Any, params: Any) -> None:
            ctx.managed = ctx.record is not None
            path = urlsplit(str(params.url)).path
            if ctx.record is None and params.method == "GET" and path in READ_PATHS:
                ctx.record = self.begin(str(params.url))
            if ctx.record is not None:
                ctx.record["http_attempts"] += 1

        async def body(session: Any, ctx: Any, params: Any) -> None:
            if ctx.record is not None and not ctx.managed:
                self.finish(ctx.record, "ok")

        async def failed(session: Any, ctx: Any, params: Any) -> None:
            if ctx.record is not None:
                self.finish(ctx.record, "error", params.exception)

        config.on_request_start.append(start)
        config.on_response_chunk_received.append(body)
        config.on_request_exception.append(failed)
        signals = {
            "on_connection_queued_start": "pool_queue",
            "on_connection_queued_end": "acquire_connection",
            "on_connection_create_start": "connect_or_tls",
            "on_connection_create_end": "connection_ready",
            "on_connection_reuseconn": "connection_ready",
            "on_dns_resolvehost_start": "dns",
            "on_dns_resolvehost_end": "connect_or_tls",
            "on_request_headers_sent": "waiting_headers",
            "on_request_end": "body_ready",
        }
        for signal, phase in signals.items():

            async def callback(session: Any, ctx: Any, params: Any, stage: str = phase) -> None:
                if ctx.record is not None and ctx.record["id"] in self.active:
                    self.phase(ctx.record, stage)
                    if stage == "body_ready":
                        ctx.record["status"] = params.response.status
                        if not ctx.managed and params.response.status == 101:
                            self.finish(ctx.record, "ok")

            getattr(config, signal).append(callback)
        config.freeze()
        return config

    def begin(self, url: str) -> dict:
        endpoint = urlsplit(url)
        self.sequence += 1
        record: dict = {
            "id": self.sequence,
            "task": id(asyncio.current_task()),
            "host": endpoint.hostname,
            "path": endpoint.path,
            "started_at": time.time(),
            "started_mono": time.monotonic(),
            "phase_ms": {},
            "read_context": (self.read_context.get() or {}).copy(),
            "http_attempts": 0,
        }
        self.phase(record, "acquire_connection")
        self.active[record["id"]] = record
        return record

    def install(self, rest: Any) -> None:
        """Preserve SDK connector/proxy/TLS/retry settings; instrument this instance only."""
        rest.pool_manager.trace_configs.append(self.config())
        original = rest.request

        async def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            endpoint = urlsplit(url)
            if method.upper() != "GET" or endpoint.path not in READ_PATHS:
                return await original(method, url, *args, **kwargs)
            record = self.begin(url)
            token = self.current.set(record)
            try:
                response = await original(method, url, *args, **kwargs)
            except BaseException as exc:
                self.finish(
                    record, "cancelled" if isinstance(exc, asyncio.CancelledError) else "error", exc
                )
                raise
            finally:
                self.current.reset(token)
            read_body = response.read

            async def read() -> Any:
                self.phase(record, "reading_body")
                try:
                    data = await read_body()
                except BaseException as exc:
                    self.finish(
                        record,
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                        exc,
                    )
                    raise
                self.finish(record, "ok")
                return data

            response.read = read
            return response

        rest.request = request

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "active": [
                {k: r[k] for k in ("id", "path", "phase", "started_at")}
                | {"elapsed_ms": round((now - r["started_mono"]) * 1000, 3)}
                for r in self.active.values()
            ],
            "recent": list(self.completed)[-16:],
            "connect_tls_resolution": "aiohttp combines TCP and TLS connection creation",
        }
