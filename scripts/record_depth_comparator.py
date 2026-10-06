"""One public depth connection, no REST snapshot, account, signer or engine.

Use the production network factory and passive socket hooks unchanged. The
deadline includes handshake; failures are retained and never reconnect here.
Python is required to match the installed production aiohttp/resolver exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import socket
import ssl
import struct
import time
import uuid
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from lldesk.models import Stamp
from lldesk.network import binance_session
from lldesk.websocket_trace import ObservedAiohttpSocket, WebsocketTrace

PUBLIC_URL = "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms"
PUBLIC_HOST = "fstream.binance.com"


class PeerControlFailure(RuntimeError):
    """The single comparison did not meet the actual peer/TLS control."""


class PeerOverrideResolver(AbstractResolver):
    """Diagnostic session only; preserve the requested hostname for TLS/SNI."""

    def __init__(self, original, target_ip: str):
        self.original = original
        self.target_ip = str(ipaddress.IPv4Address(target_ip))

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        if host != PUBLIC_HOST:
            return await self.original.resolve(host, port, family)
        if family not in {socket.AF_INET, socket.AF_UNSPEC}:
            raise OSError("same-peer control requires production IPv4")
        return [
            ResolveResult(
                hostname=host,
                host=self.target_ip,
                port=port,
                family=socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_NUMERICHOST,
            )
        ]

    async def close(self):
        # The original factory owns its resolver. This wrapper has no network resources.
        pass


@asynccontextmanager
async def same_peer_session(mode, *, target_ip: str, session_factory=binance_session, **kwargs):
    async with session_factory(mode, **kwargs) as client:
        # Installed aiohttp3.14.3 has no per-request resolver override. Only this
        # fresh diagnostic connector is changed, then restored before factory cleanup.
        original = client.connector._resolver
        client.connector._resolver = PeerOverrideResolver(original, target_ip)
        try:
            yield client
        finally:
            client.connector._resolver = original


def tls_state(transport):
    obj = transport.get_extra_info("ssl_object")
    if obj is None:
        return {"sni": None, "check_hostname": None, "verify_mode": None}
    return {
        "sni": obj.server_hostname,
        "check_hostname": obj.context.check_hostname,
        "verify_mode": int(obj.context.verify_mode),
    }


def tcp_state(transport) -> dict:
    """Passive Linux TCP_INFO; RTT is TCP's estimate, not message latency."""
    if transport is None:
        return {"peer": None, "tcp_info": None}
    result = {"peer": transport.get_extra_info("peername"), "tcp_info": None}
    sock = transport.get_extra_info("socket")
    if sock is not None:
        try:
            raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
            result["tcp_info"] = {
                name: struct.unpack_from("=I", raw, offset)[0]
                for name, offset in {
                    "unacked": 24,
                    "retrans": 36,
                    "rtt_us": 68,
                    "rttvar_us": 72,
                    "total_retrans": 100,
                }.items()
            }
        except (OSError, struct.error) as exc:
            result["tcp_info_error"] = type(exc).__name__
    return result


async def consume(
    trace,
    connection,
    url: str,
    mode: str,
    stats: dict,
    session_factory,
    expected_peer: str | None = None,
):
    async with (
        session_factory(
            mode, ws_response_class=partial(ObservedAiohttpSocket, evidence=connection)
        ) as client,
        client.ws_connect(url, heartbeat=20) as ws,
        connection.scope(),
    ):
        connection.handshake_completed(ws, "aiohttp")
        transport = ws._writer.transport
        stats["peer"] = tcp_state(transport)["peer"]
        stats["transport"] = transport
        trace.event("comparison_peer", **tcp_state(transport), tls_hostname_validation=True)
        if expected_peer is not None:
            stats["tls"] = tls_state(transport)
            peer = stats["peer"]
            matched = peer is not None and peer[0] == expected_peer and peer[1] == 443
            tls_ok = stats["tls"] == {
                "sni": PUBLIC_HOST,
                "check_hostname": True,
                "verify_mode": int(ssl.CERT_REQUIRED),
            }
            stats["peer_control"] = "matched" if matched and tls_ok else "failed"
            trace.event(
                "comparison_peer_control",
                result=stats["peer_control"],
                expected_peer=expected_peer,
                tls=stats["tls"],
            )
            if not matched or not tls_ok:
                raise PeerControlFailure("actual peer or TLS control unavailable/mismatched")
        previous = None
        async for frame in ws:
            stamp = Stamp()
            if frame.type != aiohttp.WSMsgType.TEXT:
                trace.event("comparison_non_text", frame_type=frame.type.name)
                break
            message = json.loads(frame.data)
            connection.frame("application", "TEXT", len(frame.data))
            payload = message.get("data", message)
            if payload.get("e") != "depthUpdate" or payload.get("s") != "BTCUSDT":
                trace.event("comparison_unexpected_message")
                continue
            mismatch = previous is not None and payload["pu"] != previous
            previous = payload["u"]
            stats["messages"] += 1
            stats["payload_bytes"] += len(frame.data.encode())
            stats["sequence_mismatches"] += int(mismatch)
            trace.event(
                "comparison_depth",
                payload=payload,
                receipt_ns=stamp.receipt_ns,
                receipt_monotonic_ns=stamp.monotonic_ns,
                library_frames=dict(connection.last_frames),
                sequence_mismatch=mismatch,
            )
            stats["processing_ns"] += time.monotonic_ns() - stamp.monotonic_ns


async def record(
    directory: Path,
    duration: float,
    mode: str,
    *,
    url: str = PUBLIC_URL,
    session_factory=binance_session,
    target_ip: str | None = None,
) -> dict:
    if not 0 < duration <= 900:
        raise ValueError("one observation must be in (0, 900] seconds")
    if target_ip is not None:
        target_ip = str(ipaddress.IPv4Address(target_ip))
        if url != PUBLIC_URL:
            raise ValueError("peer override is limited to the official public depth URL")
        session_factory = partial(
            same_peer_session, target_ip=target_ip, session_factory=session_factory
        )
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    run_id = uuid.uuid4().hex
    started_wall, started_mono = time.time_ns(), time.monotonic_ns()
    marker = {
        "run_id": run_id,
        "pid": os.getpid(),
        "started_wall_ns": started_wall,
        "started_monotonic_ns": started_mono,
        "deadline_monotonic_ns": started_mono + round(duration * 1e9),
        "duration_seconds": duration,
        "url": url,
        "resolver_mode": mode,
        "aiohttp_version": aiohttp.__version__,
        "heartbeat_seconds": 20,
        "trust_env": False,
        "autoping": True,
        "rest_snapshots": 0,
        "connection_attempt_limit": 1,
        "target_ip": target_ip,
        "override_hostname": PUBLIC_HOST if target_ip is not None else None,
    }
    (directory / "started.json").write_text(json.dumps(marker, indent=2) + "\n")
    trace = WebsocketTrace(directory, run_id)
    stats = {
        "messages": 0,
        "payload_bytes": 0,
        "sequence_mismatches": 0,
        "processing_ns": 0,
        "peer": None,
        "transport": None,
        "peer_control": "not_requested" if target_ip is None else "not_verified",
        "tls": None,
    }
    recorder = asyncio.create_task(trace.run(lambda: tcp_state(stats["transport"])))
    await asyncio.sleep(0)
    connection = trace.connect("binance_public_comparator", url)
    outcome = "window_complete"
    try:
        async with asyncio.timeout_at(marker["deadline_monotonic_ns"] / 1e9):
            await consume(trace, connection, url, mode, stats, session_factory, target_ip)
        outcome = "remote_end_before_deadline"
    except PeerControlFailure as exc:
        outcome = "peer_control_failed"
        trace.event("comparison_failure", exception_type=type(exc).__name__, error=str(exc))
    except TimeoutError as exc:
        if time.monotonic_ns() >= marker["deadline_monotonic_ns"]:
            trace.event("comparison_window_deadline")
        else:
            outcome = "connection_failed"
            trace.event("comparison_failure", exception_type=type(exc).__name__, error=str(exc))
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:
        outcome = "connection_failed"
        trace.event("comparison_failure", exception_type=type(exc).__name__, error=str(exc))
    finally:
        transport = stats.pop("transport")
        trace.event("comparison_finished", outcome=outcome, **stats)
        recorder.cancel()
        await asyncio.gather(recorder, return_exceptions=True)
        final = (
            marker
            | stats
            | {
                "finished_wall_ns": time.time_ns(),
                "finished_monotonic_ns": time.monotonic_ns(),
                "outcome": outcome,
                "telemetry": trace.status(),
                "connection_closed": transport is None or transport.is_closing(),
            }
        )
        (directory / "finished.json").write_text(json.dumps(final, indent=2) + "\n")
        for path in directory.iterdir():
            path.chmod(0o600)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--duration", type=float, default=900)
    parser.add_argument("--resolver-mode", choices=["doh", "system"], required=True)
    parser.add_argument("--target-ip", help="one verified production IPv4; comparator only")
    args = parser.parse_args()
    result = asyncio.run(
        record(args.directory, args.duration, args.resolver_mode, target_ip=args.target_ip)
    )
    print(json.dumps({k: result[k] for k in ["outcome", "messages", "peer", "connection_closed"]}))


if __name__ == "__main__":
    main()
