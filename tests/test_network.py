from __future__ import annotations

import socket

import pytest

from lldesk.config import Config
from lldesk.network import BinanceResolver


@pytest.mark.asyncio
async def test_doh_cache_preserves_hostname_port_and_respects_ttl(monkeypatch):
    resolver = BinanceResolver("doh")
    clock = [1.0]
    calls = []

    async def lookup(host):
        calls.append(host)
        return ["8.8.8.8"], 5

    monkeypatch.setattr(resolver, "lookup", lookup)
    monkeypatch.setattr("lldesk.network.time.monotonic", lambda: clock[0])
    try:
        first = await resolver.resolve("fapi.binance.com", 443)
        assert first[0]["hostname"] == "fapi.binance.com" and first[0]["port"] == 443
        assert first[0]["host"] == "8.8.8.8" and first[0]["family"] == socket.AF_INET
        await resolver.resolve("fapi.binance.com", 443)
        assert len(calls) == 1
        clock[0] += 6
        await resolver.resolve("fapi.binance.com", 443)
        assert len(calls) == 2
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_doh_only_changes_binance_feed_hosts(monkeypatch):
    resolver = BinanceResolver("doh")
    calls = []

    async def system(host, port, family):
        calls.append(host)
        return []

    async def unexpected(host):
        raise AssertionError("unrelated hostname sent to DoH")

    monkeypatch.setattr(resolver.system, "resolve", system)
    monkeypatch.setattr(resolver, "lookup", unexpected)
    try:
        await resolver.resolve("mainnet.zklighter.elliot.ai", 443)
        await resolver.resolve("localhost", 8765)
        assert calls == ["mainnet.zklighter.elliot.ai", "localhost"]
    finally:
        await resolver.close()


class Response:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def json(self, **kwargs):
        return self.data


class Client:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def get(self, provider, **kwargs):
        self.requests.append((provider, kwargs))
        return Response(next(self.replies))

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_doh_rejects_local_addresses_and_falls_back_to_second_provider():
    resolver = BinanceResolver("doh")
    client = Client(
        [
            {"Status": 0, "Answer": [{"type": 1, "data": "127.0.0.1", "TTL": 60}]},
            {"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8", "TTL": 10}]},
        ]
    )
    resolver.client = client
    try:
        answers, ttl = await resolver.lookup("fstream.binance.com")
        assert answers == ["8.8.8.8"] and ttl == 10
        assert len(client.requests) == 2
        assert all(r[1]["params"]["name"] == "fstream.binance.com" for r in client.requests)
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_doh_failure_does_not_accept_nxdomain_or_fall_back_to_bad_system_dns():
    resolver = BinanceResolver("doh")
    resolver.client = Client([{"Status": 3}, {"Status": 3}])
    try:
        with pytest.raises(OSError, match="unavailable"):
            await resolver.resolve("fapi.binance.com", 443)
    finally:
        await resolver.close()


def test_dns_mode_rejects_arbitrary_resolver_names():
    with pytest.raises(ValueError):
        Config(binance_dns_mode="disabled-tls")
