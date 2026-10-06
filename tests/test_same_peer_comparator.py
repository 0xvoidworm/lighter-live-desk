"""Offline/local TLS only; no exchange, account, clock request or trading imports."""

import asyncio
import json
import socket
import ssl
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from scripts.analyze_depth_comparator import controlled_depth, peer_epochs, quantiles
from scripts.record_depth_comparator import (
    PUBLIC_HOST,
    PUBLIC_URL,
    PeerOverrideResolver,
    record,
    same_peer_session,
)


class OriginalResolver:
    def __init__(self):
        self.calls = []

    async def resolve(self, host, port, family):
        self.calls.append((host, port, family))
        return ["original-result"]


async def test_override_only_exact_hostname_and_keeps_hostname_port_family():
    original = OriginalResolver()
    wrapper = PeerOverrideResolver(original, "13.112.27.226")
    result = await wrapper.resolve(PUBLIC_HOST, 443, socket.AF_INET)
    assert result[0] == dict(
        hostname=PUBLIC_HOST,
        host="13.112.27.226",
        port=443,
        family=socket.AF_INET,
        proto=socket.IPPROTO_TCP,
        flags=socket.AI_NUMERICHOST,
    )
    assert not original.calls
    for host in ["fapi.binance.com", "example.invalid", PUBLIC_HOST + ".evil"]:
        assert await wrapper.resolve(host, 443, socket.AF_INET) == ["original-result"]
    assert len(original.calls) == 3
    with pytest.raises(OSError):
        await wrapper.resolve(PUBLIC_HOST, 443, socket.AF_INET6)
    with pytest.raises(ValueError):
        PeerOverrideResolver(original, "13.112.27.226:443")


def test_receipt_p99_and_peer_epochs_never_inherit_unknown_or_changed_peer():
    assert quantiles(list(range(1, 101)))["p99"] == pytest.approx(99.01)
    events = []
    for identity, when, ip in [
        ("a", 1, "13.112.27.226"),
        ("b", 30, "13.112.27.227"),
        ("c", 60, None),
        ("d", 80, "13.112.27.226"),
    ]:
        events.append(
            {
                "kind": "handshake_completed",
                "channel": "binance_public",
                "pid": 1,
                "run_id": "run",
                "connection_id": identity,
                "monotonic_ns": when,
                "socket_metadata": {
                    "status": "available" if ip else "unavailable",
                    "remote": {"address": ip, "port": 443} if ip else None,
                },
            }
        )
    events.append({"kind": "close_initiated", "connection_id": "a", "monotonic_ns": 20})
    epochs = peer_epochs(events, "13.112.27.226", 10, 100, pid=1, run_id="run")
    assert [
        (e["start_monotonic_ns"], e["end_monotonic_ns"], e["peer_matches"]) for e in epochs
    ] == [(10, 20, True), (30, 60, False), (60, 80, False), (80, 100, True)]
    records = [
        {"receipt_monotonic_ns": t, "connection_id": c}
        for t, c in [(10, "a"), (20, "a"), (40, "b"), (70, "c"), (85, "d"), (90, "a")]
    ]
    assert controlled_depth(records, epochs, production=True) == [records[0], records[4]]
    assert controlled_depth(records, epochs, production=False) == [
        records[0],
        records[4],
        records[5],
    ]


@pytest.mark.parametrize("cancel", [False, True])
async def test_override_restored_on_exception_and_cancellation(cancel):
    original = OriginalResolver()
    connector = SimpleNamespace(_resolver=original)
    closed = []

    @asynccontextmanager
    async def factory(mode, **kwargs):
        try:
            yield SimpleNamespace(connector=connector)
        finally:
            closed.append(True)

    exception = asyncio.CancelledError if cancel else RuntimeError
    with pytest.raises(exception):
        async with same_peer_session("doh", target_ip="13.112.27.226", session_factory=factory):
            assert connector._resolver is not original
            raise exception()
    assert connector._resolver is original and closed == [True]


def fake_factory(peer, *, sni=PUBLIC_HOST, certificate_check=True, delay=0):
    calls, closed, reads = [], [], []
    opened = asyncio.Event()
    transport = SimpleNamespace(
        is_closing=lambda: bool(closed),
        get_extra_info=lambda key: {
            "peername": (peer, 443),
            "ssl_object": SimpleNamespace(
                server_hostname=sni,
                context=SimpleNamespace(
                    check_hostname=certificate_check, verify_mode=ssl.CERT_REQUIRED
                ),
            ),
        }.get(key),
    )

    class Socket:
        _writer = SimpleNamespace(transport=transport)

        async def __aenter__(self):
            await asyncio.sleep(delay)
            opened.set()
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        def __aiter__(self):
            return self

        async def __anext__(self):
            reads.append(True)
            await asyncio.Future()

    connector = SimpleNamespace(_resolver=OriginalResolver())

    @asynccontextmanager
    async def factory(mode, **kwargs):
        calls.append((mode, kwargs))

        def connect(url, **options):
            assert url == PUBLIC_URL and options == {"heartbeat": 20}
            calls.append(url)
            return Socket()

        yield SimpleNamespace(connector=connector, ws_connect=connect)

    return factory, calls, closed, reads, opened


@pytest.mark.parametrize(
    "peer,sni,check",
    [
        ("13.112.27.227", PUBLIC_HOST, True),
        ("13.112.27.226", "wrong.invalid", True),
        ("13.112.27.226", PUBLIC_HOST, False),
    ],
)
async def test_peer_or_TLS_control_failure_closes_without_receiving_or_retry(
    tmp_path, peer, sni, check
):
    factory, calls, closed, reads, _ = fake_factory(peer, sni=sni, certificate_check=check)
    result = await record(
        tmp_path / "control", 0.03, "doh", target_ip="13.112.27.226", session_factory=factory
    )
    assert result["outcome"] == "peer_control_failed" and result["messages"] == 0
    assert result["connection_closed"] and closed == [True] and not reads
    assert len(calls) == 2  # One factory + one ws_connect, no other IP or retry.


async def test_deadline_includes_delayed_handshake_no_new_attempt(tmp_path):
    factory, calls, closed, reads, opened = fake_factory("13.112.27.226", delay=1)
    result = await record(
        tmp_path / "deadline", 0.03, "doh", target_ip="13.112.27.226", session_factory=factory
    )
    assert result["outcome"] == "window_complete" and result["connection_closed"]
    assert not opened.is_set() and not reads and len(calls) == 2
    assert result["finished_monotonic_ns"] - result["started_monotonic_ns"] < 100_000_000


async def test_cancellation_after_matching_handshake_cleans_up(tmp_path):
    factory, calls, closed, reads, opened = fake_factory("13.112.27.226")
    task = asyncio.create_task(
        record(tmp_path / "cancel", 1, "doh", target_ip="13.112.27.226", session_factory=factory)
    )
    await asyncio.wait_for(opened.wait(), 1)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = json.loads((tmp_path / "cancel" / "finished.json").read_text())
    assert result["outcome"] == "cancelled" and result["connection_closed"]
    assert result["peer_control"] == "matched" and closed == [True] and len(calls) == 2


@pytest.mark.parametrize("url", ["wss://example.invalid/", PUBLIC_URL + "&other=1"])
async def test_pin_rejects_non_official_URL_before_network(tmp_path, url):
    with pytest.raises(ValueError):
        await record(tmp_path / "must-not-exist", 1, "doh", target_ip="13.112.27.226", url=url)
    assert not (tmp_path / "must-not-exist").exists()
    with pytest.raises(ValueError):
        await record(tmp_path / "must-not-exist", 901, "doh", target_ip="13.112.27.226")


@pytest.fixture
def local_cert(tmp_path):
    key, cert = tmp_path / "local-test.key", tmp_path / "local-test.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=fstream.binance.com",
            "-addext",
            "subjectAltName=DNS:fstream.binance.com",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    return key, cert


async def test_real_local_TLS_preserves_SNI_and_checks_trust_and_hostname(local_cert):
    key, cert = local_cert
    names = []
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server_context.set_servername_callback(lambda obj, name, ctx: names.append(name))

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str("local-TLS-only")
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"wss://{PUBLIC_HOST}:{port}/ws"
    trusted = ssl.create_default_context(cafile=str(cert))
    assert trusted.check_hostname and trusted.verify_mode == ssl.CERT_REQUIRED
    try:
        async with same_peer_session("system", target_ip="127.0.0.1") as client:
            assert client._trust_env is False and client.connector._ssl is True
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                async with client.ws_connect(url):
                    pytest.fail("untrusted local cert accepted")
            async with client.ws_connect(url, ssl=trusted) as ws:
                assert (await ws.receive()).data == "local-TLS-only"
                obj = ws._writer.transport.get_extra_info("ssl_object")
                assert obj.server_hostname == PUBLIC_HOST
                assert ws._writer.transport.get_extra_info("peername")[0] == "127.0.0.1"
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                async with client.ws_connect(url, ssl=trusted, server_hostname="wrong.invalid"):
                    pytest.fail("wrong hostname accepted")
        assert client.closed
        assert PUBLIC_HOST in names and "wrong.invalid" in names
    finally:
        await runner.cleanup()
