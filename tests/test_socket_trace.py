"""Only localhost sockets and fake account auth; no SDK constructor or exchange."""

import asyncio
import json
import os
import socket
import stat
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace

import aiohttp
import pytest
import websockets
from aiohttp import web

import lldesk.account as account_module
from lldesk.account import LighterAccount
from lldesk.socket_trace import (
    _proc_endpoint,
    match_socket_observation,
    process_socket_observation,
    socket_metadata,
)
from lldesk.websocket_trace import ObservedAiohttpSocket, ObservedLighterSocket, WebsocketTrace


@pytest.fixture
async def server():
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if request.path == "/account":
            await ws.send_json({"type": "update/account_market", "timestamp": 1234})
        else:
            await ws.send_str(request.path)
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/{channel}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    await runner.cleanup()


def raw_handshakes(trace):
    # Private queue, deliberately distinct from the public/status view.
    return [r for r in list(trace.queue.queue) if r["kind"] == "handshake_completed"]


async def test_four_local_channels_actual_transports_stay_distinct(server, tmp_path):
    trace = WebsocketTrace(tmp_path, "local-four")
    ready, release = asyncio.Queue(), asyncio.Event()

    async def channel(name, library):
        url = f"{server}/{name}?auth=DO_NOT_LOG_THIS"
        evidence = trace.connect(name, url)
        if library == "aiohttp":
            async with (
                aiohttp.ClientSession(
                    ws_response_class=partial(ObservedAiohttpSocket, evidence=evidence)
                ) as client,
                client.ws_connect(url, heartbeat=20) as ws,
            ):
                evidence.handshake_completed(ws, library)
                assert (await ws.receive()).data == f"/{name}"
                await ready.put(evidence)
                await release.wait()
        else:
            async with websockets.connect(
                url,
                create_connection=partial(ObservedLighterSocket, evidence=evidence),
                ping_interval=30,
            ) as ws:
                evidence.handshake_completed(ws, library)
                assert await ws.recv() == f"/{name}"
                await ready.put(evidence)
                await release.wait()

    tasks = [
        asyncio.create_task(channel(name, lib))
        for name, lib in [
            ("binance_public", "aiohttp"),
            ("binance_market", "aiohttp"),
            ("lighter", "websockets"),
            ("lighter_account", "websockets"),
        ]
    ]
    try:
        for _ in tasks:
            await asyncio.wait_for(ready.get(), 2)
        events = raw_handshakes(trace)
        assert {r["channel"] for r in events} == {
            "binance_public",
            "binance_market",
            "lighter",
            "lighter_account",
        }
        assert len({r["socket_metadata"]["local"]["port"] for r in events}) == 4
        assert len({r["socket_metadata"]["inode"] for r in events}) == 4
        observed = process_socket_observation(os.getpid())
        for row in events:
            m = row["socket_metadata"]
            assert m["status"] == "available" and m["address_family"]["name"] == "AF_INET"
            assert m["remote"]["port"] == int(server.rsplit(":", 1)[1])
            assert (
                match_socket_observation(row, observed, active_at_observation=True)["status"]
                == "matched"
            )
        assert "DO_NOT_LOG_THIS" not in json.dumps(list(trace.queue.queue))
        public = json.dumps(trace.status())
        assert '"socket_metadata"' not in public and '"local"' not in public
    finally:
        release.set()
        await asyncio.gather(*tasks)


class Transport:
    def __init__(self, sock=None, closing=False, error=False):
        self.sock, self.closing, self.error = sock, closing, error

    def is_closing(self):
        return self.closing

    def get_extra_info(self, name):
        if self.error:
            raise RuntimeError("NEVER_LOG_CREDENTIAL")
        return {
            "socket": self.sock,
            "sockname": ("127.0.0.1", 100),
            "peername": ("127.0.0.2", 200),
        }.get(name)


def test_fd_reuse_new_identity_does_not_inherit_and_old_not_remapped(tmp_path, monkeypatch):
    inode = [1001]
    monkeypatch.setattr(
        "lldesk.socket_trace.os",
        SimpleNamespace(fstat=lambda fd: SimpleNamespace(st_mode=stat.S_IFSOCK, st_ino=inode[0])),
    )
    sock = SimpleNamespace(fileno=lambda: 42, family=socket.AF_INET)
    ws = SimpleNamespace(transport=Transport(sock))
    trace = WebsocketTrace(tmp_path, "reuse")
    a = trace.connect("lighter_account", "wss://example.invalid/stream")
    a.handshake_completed(ws, "websockets")
    old = raw_handshakes(trace)[0]
    a.begin_close("application_auth_rotation")
    inode[0] = 2002
    b = trace.connect("lighter_account", "wss://example.invalid/stream")
    b.handshake_completed(ws, "websockets")
    new = raw_handshakes(trace)[1]
    a.handshake_completed(ws, "websockets")  # Never overwrite this lifetime's first capture.
    assert len(raw_handshakes(trace)) == 2 and old["connection_id"] != new["connection_id"]
    assert old["socket_metadata"]["fd"] == new["socket_metadata"]["fd"] == 42
    assert old["socket_metadata"]["inode"] == 1001
    assert new["socket_metadata"]["inode"] == 2002
    observed = {
        "pid": os.getpid(),
        "started_monotonic_ns": new["monotonic_ns"] + 1,
        "finished_monotonic_ns": new["monotonic_ns"] + 2,
        "errors": [],
        "records": [
            {
                "pid": os.getpid(),
                "fds": [42],
                "inode": 2002,
                "address_family": 2,
                "local": new["socket_metadata"]["local"],
                "remote": new["socket_metadata"]["remote"],
            }
        ],
    }
    assert (
        match_socket_observation(new, observed, active_at_observation=True)["status"] == "matched"
    )
    assert match_socket_observation(new, observed)["reason"] == "connection_lifetime_not_verified"
    assert match_socket_observation(old, observed)["status"] == "ambiguous"
    assert (
        match_socket_observation(old, observed, a.first_close["monotonic_ns"])["status"]
        == "ambiguous"
    )
    assert match_socket_observation({"pid": os.getpid()}, observed)["status"] == "unavailable"


@pytest.mark.parametrize("family,ip", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_procfs_ipv4_and_ipv6_host_order(family, ip):
    import sys

    packed = socket.inet_pton(family, ip)
    if sys.byteorder == "little":
        packed = b"".join(packed[i : i + 4][::-1] for i in range(0, len(packed), 4))
    assert _proc_endpoint(packed.hex() + ":01BB", family) == {"address": ip, "port": 443}


@pytest.mark.parametrize(
    "invalid", ["wrong_pid", "before_handshake", "duplicate", "error", "fd_race"]
)
def test_OS_join_insufficient_evidence_is_ambiguous(invalid):
    m = {
        "local": {"address": "127.0.0.1", "port": 10},
        "remote": {"address": "127.0.0.2", "port": 20},
        "address_family": {"number": 2},
        "fd": 42,
        "inode": 1001,
    }
    event = {"pid": 1, "monotonic_ns": 10, "connection_id": "run/channel/1", "socket_metadata": m}
    row = {
        "pid": 1,
        "address_family": 2,
        "fds": [42],
        "inode": 1001,
        "local": m["local"],
        "remote": m["remote"],
    }
    observation = {
        "pid": 1,
        "started_monotonic_ns": 11,
        "finished_monotonic_ns": 12,
        "records": [row],
        "errors": [],
        "warnings": [],
    }
    if invalid == "wrong_pid":
        observation["pid"] = 2
    elif invalid == "before_handshake":
        observation["started_monotonic_ns"] = 9
    elif invalid == "duplicate":
        observation["records"].append(dict(row))
    elif invalid == "error":
        observation["errors"].append("tcp_unavailable")
    else:
        observation["warnings"].append({"fd": 42})
    assert (
        match_socket_observation(event, observation, active_at_observation=True)["status"]
        == "ambiguous"
    )


async def test_full_recorder_queue_does_not_hold_handshake_or_receive(server, tmp_path):
    trace = WebsocketTrace(tmp_path, "full", capacity=1)
    evidence = trace.connect("binance_public", server + "/book")
    async with (
        aiohttp.ClientSession(
            ws_response_class=partial(ObservedAiohttpSocket, evidence=evidence)
        ) as client,
        client.ws_connect(server + "/book") as ws,
    ):
        evidence.handshake_completed(ws, "aiohttp")
        assert (await asyncio.wait_for(ws.receive(), 1)).data == "/book"
        assert trace.status()["events_dropped"] > 0
        assert evidence.socket_metadata_status == "available"


@pytest.mark.parametrize(
    "transport,reason",
    [
        (None, "transport_unavailable"),
        (Transport(closing=True), "transport_closing"),
        (Transport(SimpleNamespace(fileno=lambda: -1)), "socket_closed"),
        (Transport(error=True), "socket_access_failed:RuntimeError"),
    ],
)
def test_closed_or_missing_socket_explicit_null_and_no_secret(transport, reason):
    m = socket_metadata(SimpleNamespace(transport=transport), "websockets")
    assert m["fd"] is None and m["inode"] is None
    assert reason in m["missing"].values()
    assert "NEVER_LOG_CREDENTIAL" not in json.dumps(m)


def test_probe_failure_and_log_query_never_changes_transport(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("PRIVATE_KEY_NEVER_LOG")

    monkeypatch.setattr("lldesk.websocket_trace.socket_metadata", fail)
    trace = WebsocketTrace(tmp_path, "fault")
    evidence = trace.connect(
        "lighter_account", "wss://USER:PASSWORD@host.invalid/stream?token=TOKEN#COOKIE"
    )
    ws = SimpleNamespace()
    evidence.handshake_completed(ws, "websockets")
    row = raw_handshakes(trace)[0]
    assert row["socket_metadata"]["status"] == "unavailable"
    encoded = json.dumps(list(trace.queue.queue))
    assert all(
        secret not in encoded
        for secret in ["PRIVATE_KEY_NEVER_LOG", "USER", "PASSWORD", "TOKEN", "COOKIE"]
    )
    assert row["socket_metadata"]["missing"]["remote"] == "metadata_probe_failed:RuntimeError"


async def test_actual_account_stream_rotation_and_cancellation_local(server, tmp_path, monkeypatch):
    trace = WebsocketTrace(tmp_path, "account-rotation")
    clock = [0]
    original_connect = websockets.connect
    calls = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        calls.append(url)
        index = len(calls)
        async with original_connect(server + "/account", **kwargs) as ws:

            class LocalSocket:
                def __getattr__(self, key):
                    return getattr(ws, key)

                async def recv(self):
                    data = await ws.recv()
                    if index == 1:
                        clock[0] = 500  # Only account module clock, not asyncio transport timers.
                    return data

            yield LocalSocket()

    monkeypatch.setattr(account_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(account_module.websockets, "connect", connect)
    account = object.__new__(LighterAccount)
    account.account_index = 751745
    account.auth = lambda: "FAKE_AUTH_NEVER_LOG"
    updates = asyncio.Queue()
    task = asyncio.create_task(account.stream(1, updates, telemetry=trace))
    try:
        await asyncio.wait_for(updates.get(), 2)
        await asyncio.wait_for(updates.get(), 2)
        events = raw_handshakes(trace)
        assert len(events) == 2
        assert events[0]["connection_id"] != events[1]["connection_id"]
        assert events[0]["socket_metadata"]["inode"] != events[1]["socket_metadata"]["inode"]
        first = trace.connections[events[0]["connection_id"]]
        assert first.first_close["cause"] == "application_auth_rotation"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    second = trace.connections[events[1]["connection_id"]]
    assert second.first_close["cause"] == "task_cancellation"
    assert "FAKE_AUTH_NEVER_LOG" not in json.dumps(list(trace.queue.queue))
