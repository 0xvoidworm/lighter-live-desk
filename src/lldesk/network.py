from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from .clock_trace import calibration_request, clock_trace, transport_record

BINANCE_HOSTS = {"fapi.binance.com", "fstream.binance.com"}
DNS_PROVIDERS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
DnsMode = Literal["system", "doh"]


class BinanceResolver(AbstractResolver):
    """Opt-in DNS over HTTPS for public Binance feeds, preserving TLS hostname checks."""

    def __init__(self, mode: DnsMode = "system", *, keepalive_timeout: float | None = None) -> None:
        self.mode = mode
        self.system = DefaultResolver()
        self.client: aiohttp.ClientSession | None = None
        self.cache: dict[str, tuple[float, list[str]]] = {}
        self.lock = asyncio.Lock()
        self.keepalive_timeout = keepalive_timeout

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        if self.mode == "system" or host not in BINANCE_HOSTS:
            return await self.system.resolve(host, port, socket.AddressFamily(family))
        # IPv4 is intentional; only A answers to the requested hostname are accepted.
        if family not in {socket.AF_UNSPEC, socket.AF_INET}:
            raise OSError("Binance DoH requires IPv4")
        async with self.lock:
            cached = self.cache.get(host)
            if cached and cached[0] > time.monotonic():
                addresses = cached[1]
            else:
                addresses, ttl = await self.lookup(host)
                self.cache[host] = (time.monotonic() + ttl, addresses)
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_NUMERICHOST,
            )
            for address in addresses
        ]

    async def lookup(self, host: str) -> tuple[list[str], int]:
        if self.client is None:
            # Only the dedicated calibration caller overrides transport idle time.
            # This does not extend the answer TTL in resolve().
            kwargs: dict[str, Any] = (
                {"keepalive_timeout": self.keepalive_timeout}
                if self.keepalive_timeout is not None
                else {}
            )
            self.client = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(**kwargs),
                timeout=aiohttp.ClientTimeout(total=3),
                trust_env=False,
                trace_configs=[clock_trace()] if self.keepalive_timeout is not None else None,
            )
        for provider in DNS_PROVIDERS:
            parent = calibration_request.get()
            attempt: dict[str, Any] | None = None
            if parent is not None:
                # Bound provider fallback plus one stale-connection retry of the outer GET.
                attempts = parent.setdefault("resolver_http_attempts", [])
                if len(attempts) < 4:
                    attempt = {"provider": provider, "transport": transport_record()}
                    attempts.append(attempt)
                else:
                    parent["resolver_http_attempts_dropped"] = (
                        parent.get("resolver_http_attempts_dropped", 0) + 1
                    )
            try:
                async with self.client.get(
                    provider,
                    params={"name": host, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                    trace_request_ctx=attempt,
                ) as response:
                    if attempt is not None:
                        attempt["status"] = response.status
                    response.raise_for_status()
                    data = await response.json(content_type=None)
                if data.get("Status") != 0 or data.get("TC"):
                    continue
                # CNAME owners may differ; the HTTPS resolver follows the requested name.
                answers = [a for a in data.get("Answer", []) if a.get("type") == 1]
                addresses = [str(ipaddress.IPv4Address(a["data"])) for a in answers]
                if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                    continue
                ttl = min(60, max(0, min(int(a["TTL"]) for a in answers)))
                return list(dict.fromkeys(addresses)), ttl
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                if attempt is not None:
                    attempt["exception"] = type(exc).__name__
                continue
            finally:
                if attempt is not None:
                    sends = attempt["transport"]["http_send_attempts"]
                    attempt.update(http_attempts=sends, http_retries=max(0, sends - 1))
        raise OSError("Binance DNS over HTTPS unavailable")

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
        await self.system.close()


@asynccontextmanager
async def binance_session(
    mode: DnsMode = "system",
    *,
    trace_configs: list[aiohttp.TraceConfig] | None = None,
    ws_response_class: Any = aiohttp.ClientWebSocketResponse,
    keepalive_timeout: float | None = None,
) -> AsyncIterator[aiohttp.ClientSession]:
    resolver = BinanceResolver(mode, keepalive_timeout=keepalive_timeout)
    try:
        kwargs: dict[str, Any] = (
            {"keepalive_timeout": keepalive_timeout} if keepalive_timeout is not None else {}
        )
        connector = aiohttp.TCPConnector(
            resolver=resolver, family=socket.AF_INET, use_dns_cache=False, **kwargs
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=10, connect=5),
            trust_env=False,
            trace_configs=trace_configs,
            ws_response_class=ws_response_class,
        ) as client:
            yield client
    finally:
        await resolver.close()
