"""Passive timing of the existing clock GET; no transport settings or extra traffic."""

import time
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import aiohttp

calibration_request: ContextVar[dict | None] = ContextVar("calibration_request", default=None)


def clock_trace() -> aiohttp.TraceConfig:
    config = aiohttp.TraceConfig()
    signals = (
        "request_start",
        "connection_queued_start",
        "connection_queued_end",
        "dns_resolvehost_start",
        "dns_resolvehost_end",
        "dns_cache_hit",
        "dns_cache_miss",
        "connection_create_start",
        "connection_create_end",
        "connection_reuseconn",
        "request_headers_sent",
        "request_end",
        "request_exception",
    )
    for name in signals:

        async def observe(
            session: Any, ctx: SimpleNamespace, params: Any, stage: str = name
        ) -> None:
            attempt = ctx.trace_request_ctx
            if not isinstance(attempt, dict):
                return
            begin = time.monotonic_ns()
            phases = attempt["transport"]
            events = phases["events"]
            if len(events) < 24:
                events.append({"phase": stage, "monotonic_ns": begin})
            else:
                phases["events_dropped"] += 1
            if stage == "connection_reuseconn":
                phases["connection_reused"] = True
                phases["connection_attempts"] += 1
            elif stage == "connection_create_start":
                phases["connection_reused"] = False
                phases["connection_attempts"] += 1
            elif stage == "request_headers_sent":
                # aiohttp may retry idempotent GET on a stale persistent connection.
                # request_start fires once, headers_sent once per actual send attempt.
                phases["http_send_attempts"] += 1
            phases["last_phase"] = stage
            phases["callback_cpu_ns"] += time.monotonic_ns() - begin

        getattr(config, "on_" + name).append(observe)
    return config


def transport_record() -> dict:
    return {
        "events": [],
        "events_dropped": 0,
        "callback_cpu_ns": 0,
        "connection_reused": None,
        "connection_attempts": 0,
        "http_send_attempts": 0,
        "dns": "unknown until observed; resolver-internal cache is not observed",
        "connection_create": "combined DNS/TCP/TLS; TLS separately unknown",
        "last_phase": "not_started",
    }
