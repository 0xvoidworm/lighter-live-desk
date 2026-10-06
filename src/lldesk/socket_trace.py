"""Passive socket identity. No network operations, frame reads or transport mutations."""

from __future__ import annotations

import ipaddress
import os
import socket
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Any

FIELDS = ("local", "remote", "address_family", "fd", "inode", "cookie")


def unavailable(reason: str) -> dict:
    return {k: None for k in FIELDS} | {
        "status": "unavailable",
        "missing": {k: reason for k in FIELDS},
    }


def address(value: Any) -> dict | None:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    if not isinstance(value[0], str) or type(value[1]) is not int:
        return None
    result = {"address": str(ipaddress.ip_address(value[0])), "port": value[1]}
    if len(value) == 4:
        result.update(flowinfo=value[2], scope_id=value[3])
    return result


def socket_metadata(ws: Any, library: str) -> dict:
    """Read the connected library transport; never infer endpoints from DNS."""
    try:
        if library == "aiohttp":
            transport = ws._writer.transport
        elif library == "websockets":
            transport = ws.transport
        else:
            return unavailable("unsupported_library")
    except Exception as exc:
        return unavailable(f"transport_access_failed:{type(exc).__name__}")
    if transport is None:
        return unavailable("transport_unavailable")
    try:
        if transport.is_closing():
            return unavailable("transport_closing")
    except Exception as exc:
        return unavailable(f"transport_state_failed:{type(exc).__name__}")
    result = unavailable("not_observed")
    missing = result["missing"]
    try:
        sock = transport.get_extra_info("socket")
    except Exception as exc:
        sock = None
        missing.update({k: f"socket_access_failed:{type(exc).__name__}" for k in FIELDS})
    if sock is not None:
        try:
            fd = sock.fileno()
            if fd < 0:
                return unavailable("socket_closed")
            result["fd"] = fd
            missing.pop("fd")
        except Exception as exc:
            missing["fd"] = f"fileno_failed:{type(exc).__name__}"
        try:
            result["address_family"] = {
                "number": int(sock.family),
                "name": socket.AddressFamily(sock.family).name,
            }
            missing.pop("address_family")
        except Exception as exc:
            missing["address_family"] = f"family_failed:{type(exc).__name__}"
        if result["fd"] is not None:
            try:
                info = os.fstat(result["fd"])
                if stat.S_ISSOCK(info.st_mode):
                    result["inode"] = info.st_ino
                    missing.pop("inode")
                else:
                    missing["inode"] = "fd_is_not_socket"
            except Exception as exc:
                missing["inode"] = f"fstat_failed:{type(exc).__name__}"
        option = getattr(socket, "SO_COOKIE", None)
        if option is None:
            missing["cookie"] = "SO_COOKIE_not_exposed_by_installed_python"
        else:
            try:
                result["cookie"] = str(
                    struct.unpack("=Q", sock.getsockopt(socket.SOL_SOCKET, option, 8))[0]
                )
                missing.pop("cookie")
            except Exception as exc:
                missing["cookie"] = f"cookie_failed:{type(exc).__name__}"
    else:
        for key in ("address_family", "fd", "inode", "cookie"):
            if missing[key] == "not_observed":
                missing[key] = "socket_unavailable"
    for key, extra in [("local", "sockname"), ("remote", "peername")]:
        try:
            result[key] = address(transport.get_extra_info(extra))
            if result[key] is None:
                missing[key] = "endpoint_unavailable"
            else:
                missing.pop(key)
        except Exception as exc:
            missing[key] = f"endpoint_failed:{type(exc).__name__}"
    result["status"] = (
        "available"
        if all(result[k] is not None for k in ("local", "remote", "address_family"))
        else "partial"
        if any(result[k] is not None for k in FIELDS)
        else "unavailable"
    )
    return result


def _proc_endpoint(raw: str, family: socket.AddressFamily) -> dict:
    host, port = raw.split(":")
    packed = bytes.fromhex(host)
    # procfs prints host-order u32 words, including four words for IPv6.
    if sys.byteorder == "little":
        packed = b"".join(packed[i : i + 4][::-1] for i in range(0, len(packed), 4))
    return {"address": socket.inet_ntop(family, packed), "port": int(port, 16)}


def process_socket_observation(pid: int) -> dict:
    """Diagnostic caller only: bounded procfs read, never scheduled by the engine."""
    if pid <= 0:
        raise ValueError("positive PID required")
    started, wall = time.monotonic_ns(), time.time_ns()
    root = Path(f"/proc/{pid}")
    owners: dict[int, list[int]] = {}
    errors, warnings, records = [], [], []
    try:
        for entry in (root / "fd").iterdir():
            try:
                target = entry.readlink().name
                if target.startswith("socket:[") and target.endswith("]"):
                    owners.setdefault(int(target[8:-1]), []).append(int(entry.name))
            except (OSError, ValueError):
                warnings.append({"fd": int(entry.name), "reason": "fd_disappeared_or_unreadable"})
    except OSError as exc:
        errors.append(f"fd_listing_failed:{type(exc).__name__}")
    for name, family in [("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)]:
        try:
            lines = (root / "net" / name).read_text().splitlines()[1:]
        except OSError as exc:
            errors.append(f"{name}_unavailable:{type(exc).__name__}")
            continue
        for line in lines:
            parts = line.split()
            inode = int(parts[9])
            if inode not in owners:
                continue

            records.append(
                {
                    "pid": pid,
                    "inode": inode,
                    "fds": owners[inode],
                    "local": _proc_endpoint(parts[1], family),
                    "remote": _proc_endpoint(parts[2], family),
                    "address_family": int(family),
                    "tcp_state_hex": parts[3],
                }
            )
    return {
        "pid": pid,
        "started_monotonic_ns": started,
        "wall_ns": wall,
        "finished_monotonic_ns": time.monotonic_ns(),
        "records": records,
        "errors": errors,
        "warnings": warnings,
    }


def match_socket_observation(
    event: dict,
    observation: dict,
    close_monotonic_ns: int | None = None,
    *,
    active_at_observation: bool = False,
) -> dict:
    """Match PID + exact tuple + lifetime; FD alone is never historical identity."""
    metadata = event.get("socket_metadata")
    if not metadata or any(metadata.get(k) is None for k in ("local", "remote", "address_family")):
        return {"status": "unavailable", "reason": "complete_socket_tuple_not_observed"}
    if close_monotonic_ns is None and not active_at_observation:
        return {"status": "ambiguous", "reason": "connection_lifetime_not_verified"}
    start, end = observation["started_monotonic_ns"], observation["finished_monotonic_ns"]
    if (
        observation["pid"] != event["pid"]
        or start < event["monotonic_ns"]
        or end < start
        or (close_monotonic_ns is not None and end >= close_monotonic_ns)
    ):
        return {"status": "ambiguous", "reason": "PID_or_observation_outside_proven_lifetime"}
    if observation["errors"]:
        return {"status": "ambiguous", "reason": "OS_observation_incomplete"}
    if any(w.get("fd") == metadata["fd"] for w in observation.get("warnings", [])):
        return {"status": "ambiguous", "reason": "matching_FD_changed_during_OS_observation"}

    def same_endpoint(a, b):
        return a["address"] == b["address"] and a["port"] == b["port"]

    matches = [
        r
        for r in observation["records"]
        if r["pid"] == event["pid"]
        and r["address_family"] == metadata["address_family"]["number"]
        and same_endpoint(r["local"], metadata["local"])
        and same_endpoint(r["remote"], metadata["remote"])
        and (metadata["inode"] is None or metadata["inode"] == r["inode"])
        and (metadata["fd"] is None or metadata["fd"] in r["fds"])
    ]
    if len(matches) != 1:
        return {
            "status": "ambiguous",
            "reason": "zero_or_multiple_matching_OS_sockets",
            "candidate_count": len(matches),
        }
    return {
        "status": "matched",
        "reason": "unique_PID_tuple_with_verified_lifetime",
        "checks": ["pid", "tuple", "address_family", "lifetime"]
        + [k for k in ("inode", "fd") if metadata[k] is not None],
        "connection_id": event["connection_id"],
        "inode": matches[0]["inode"],
    }
