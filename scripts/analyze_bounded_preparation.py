"""Offline summary of a preparation that never activated a trading session."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import sqlite3
from bisect import bisect_right
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def analyze_depth_ab(directory: Path, root: Path) -> dict:
    """Offline A/B transport analysis; no engine/SDK imports or network calls."""
    start = json.loads((directory / "started.json").read_text())
    finish = json.loads((directory / "finished.json").read_text())
    end = start["deadline_epoch"]
    app, clocks, clock_times, clock_attempts = [], [], [], {}
    with (directory / "app.jsonl").open() as source:
        for line in source:
            row = json.loads(line)
            if row["kind"] != "app_snapshot":
                continue
            s = row["body"]["status"]
            app.append((row["at"], s))
            clocks.append(s.get("source_clock", {}).get("evidence"))
            clock_times.append(row["at"])
            for attempt in s.get("source_clock", {}).get("recent", []):
                clock_attempts[attempt["id"]] = attempt

    def distribution(values):
        values = sorted(values)
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "min": values[0],
            "p50": values[int((len(values) - 1) * 0.50)],
            "p95": values[int((len(values) - 1) * 0.95)],
            "p99": values[int((len(values) - 1) * 0.99)],
            "max": values[-1],
        }

    def bound(at):
        index = bisect_right(clock_times, at) - 1
        if index < 0:
            return None
        c = clocks[index]
        if not c or not c["host"]["synchronized"]:
            return None
        age = at - c["received_ns"] / 1e9
        if not 0 <= age <= 120:
            return None
        drift = age * 0.1  # Same explicit 100ppm allowance as runtime, ms.
        low, high = c["offset_low_ms"] - drift, c["offset_high_ms"] + drift
        if (high - low) / 2 > 250:
            return None
        if (
            app[index][1]["source_freshness"]["reason"]
            == "SOURCE_CLOCK_WALL_STEP_OR_INCONSISTENT_STAMP"
        ):
            return None
        return low, high

    blockers, overlap, source_reasons = Counter(), Counter(), Counter()
    ready_s = streak = longest = receipt_new_but_source_late_s = 0.0
    for i, (at, s) in enumerate(app):
        duration = max(0, min(end, app[i + 1][0] if i + 1 < len(app) else end) - at)
        blocked = tuple(s["diagnostic_observation"]["blockers"])
        overlap[blocked] += duration
        for b in blocked:
            blockers[b] += duration
        if not blocked:
            ready_s += duration
            streak += duration
            longest = max(longest, streak)
        else:
            streak = 0.0
        sf = s["source_freshness"]
        source_reasons[sf["reason"]] += duration
        if sf["reason"] == "SOURCE_DATA_LATE" and sf["receipt_age_ms"] < 1500:
            receipt_new_but_source_late_s += duration

    arms, depth_rows = {}, {}
    for arm in ["depth", "combined"]:
        counts, streams, payload, lag, corrected, events, peers = (
            Counter(),
            Counter(),
            0,
            {},
            {},
            [],
            set(),
        )
        depths, last, gaps, parse_ns, stale, wall_discrepancy_ms = {}, {}, [], [], Counter(), []
        parse_groups = {}
        first_at = first_elapsed = None
        with (directory / f"{arm}.jsonl").open() as source:
            for line in source:
                row = json.loads(line)
                if row["at"] > end + 0.025:
                    continue  # Exclude completion bookkeeping; never extend the window.
                b, kind = row["body"], row["kind"]
                counts[kind] += 1
                if first_at is None:
                    first_at, first_elapsed = row["at"], row["elapsed"]
                wall_discrepancy_ms.append(
                    abs(row["at"] - first_at - row["elapsed"] + first_elapsed) * 1000
                )
                if kind != "message":
                    events.append(row)
                    if kind == "tcp_connected":
                        peers.add(b["peer"])
                    continue
                received = b.get("received_at", row["at"])
                streams[b["event"]] += 1
                payload += b["bytes"]
                parse_ns.append(b.get("parse_ns", 0))
                parse_groups.setdefault(b["event"], []).append(b.get("parse_ns", 0))
                previous = last.get(b["connection"])
                if previous is not None:
                    gaps.append(received - previous)
                last[b["connection"]] = received
                bounds = bound(received)
                for key in ["E", "T"]:
                    if type(b.get(key)) is not int:
                        stale[f"{b['event']}:{key}:missing"] += 1
                        continue
                    raw_ms = (received - b[key] / 1000) * 1000
                    group = b["event"] + ":" + key
                    lag.setdefault(group, []).append(raw_ms)
                    if bounds and wall_discrepancy_ms[-1] <= 25:
                        low, high = raw_ms + bounds[0], raw_ms + bounds[1]
                        corrected.setdefault(group + ":lower", []).append(low)
                        corrected.setdefault(group + ":upper", []).append(high)
                        state = (
                            "late"
                            if high >= 1500
                            else "future_uncertain"
                            if low < -1
                            else "fresh_bound"
                        )
                        stale[group + ":" + state] += 1
                    else:
                        stale[group + ":clock_unknown"] += 1
                if b["event"] == "depthUpdate" and b.get("st", 1) in {1, None}:
                    depths[(b["u"], b.get("T"))] = {
                        "at": received,
                        "economics": [b.get("U"), b.get("pu"), b["bid"], b["ask"]],
                        "connection": b["connection"],
                        "sequence_gap": b["sequence_gap"],
                        "T": b.get("T"),
                        "E": b.get("E"),
                        "parse_ns": b.get("parse_ns"),
                    }
        marker = directory / f"{arm}-finished.json"
        arms[arm] = {
            "counts": dict(counts),
            "streams": dict(streams),
            "payload_bytes": payload,
            "log_bytes": (directory / f"{arm}.jsonl").stat().st_size,
            "source_indicator_ms": {k: distribution(v) for k, v in lag.items()},
            "source_clock_bound_ms": {k: distribution(v) for k, v in corrected.items()},
            "source_classifications": dict(stale),
            "parse_and_field_ns": distribution(parse_ns),
            "parse_and_field_by_event_ns": {k: distribution(v) for k, v in parse_groups.items()},
            "within_connection_message_gap_s": distribution(gaps),
            "sequence_gaps": sum(r["sequence_gap"] for r in depths.values()),
            "clock_wall_vs_monotonic_max_ms": max(wall_discrepancy_ms, default=0),
            "destination_peers": sorted(peers),
            "events": events,
            "finish": json.loads(marker.read_text()) if marker.exists() else None,
        }
        depth_rows[arm] = depths
    common = set(depth_rows["depth"]) & set(depth_rows["combined"])
    paired = [
        (depth_rows["combined"][k]["at"] - depth_rows["depth"][k]["at"]) * 1000 for k in common
    ]
    hashes = {}
    for name in ["started.json", "finished.json", "app.jsonl", "depth.jsonl", "combined.jsonl"]:
        digest = hashlib.sha256()
        with (directory / name).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[name] = digest.hexdigest()
    binary = root / "diagnostics/binance-ws-compare/target/release/binance-ws-compare"

    def state_summary(state):
        keys = [
            "engine_pid",
            "state",
            "allow_live_orders",
            "account",
            "quotes",
            "coverage",
            "source_freshness",
            "net_cash_audit",
            "request_budget",
            "market_processing",
            "queue_backlog",
            "event_loop_lag_ms",
        ]
        result = {key: state[key] for key in keys if key in state}
        result["session"] = {
            key: state.get("strategy_session", {}).get(key) for key in ["id", "status"]
        }
        result["history_complete"] = state.get("history_audit", {}).get("complete")
        result["feed_reconnects"] = {
            key: value.get("reconnects") for key, value in state.get("feed_channels", {}).items()
        }
        return result

    return {
        "start": start,
        "finish": finish,
        "raw_directory": str(directory),
        "sha256": hashes,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()
        if binary.exists()
        else None,
        "arms": arms,
        "paired_depth": {
            "common_ids_T": len(common),
            "economic_payload_mismatches": sum(
                depth_rows["depth"][k]["economics"] != depth_rows["combined"][k]["economics"]
                for k in common
            ),
            "combined_minus_depth_receipt_ms": distribution(paired),
            "largest_receipt_difference": [
                {
                    "id_T": list(k),
                    "depth": depth_rows["depth"][k],
                    "combined": depth_rows["combined"][k],
                }
                for k in sorted(
                    common,
                    key=lambda k: depth_rows["combined"][k]["at"] - depth_rows["depth"][k]["at"],
                    reverse=True,
                )[:5]
            ],
        },
        "clock_attempts": list(clock_attempts.values()),
        "engine": {
            "technical_ready_s": ready_s,
            "longest_ready_s": longest,
            "blocker_s": dict(blockers),
            "source_reason_s": dict(source_reasons),
            "receipt_new_but_source_late_s": receipt_new_but_source_late_s,
            "overlap_s": [{"blockers": list(k), "seconds": v} for k, v in overlap.items()],
            "first": state_summary(app[0][1]) if app else None,
            "last": state_summary(app[-1][1]) if app else None,
        },
        "limitations": [
            "One pair/same IP; different upstream workers/routes remain possible",
            "No REST bootstrap; sequence gaps only checked within each connection",
            "Source indicators are cross-clock; bounds assume UTC USD-M REST/WS clock domain",
            "No one-way network or fill latency measurement; no order transport",
            "Parse timing excludes body construction; logging includes serialize/write/flush",
            "Readiness integration uses 1Hz sampled engine state; blocker durations overlap",
        ],
    }


def analyze_comparison(directory: Path, root: Path) -> dict:
    """One independent read-only run; local telemetry is sampled, not fill latency."""
    start = json.loads((directory / "started.json").read_text())
    finish = json.loads((directory / "finished.json").read_text())
    kinds, overlap, blocker_s = Counter(), Counter(), Counter()
    minimal, events, app, requests, peers = {}, [], [], {}, set()
    source_lags, app_source_lags, relative_lags = {}, {}, []
    process_peaks, message_last, previous, streak, longest = {}, {}, None, 0.0, 0.0

    def accumulate(status, seconds):
        nonlocal streak, longest
        blockers = tuple(status["diagnostic_observation"]["blockers"])
        overlap[blockers] += seconds
        for blocker in blockers:
            blocker_s[blocker] += seconds
        streak = 0.0 if blockers else streak + seconds
        longest = max(longest, streak)

    with (directory / "trace.jsonl").open() as source:
        for line in source:
            row = json.loads(line)
            kind, body = row["kind"], row["body"]
            kinds[kind] += 1
            if kind == "message":
                connection = minimal.setdefault(
                    body["connection"],
                    {
                        "messages": 0,
                        "streams": Counter(),
                        "max_message_gap_s": 0,
                        "sequence_gaps": 0,
                    },
                )
                connection["messages"] += 1
                connection["streams"][body["stream"]] += 1
                connection["sequence_gaps"] += int(body["sequence_gap"])
                last = message_last.get(body["connection"])
                if last is not None:
                    connection["max_message_gap_s"] = max(
                        connection["max_message_gap_s"], row["elapsed"] - last
                    )
                message_last[body["connection"]] = row["elapsed"]
                connection.setdefault("first_message_epoch", row["at"])
                connection["last_message_epoch"] = row["at"]
                connection["last_source_event_ms"] = body.get("E")
                if isinstance(body.get("E"), (int, float)):
                    source_lags.setdefault(body["event"], []).append(row["at"] - body["E"] / 1000)
            elif kind == "app_snapshot":
                status = body["status"]
                if previous:
                    accumulate(previous["status"], row["elapsed"] - previous["elapsed"])
                previous = {"status": status, "elapsed": row["elapsed"]}
                for request in status["binance_transport_trace"]["recent"]:
                    requests[request["id"]] = request
                decoded = []
                for socket in body["sockets"]:
                    raw, port = socket["remote_hex"].split(":")
                    if int(port, 16) != 443:
                        continue
                    octets = (
                        bytes.fromhex(raw)[::-1]
                        if socket["family"] == "tcp"
                        else b"".join(
                            bytes.fromhex(raw[index : index + 8])[::-1] for index in range(0, 32, 8)
                        )
                    )
                    peer = str(ipaddress.ip_address(octets))
                    peers.add(peer)
                    decoded.append(
                        {
                            "ip": peer,
                            "inode": socket["inode"],
                            "family": socket["family"],
                            "local_hex": socket["local_hex"],
                            "state": socket["state"],
                        }
                    )
                for name, metric in status["market_processing"].items():
                    received = metric.get("receipt_monotonic_ns")
                    queued = metric.get("queued_monotonic_ns")
                    processed = metric.get("processing_started_monotonic_ns")
                    finished = metric.get("processing_finished_monotonic_ns")
                    if (
                        received is not None
                        and queued is not None
                        and processed is not None
                        and finished is not None
                    ):
                        values = {
                            "receive_to_enqueue_ms": (queued - received) / 1e6,
                            "queue_wait_ms": (processed - queued) / 1e6,
                            "processing_ms": (finished - processed) / 1e6,
                        }
                        peak = process_peaks.setdefault(name, {})
                        for key, value in values.items():
                            peak[key] = max(peak.get(key, 0), value)
                    if metric.get("receipt_ns") and metric.get("source_exchange_ms"):
                        app_source_lags.setdefault(name, []).append(
                            metric["receipt_ns"] / 1e9 - metric["source_exchange_ms"] / 1000
                        )
                public = status["market_processing"].get("binance:depthUpdate", {})
                market = status["market_processing"].get("binance:aggTrade", {})
                if (
                    public.get("source_exchange_ms")
                    and market.get("source_exchange_ms")
                    and abs(public["receipt_ns"] - market["receipt_ns"]) < 1e9
                ):
                    relative_lags.append(
                        (public["receipt_ns"] - market["receipt_ns"]) / 1e9
                        - (public["source_exchange_ms"] - market["source_exchange_ms"]) / 1000
                    )
                app.append(
                    {
                        "at": row["at"],
                        "elapsed": row["elapsed"],
                        "channels": status["feed_channels"],
                        "sockets": decoded,
                        "queue": status["queue_backlog"],
                        "event_loop_lag_ms": status["event_loop_lag_ms"],
                        "fx_age_s": status["diagnostic_observation"]["fx_age_s"],
                    }
                )
            else:
                events.append(row)
    if previous:
        accumulate(
            previous["status"],
            max(0, min(start["seconds"], finish["elapsed"]) - previous["elapsed"]),
        )
    handshakes = sorted(
        (request for request in requests.values() if request["path"] == "/public/stream"),
        key=lambda request: request["started_at"],
    )
    public_events = []
    old = app[0]["channels"]["binance_public"]["reconnects"] if app else None
    for sample in app:
        channel = sample["channels"]["binance_public"]
        if channel["reconnects"] == old:
            continue
        disconnected = channel["last_disconnect_at"]
        preceding = [
            request
            for request in handshakes
            if request.get("finished_at", float("inf")) < disconnected
        ]
        connected = preceding[-1] if preceding else None
        following = next(
            (request for request in handshakes if request["started_at"] > disconnected), None
        )
        public_events.append(
            {
                "counter_before": old,
                "counter_after": channel["reconnects"],
                "at": disconnected,
                "connection_id": f"pid{start['engine_pid']}:http{connected['id']}"
                if connected
                else None,
                "connected_at": connected.get("finished_at") if connected else None,
                "age_s": disconnected - connected["finished_at"] if connected else None,
                "exception": channel["last_error"],
                "detail": channel["last_transport_detail"],
                "retry_s": channel["retry_delay_s"],
                "next_handshake": following,
                "queue": sample["queue"],
                "lag_ms": sample["event_loop_lag_ms"],
            }
        )
        old = channel["reconnects"]
    with sqlite3.connect(
        "file:" + str(root / "state/desk.db") + "?mode=ro", uri=True
    ) as connection:
        gap_events = list(
            connection.execute(
                "SELECT at,kind,body FROM events WHERE at BETWEEN ? AND ? "
                "AND kind='BOOK_GAP' ORDER BY at",
                (start["at"], finish["at"]),
            )
        )
        command_count = connection.execute(
            "SELECT COUNT(*) FROM commands WHERE requested_at BETWEEN ? AND ?",
            (start["at"], finish["at"]),
        ).fetchone()[0]

    def distribution(values):
        ordered = sorted(values)
        return (
            {
                "n": len(ordered),
                **{
                    name: ordered[int(q * (len(ordered) - 1))]
                    for name, q in [
                        ("min", 0),
                        ("p50", 0.5),
                        ("p95", 0.95),
                        ("p99", 0.99),
                        ("max", 1),
                    ]
                },
            }
            if ordered
            else None
        )

    return {
        "started": start,
        "finished": finish,
        "record_kinds": dict(kinds),
        "minimal_connections": minimal,
        "minimal_nonmessage_events": events,
        "app_channel_initial": app[0]["channels"] if app else None,
        "app_channel_final": app[-1]["channels"] if app else None,
        "app_public_reconnects": public_events,
        "app_socket_observations": app,
        "app_443_peer_candidates": sorted(peers),
        "technical_ready_s": overlap.get((), 0),
        "longest_technical_ready_s": longest,
        "blocker_s": dict(blocker_s),
        "overlap_s": [
            {"blockers": list(key), "seconds": seconds} for key, seconds in overlap.items()
        ],
        "sampled_last_event_processing_peak_ms": process_peaks,
        "minimal_source_event_to_receive_indicator_s": {
            name: distribution(values) for name, values in source_lags.items()
        },
        "app_sampled_source_to_receive_indicator_s": {
            name: distribution(values) for name, values in app_source_lags.items()
        },
        "public_minus_market_source_to_receive_indicator_s": distribution(relative_lags),
        "source_time_limits": (
            "Exchange vs host clock offset unknown; these are delay indicators, "
            "not one-way network or fill latency. Relative Binance public/market "
            "indicator removes a common exchange/host clock offset, using receipts "
            "less than 1s apart. App distributions are 1Hz sampled last events, "
            "not all messages."
        ),
        "sequence_recovery_events": gap_events,
        "runtime_command_count": command_count,
        "sampling_limits": (
            "Local snapshots 1 Hz; only sampled last processing metrics, socket "
            "ownership does not prove hostname/channel. No wire packet capture. "
            "Initial partial app connection and final ongoing connection are censored."
        ),
        "raw_sha256": hashlib.sha256((directory / "trace.jsonl").read_bytes()).hexdigest(),
    }


def reconnect_timeline(directory: Path, root: Path) -> dict:
    """Reconstruct existing telemetry; never contact an exchange or assume 1006 ownership."""
    rows = [json.loads(line) for line in gzip.open(directory / "snapshots.jsonl.gz", "rt")]
    rows = [row for row in rows if "status" in row]
    requests = {
        request["id"]: request
        for row in rows
        for request in row["status"]["binance_transport_trace"]["recent"]
    }
    handshakes = sorted(
        (request for request in requests.values() if request["path"] == "/public/stream"),
        key=lambda request: request["started_at"],
    )
    start = json.loads((directory / "started.json").read_text())
    finish = json.loads((directory / "finished.json").read_text())
    previous = rows[0]["status"]["feed_channels"]["binance_public"]["reconnects"]
    timeline = []
    for row in rows:
        status = row["status"]
        channel = status["feed_channels"]["binance_public"]
        if channel["reconnects"] == previous:
            continue
        at = channel["last_disconnect_at"]
        preceding = [
            request for request in handshakes if request.get("finished_at", float("inf")) < at
        ]
        connection = preceding[-1] if preceding else None
        following = next((request for request in handshakes if request["started_at"] > at), None)
        last = next(
            (
                sample
                for sample in reversed(rows)
                if sample["epoch"] < at
                and sample["status"]["feed_channels"]["binance_public"]["connected"]
            ),
            None,
        )
        restored = next(
            (
                sample
                for sample in rows
                if sample["epoch"] > at and sample["status"]["binance_streams"]["public"]
            ),
            None,
        )
        timeline.append(
            {
                "counter_before": previous,
                "counter_after": channel["reconnects"],
                "counter_delta": channel["reconnects"] - previous,
                "connection_id": f"pid{start['engine_pid']}:http{connection['id']}"
                if connection
                else None,
                "connection_id_basis": (
                    "derived HTTP handshake trace identity; not a native WS UUID"
                ),
                "connected_at": connection.get("finished_at") if connection else None,
                "disconnect_at": at,
                "connection_age_s": at - connection["finished_at"] if connection else None,
                "exception": channel["last_error"],
                "transport_detail": channel["last_transport_detail"],
                "close_owner": "not recorded; 1006 is not a received close frame",
                "last_connected_status_at": last["epoch"] if last else None,
                "last_frame_age_ms_at_last_status": last["status"]["feed_channels"][
                    "binance_public"
                ]["last_frame_age_ms"]
                if last
                else None,
                "queue_at_first_disconnect_status": status["queue_backlog"],
                "event_loop_lag_ms_at_first_disconnect_status": status["event_loop_lag_ms"],
                "heartbeat_frames": (
                    "not recorded; aiohttp autoping handles control frames internally"
                ),
                "configured_backoff_s": channel["retry_delay_s"],
                "actual_disconnect_to_next_handshake_start_s": following["started_at"] - at
                if following
                else None,
                "next_handshake": following,
                "first_public_valid_status_at": restored["epoch"] if restored else None,
                "valid_status_observation_precision": (
                    "collector sampling; not exact receive/apply timestamp"
                ),
            }
        )
        previous = channel["reconnects"]
    with sqlite3.connect(
        "file:" + str(root / "state/desk.db") + "?mode=ro", uri=True
    ) as connection:
        gap_events = list(
            connection.execute(
                "SELECT at,kind,body FROM events WHERE at BETWEEN ? AND ? "
                "AND kind='BOOK_GAP' ORDER BY at",
                (start["preparation_started_epoch"], finish["session"]["ended_at"]),
            )
        )
    return {
        "counter_initial": rows[0]["status"]["feed_channels"]["binance_public"]["reconnects"],
        "counter_final": previous,
        "observed_reconnects": sum(event["counter_delta"] for event in timeline),
        "capture_first_epoch": rows[0]["epoch"],
        "capture_last_epoch": rows[-1]["epoch"],
        "uncaptured_initial_preparation_s": rows[0]["epoch"] - start["preparation_started_epoch"],
        "url": "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/btcusdt@depth@100ms",
        "subscription_mode": "implicit combined URL, no JSON SUBSCRIBE messages",
        "sequence_recovery_events": gap_events,
        "timeline": timeline,
        "missing_evidence": [
            "full original exception/stack",
            "peer IP",
            "server/client close frame",
            "ping/pong frames",
            "continuous initial preparation status",
        ],
    }


def add_raw_reconnect_evidence(result: dict, root: Path) -> None:
    """Replay only the existing public Book implementation; no engine/signing/network."""
    from lldesk.market import Book, GapError
    from lldesk.models import Stamp

    begin, end = result["capture_first_epoch"], result["capture_last_epoch"]
    hours = {datetime.fromtimestamp(point, UTC).strftime("%Y%m%d-%H") for point in (begin, end)}
    pid = int(result["timeline"][0]["connection_id"].split(":")[0][3:])
    book = Book("binance")
    last_message, last_valid, gaps = None, None, 0
    evidence = [{} for _ in result["timeline"]]
    for path in sorted((root / "state/market").glob("*.jsonl.gz")):
        if not any("market-" + hour + "-" in path.name for hour in hours):
            continue
        with gzip.open(path, "rt") as source:
            for line in source:
                row = json.loads(line)
                if (
                    row.get("pid") != pid
                    or row.get("venue") != "binance"
                    or row["kind"] == "aggTrade"
                ):
                    continue
                processed = row["recorded_ns"] / 1e9
                if processed > end:
                    break
                stamp = Stamp(
                    receipt_ns=row["receipt_ns"],
                    monotonic_ns=row["monotonic_ns"],
                    exchange_ms=row.get("exchange_ms"),
                )
                brief = {
                    "kind": row["kind"],
                    "received_epoch": row["receipt_ns"] / 1e9,
                    "recorded_before_apply_epoch": processed,
                    "source_exchange_ms": row.get("exchange_ms"),
                    "receipt_to_record_ms": (row["recorded_monotonic_ns"] - row["monotonic_ns"])
                    / 1e6,
                    "update_id": row["payload"].get("u", row["payload"].get("lastUpdateId")),
                }
                try:
                    if row["kind"] == "disconnect" and row["payload"].get("channel") == "public":
                        book.invalidate()
                    elif row["kind"] == "snapshot":
                        book.binance_snapshot(row["payload"], stamp)
                    elif row["kind"] == "depthUpdate":
                        book.binance_delta(row["payload"], stamp)
                except GapError:
                    gaps += int(processed >= begin)
                if row["kind"] in {"bookTicker", "depthUpdate"}:
                    last_message = brief
                if row["kind"] == "depthUpdate" and book.valid and book.bridged:
                    last_valid = brief
                for event, raw in zip(result["timeline"], evidence, strict=True):
                    at = event["disconnect_at"]
                    following = event["next_handshake"]
                    if processed <= at:
                        raw["last_received_public_message"] = last_message
                        raw["last_valid_depth"] = last_valid
                    if processed >= at and following:
                        if row["kind"] == "disconnect" and "disconnect_record" not in raw:
                            raw["disconnect_record"] = brief
                        if row["kind"] == "snapshot" and processed >= following["finished_at"]:
                            raw.setdefault("next_snapshot", brief)
                        if (
                            row["kind"] in {"bookTicker", "depthUpdate"}
                            and row["receipt_ns"] / 1e9 >= following["finished_at"]
                        ):
                            raw.setdefault("first_next_received_public_message", brief)
                        if (
                            row["kind"] == "depthUpdate"
                            and book.valid
                            and book.bridged
                            and processed >= following["finished_at"]
                        ):
                            raw.setdefault("first_next_valid_depth", brief)
    for event, raw in zip(result["timeline"], evidence, strict=True):
        event["raw_evidence"] = raw
    result["raw_replay_sequence_errors"] = gaps
    result["raw_replay_limits"] = (
        "Book-only deterministic replay, not a full evaluator/FX replay. "
        "recorded_ns is before apply; received stamp is preserved for buffered data. "
        "No NIC wire timestamps."
    )


def analyze(directory: Path, root: Path) -> dict:
    start = json.loads((directory / "started.json").read_text())
    finish = json.loads((directory / "finished.json").read_text())
    record = finish["session"]
    assert not finish["active_seen"] and record["started_at"] is None
    assert record["deadline_epoch"] is None and record["status"] == "closed"
    assert not record["attempts"] and finish["source_hashes_unchanged"]
    seconds, overlap, evaluations, reasons = Counter(), Counter(), set(), Counter()
    failures, market_intervals, latest = {}, [], None
    peak_used, minimum_exit_capacity, component_max = 0, 55, {}
    fx_max_age, max_sample_gap, longest = 0.0, 0.0, 0.0
    signal_count, eligible_residuals, quotes_invalid_ms = 0, [], {}
    last_market_state = None
    with gzip.open(directory / "snapshots.jsonl.gz", "rt") as source:
        for line in source:
            row = json.loads(line)
            if "status" not in row:
                continue
            status = row["status"]
            observation = status["diagnostic_observation"]
            if latest:
                dt = row["epoch"] - latest["epoch"]
                blockers = tuple(latest["status"]["diagnostic_observation"]["blockers"])
                overlap[blockers] += dt
                for blocker in blockers:
                    seconds[blocker] += dt
                if not blockers:
                    seconds["technical_ready"] += dt
                    longest += dt
            decision = status["decision"]
            key = status["decision_inputs"].get("monotonic_ns")
            if key not in evaluations:
                evaluations.add(key)
                reasons.update(decision["reasons"])
                signal_count += int(decision["side"] is not None)
                if observation["technical_ready"] and observation["decision_input_match"]:
                    if decision["deviation_bps"] is not None and decision["basis_bps"] is not None:
                        eligible_residuals.append(
                            float(decision["deviation_bps"]) - float(decision["basis_bps"])
                        )
            budget = status["request_budget"]
            used = budget["used_last_60s"] + budget["reserved_unconsumed"]
            peak_used = max(peak_used, used)
            minimum_exit_capacity = min(minimum_exit_capacity, 55 - used)
            for name, age in status["account"]["component_ages_s"].items():
                component_max[name] = max(component_max.get(name, 0), age)
            fx_max_age = max(fx_max_age, observation["fx_age_s"] or 0)
            max_sample_gap = max(max_sample_gap, status["coverage"]["max_sample_gap_s"])
            for request in status["fx_diagnostic"]["recent"]:
                if request.get("outcome") == "failed":
                    failures[request["id"]] = request
            market_state = tuple(
                name
                for name in ("market_data", "fx_normalization")
                if not observation["checks"][name]
            )
            if market_state != last_market_state:
                market_intervals.append(
                    {
                        "at_utc": datetime.fromtimestamp(row["epoch"], UTC).isoformat(),
                        "blockers": list(market_state),
                        "quote_ages_ms": {
                            name: quote["age_ms"] if quote else None
                            for name, quote in observation["quotes"].items()
                        },
                        "queue_backlog": status["queue_backlog"],
                        "event_loop_lag_ms": status["event_loop_lag_ms"],
                    }
                )
                last_market_state = market_state
            for name, quote in observation["quotes"].items():
                if quote:
                    quotes_invalid_ms[name] = max(quotes_invalid_ms.get(name, 0), quote["age_ms"])
            latest = row
    assert not eligible_residuals and signal_count == 0 and seconds["technical_ready"] == 0
    connection = sqlite3.connect("file:" + str(root / "state/desk.db") + "?mode=ro", uri=True)
    commands = list(
        connection.execute(
            "SELECT id,kind,state,requested_at,result FROM commands "
            "WHERE requested_at>=? ORDER BY id",
            (start["preparation_started_epoch"],),
        )
    )
    new_intents = list(
        connection.execute(
            "SELECT client_id FROM intents WHERE created_at>=?", (record["authorized_at"],)
        )
    )
    new_fills = list(
        connection.execute(
            "SELECT fill_id FROM fills WHERE received_at>=?", (record["authorized_at"],)
        )
    )
    assert not new_intents and not new_fills
    assert not any(command[1] in {"START_LIVE", "PROBE_ONCE"} for command in commands)
    connection.close()
    final = finish["final_status"]
    return {
        "result": "preparation_blocked_no_activation_no_order",
        "session_id": record["id"],
        "session_record": record,
        "basis_commits": ["283ab14", "5dfd09b"],
        "engine_pid": start["engine_pid"],
        "source_hashes": start["source_hashes"],
        "source_hashes_unchanged": True,
        "frozen_config": start["config"],
        "preparation_started_utc": datetime.fromtimestamp(
            start["preparation_started_epoch"], UTC
        ).isoformat(),
        "preparation_closed_utc": datetime.fromtimestamp(record["ended_at"], UTC).isoformat(),
        "preparation_seconds": record["ended_at"] - start["preparation_started_epoch"],
        "preparation_deadline_utc": datetime.fromtimestamp(
            start["preparation_deadline_epoch"], UTC
        ).isoformat(),
        "active_seconds": 0,
        "entry_attempts": 0,
        "new_intents": 0,
        "new_fills": 0,
        "positions_completed": 0,
        "signal_valid_observed": 0,
        "observed_evaluations": len(evaluations),
        "sampled_filter_counts": dict(reasons),
        "active_eligible_residual_distribution": None,
        "preparation_eligible_residual_count": 0,
        "lost_opportunities_during_preflight": None,
        "activation_preflight_runs": 0,
        "per_entry_preflight_runs": 0,
        "interpretation": "No eligible data: coverage/basis blocked. "
        "Cannot assess market or threshold. "
        "Observed evaluations are deduplicated captured inputs, not a total engine-call counter. "
        "No opportunity lost during preflight can be assessed because preflight never ran.",
        "capture_start_utc": start["collector_started_utc"],
        "capture_finish_utc": finish["finished_at_utc"],
        "status_samples": finish["samples"],
        "local_status_errors": finish["local_status_errors"],
        "technical_ready_s": 0,
        "longest_technical_ready_s": longest,
        "blocker_s": dict(seconds),
        "overlap_s": [{"blockers": list(k), "seconds": v} for k, v in overlap.items()],
        "max_valid_sample_gap_s": max_sample_gap,
        "market_validity_transitions": market_intervals,
        "failed_fx_requests": list(failures.values()),
        "max_fx_conservative_age_s": fx_max_age,
        "max_quote_age_ms": quotes_invalid_ms,
        "max_component_age_s": component_max,
        "peak_lighter_used_including_leases": peak_used,
        "minimum_capacity_to_55": minimum_exit_capacity,
        "commands": commands,
        "final_state": final["state"],
        "order_permission": final["allow_live_orders"],
        "entries_stopped": final["entries_stopped"],
        "final_account": final["account"],
        "cash_audit": final["net_cash_audit"],
        "fee_reports": final["fee_reports"],
        "history_complete": final["history_audit"]["complete"],
        "reconnects": final["reconnects"],
        "raw_snapshot_sha256": hashlib.sha256(
            (directory / "snapshots.jsonl.gz").read_bytes()
        ).hexdigest(),
        "initial_capture_unobserved_preparation_s": datetime.fromisoformat(
            start["collector_started_utc"]
        ).timestamp()
        - start["preparation_started_epoch"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--reconnects",
        action="store_true",
        help="Reconstruct public transport instead of strategy results",
    )
    mode.add_argument(
        "--comparison", action="store_true", help="Summarize the single Rust read-only comparison"
    )
    mode.add_argument("--depth-ab", action="store_true", help="Summarize one pinned-IP Rust A/B")
    parser.add_argument(
        "--raw", action="store_true", help="Add raw existing Book replay to --reconnects"
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Omit repetitive local socket snapshots from comparison export",
    )
    args = parser.parse_args()
    function = (
        analyze_depth_ab
        if args.depth_ab
        else analyze_comparison
        if args.comparison
        else reconnect_timeline
        if args.reconnects
        else analyze
    )
    result = function(args.directory, Path(__file__).resolve().parents[1])
    if args.raw:
        if not args.reconnects:
            parser.error("--raw requires --reconnects")
        add_raw_reconnect_evidence(result, Path(__file__).resolve().parents[1])
    if args.compact:
        result.pop("app_socket_observations", None)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("Saved bounded preparation summary: " + str(args.output))
