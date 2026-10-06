"""One public depth connection, no REST snapshot, account, signer or engine.

Use the production network factory and passive socket hooks unchanged. The
deadline includes handshake; failures are retained and never reconnect here.
Python is required to match the installed production aiohttp/resolver exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import struct
import time
import uuid
from functools import partial
from pathlib import Path

import aiohttp

from lldesk.models import Stamp
from lldesk.network import binance_session
from lldesk.websocket_trace import ObservedAiohttpSocket, WebsocketTrace

PUBLIC_URL = "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms"


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


async def consume(trace, connection, url: str, mode: str, stats: dict, session_factory):
    async with (
        session_factory(
            mode, ws_response_class=partial(ObservedAiohttpSocket, evidence=connection)
        ) as client,
        client.ws_connect(url, heartbeat=20) as ws,
        connection.scope(),
    ):
        connection.event("handshake_completed")
        transport = ws._writer.transport
        stats["peer"] = tcp_state(transport)["peer"]
        stats["transport"] = transport
        trace.event("comparison_peer", **tcp_state(transport), tls_hostname_validation=True)
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
) -> dict:
    if not 0 < duration <= 900:
        raise ValueError("one observation must be in (0, 900] seconds")
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
    }
    recorder = asyncio.create_task(trace.run(lambda: tcp_state(stats["transport"])))
    await asyncio.sleep(0)
    connection = trace.connect("binance_public_comparator", url)
    outcome = "window_complete"
    try:
        async with asyncio.timeout_at(marker["deadline_monotonic_ns"] / 1e9):
            await consume(trace, connection, url, mode, stats, session_factory)
        outcome = "remote_end_before_deadline"
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
    args = parser.parse_args()
    result = asyncio.run(record(args.directory, args.duration, args.resolver_mode))
    print(json.dumps({k: result[k] for k in ["outcome", "messages", "peer", "connection_closed"]}))


if __name__ == "__main__":
    main()
