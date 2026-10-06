"""Offline timing analysis of one passive recording; no network/order imports."""

import argparse
import gzip
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
from itertools import chain
from pathlib import Path

from analyze_split_audit import describe


def frozen_records(directory: Path):
    """Read archived gzip prefixes, retaining an explicit incomplete-tail limitation."""
    for path in sorted(directory.glob("market-*.jsonl.gz")):
        with gzip.open(path, "rt") as stream:
            try:
                for line in stream:
                    yield json.loads(line)
            except EOFError:
                # Runtime copies of an open member have no trailer. Never synthesize rows.
                pass


def clock_replay(rows: list[dict], clock_records: list[dict]) -> dict:
    """Replay the production guard with only proof records available before evaluation.

    Wall time is reconstructed within the recorded evaluation bracket, not measured
    at the original source check. The historical first data_valid check is unrecorded.
    """
    from lldesk.models import Stamp
    from lldesk.source_time import BinanceSourceClock, ClockEvidence

    records = sorted(clock_records, key=lambda r: r["recorded_monotonic_ns"])
    available = [r["recorded_monotonic_ns"] for r in records]
    outcomes, lower, drift, differences = Counter(), [], [], []
    timing_ms, mismatches = [], []
    replay_match = proof_unavailable = 0
    basis_context = Counter()
    post_basis_seconds, ready_endings = Counter(), Counter()
    for i, row in enumerate(rows):
        status = row["status"]
        inputs = status["decision_inputs"]
        original = inputs["source_freshness"]
        now = original["evaluated_monotonic_ns"]
        index = bisect_right(available, now) - 1
        if index < 0:
            proof_unavailable += 1
            continue
        proof = records[index]["payload"]["clock"]
        clock = BinanceSourceClock(1500)
        if proof is not None:
            clock.evidence = ClockEvidence(
                Stamp(receipt_ns=proof["received_ns"], monotonic_ns=proof["received_monotonic_ns"]),
                proof["offset_low_ms"],
                proof["offset_high_ms"],
                proof["server_ms"],
                proof["rtt_ms"],
                proof["host"],
            )
        q = inputs["quotes"]["binance"]
        stamp = Stamp(
            receipt_ns=q["receipt_ns"],
            monotonic_ns=q["monotonic_ns"],
            exchange_ms=q["exchange_ms"],
            exchange_event_ms=q["exchange_event_ms"],
        )
        wall = round(inputs["evaluated_at"] * 1e9) + now - inputs["monotonic_ns"]
        result = clock.assess(stamp, now_ns=now, wall_ns=wall)
        replay_match += result["reason"] == original["reason"]
        outcomes[result["reason"]] += 1
        lower.append(result.get("source_age_low_ms"))
        drift.append(result.get("clock_drift_allowance_ms"))
        timing_ms.append((now - inputs["monotonic_ns"]) / 1e6)
        diagnostic = status["diagnostic_observation"]["source_freshness"]
        if inputs["data_valid"] is False and diagnostic["passed"]:
            differences.append(
                {
                    "elapsed_s": row["elapsed_s"],
                    "decision_source_reason": result["reason"],
                    "diagnostic_delay_ms": (diagnostic["evaluated_monotonic_ns"] - now) / 1e6,
                }
            )
        if not status["diagnostic_observation"]["checks"]["basis_scale"]:
            basis_context["invalid_input" if not inputs["data_valid"] else "valid_input"] += 1
            if status.get("coverage", {}).get("warmup_complete"):
                end = min(1200, rows[i + 1]["elapsed_s"] if i + 1 < len(rows) else 1200)
                key = f"decision:{result['reason']};diagnostic:{diagnostic['reason']}"
                post_basis_seconds[key] += max(0, end - row["elapsed_s"])
        if status.get("coverage", {}).get("warmup_complete") and i:
            observation = status["diagnostic_observation"]
            if (
                rows[i - 1]["status"]["diagnostic_observation"]["technical_ready"]
                and not observation["technical_ready"]
            ):
                ready_endings["+".join(observation["blockers"])] += 1
        if inputs["data_valid"] is False and original["passed"]:
            mismatches.append(
                {
                    "elapsed_s": row["elapsed_s"],
                    "decision_monotonic_ns": inputs["monotonic_ns"],
                    "second_assessment": result,
                    "quotes": inputs["quotes"],
                    "fx_age_s": inputs["fx_age_s"],
                    "queue_backlog": inputs["queue_backlog"],
                    "diagnostic_delay_ms": (diagnostic["evaluated_monotonic_ns"] - now) / 1e6,
                    "lower_bound_margin_to_future_boundary_us": (result["source_age_low_ms"] + 1)
                    * 1000,
                    "first_assessment": "not recorded; boundary crossing is plausible, not proven",
                }
            )
    return {
        "samples": len(rows),
        "matching_recorded_second_assessment_reasons": replay_match,
        "proof_unavailable": proof_unavailable,
        "second_assessment_reasons": dict(outcomes),
        "source_lower_ms": describe(lower),
        "drift_allowance_ms": describe(drift),
        "second_assessment_after_decision_stamp_ms": describe(timing_ms),
        "basis_scale_sample_context": dict(basis_context),
        "post_warmup_basis_context_seconds": dict(post_basis_seconds),
        "sampled_post_warmup_ready_endings": dict(ready_endings),
        "decision_invalid_diagnostic_source_passed": len(differences),
        "diagnostic_delay_for_invalid_decision_ms": describe(
            [d["diagnostic_delay_ms"] for d in differences]
        ),
        "two_ambiguous_decisions": mismatches,
        "limits": "First guard time/result absent. Replay checks recorded SECOND assessment "
        "using already available proof and bracketed wall time; no retroactive approval.",
    }


def reconnect_evidence(directory: Path, begin: float, end: float) -> tuple[dict, list[dict]]:
    """Existing runtime receipts + deterministic Book replay, not a new observation."""
    from lldesk.market import Book, GapError
    from lldesk.models import Stamp

    book = Book("binance")
    last, events, clocks = {}, [], []
    counters, gaps, queue_ms = Counter(), [], []
    wall_steps, prior_stamp = [], None
    sequence_errors = 0
    for r in frozen_records(directory):
        at = r["recorded_ns"] / 1e9
        if at > end:
            continue
        brief = {
            k: r[k]
            for k in (
                "kind",
                "receipt_ns",
                "monotonic_ns",
                "recorded_ns",
                "recorded_monotonic_ns",
                "exchange_ms",
            )
        }
        brief["update_id"] = r["payload"].get("u", r["payload"].get("lastUpdateId"))
        if r["kind"] == "source_clock":
            clocks.append(r)
        if r["kind"] == "disconnect":
            p = r["payload"]
            channel = "lighter" if r["venue"] == "lighter" else "binance_" + p["channel"]
            if channel == "binance_public":
                book.invalidate()
            if begin <= at <= end:
                events.append(
                    {
                        "channel": channel,
                        "disconnect": brief,
                        "payload": p,
                        "last_messages": dict(last),
                    }
                )
        stamp = Stamp(
            receipt_ns=r["receipt_ns"],
            monotonic_ns=r["monotonic_ns"],
            exchange_ms=r.get("exchange_ms"),
        )
        try:
            if r["kind"] == "snapshot" and r["venue"] == "binance":
                book.binance_snapshot(r["payload"], stamp)
            elif r["kind"] == "depthUpdate":
                book.binance_delta(r["payload"], stamp)
        except GapError:
            sequence_errors += int(begin <= at <= end)
        key = {
            ("binance", "depthUpdate"): "binance_public",
            ("binance", "aggTrade"): "binance_market",
            ("lighter", "order_book"): "lighter",
            ("binance_spot", "fx"): "fx",
        }.get((r["venue"], r["kind"]))
        if r["kind"] == "fx":
            key = "fx"
        if begin <= at <= end:
            counters[r["venue"] + ":" + r["kind"]] += 1
            delta_ms = (r["recorded_monotonic_ns"] - r["monotonic_ns"]) / 1e6
            if key:
                queue_ms.append(delta_ms)
                if key in last:
                    gap = (r["monotonic_ns"] - last[key]["monotonic_ns"]) / 1e9
                    if gap > 5:
                        gaps.append(
                            {"channel": key, "gap_s": gap, "before": last[key], "after": brief}
                        )
            # Recorder wall/monotonic pair, unaffected by buffered receipt order.
            current = (r["recorded_ns"], r["recorded_monotonic_ns"])
            if prior_stamp:
                discrepancy_ms = (
                    (current[0] - prior_stamp[0]) - (current[1] - prior_stamp[1])
                ) / 1e6
                if abs(discrepancy_ms) > 25:
                    wall_steps.append({"at": at, "wall_minus_monotonic_change_ms": discrepancy_ms})
            prior_stamp = current
        for event in events:
            if at < event["disconnect"]["recorded_ns"] / 1e9:
                continue
            if key == event["channel"]:
                event.setdefault("first_following_message", brief)
            if event["channel"] == "binance_public":
                if r["kind"] == "snapshot" and r["venue"] == "binance":
                    event.setdefault("following_snapshot", brief)
                if r["kind"] == "depthUpdate" and book.valid and book.bridged:
                    event.setdefault("following_valid_depth", brief)
        if key:
            last[key] = brief
    for event in events:
        prev = event["last_messages"].get(event["channel"])
        after = event.get("first_following_message")
        if prev and after:
            event["receipt_gap_s"] = (after["monotonic_ns"] - prev["monotonic_ns"]) / 1e9
            event["last_receipt_to_disconnect_s"] = (
                event["disconnect"]["monotonic_ns"] - prev["monotonic_ns"]
            ) / 1e9
            event["disconnect_to_receipt_s"] = (
                after["monotonic_ns"] - event["disconnect"]["monotonic_ns"]
            ) / 1e9
    return {
        "begin_epoch": begin,
        "end_epoch": end,
        "reconnects": events,
        "raw_event_counts": dict(counters),
        "receipt_gaps_over_5s": gaps,
        "receipt_to_record_ms": describe(queue_ms),
        "recorder_wall_step_over_25ms": wall_steps,
        "offline_sequence_errors": sequence_errors,
        "limits": "No continuous post-window status, ping/pong wire stamps, connection IDs, "
        "handshake phases, event-loop scheduling delay or complete recovery coverage timeline. "
        "Gzip prefixes may lack trailers. Raw stamps precede book application.",
    }, clocks


def clock_reconnect_analysis(source: Path, market: Path, end: float, journal: Path) -> dict:
    manifest = json.loads((source / "started.json").read_text())
    with gzip.open(source / "snapshots.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    post, clocks = reconnect_evidence(market, manifest["deadline_epoch"], end)
    warnings = [
        {"at_epoch": int(r["__REALTIME_TIMESTAMP"]) / 1e6, "message": r["MESSAGE"]}
        for line in journal.read_text().splitlines()
        if "reconciliation blocked" in (r := json.loads(line)).get("MESSAGE", "")
        and int(r["__REALTIME_TIMESTAMP"]) / 1e6 <= end
    ]
    return {
        "historical_window": {
            k: manifest[k] for k in ("start_epoch", "deadline_epoch", "duration_seconds")
        },
        "clock_replay": clock_replay(rows, clocks),
        "post_window": post,
        "account_timeout_warnings": warnings,
        "clock_attempts": [r["payload"] for r in clocks],
        "no_runtime_recording": True,
    }


def persistent_diagnostics(database: Path, begin: float, end: float) -> dict:
    """Export existing read-only audit rows; omit task IDs and unknown request fields."""
    import sqlite3

    fields = {
        "id",
        "host",
        "path",
        "started_at",
        "finished_at",
        "elapsed_ms",
        "phase_ms",
        "reason",
        "quota_priority",
        "local_quota_weight",
        "http_attempts",
        "outcome",
        "last_phase",
        "error",
        "status",
        "pagination",
    }
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT at,kind,body FROM events WHERE at BETWEEN ? AND ? AND kind IN "
            "('ACCOUNT_RECONCILE_DIAGNOSTIC','ACCOUNT_STATE_DIAGNOSTIC','BOOK_GAP') ORDER BY at",
            (begin, end),
        ).fetchall()
    records = []
    for at, kind, body in rows:
        value = json.loads(body)
        if kind == "BOOK_GAP":
            value = {"sequence_gap_recorded": True}
        else:
            value = {
                k: v
                for k, v in value.items()
                if k
                in {
                    "finished_at",
                    "elapsed_ms",
                    "lock_wait_ms",
                    "request_deadline_s",
                    "outcome",
                    "error",
                    "requests",
                    "request_used_60s",
                }
            }
            value["requests"] = [
                {k: v for k, v in r.items() if k in fields} for r in value.get("requests", [])
            ]
        records.append({"at_epoch": at, "kind": kind, "body": value})
    errors = [r for r in records if r["body"].get("outcome") == "error"]
    requests = {q["id"]: q for r in records for q in r["body"].get("requests", [])}
    return {
        "begin_epoch": begin,
        "end_epoch": end,
        "records": records,
        "outcomes": dict(
            Counter(r["kind"] + ":" + r["body"].get("outcome", "event") for r in records)
        ),
        "request_failure_phases": dict(
            Counter(
                q.get("last_phase", "unknown")
                for q in requests.values()
                if q.get("outcome") == "error"
            )
        ),
        "failed_reconcile_lock_wait_ms": describe([r["body"].get("lock_wait_ms") for r in errors]),
        "failed_reconcile_quota_used": describe(
            [r["body"].get("request_used_60s") for r in errors]
        ),
        "no_exchange_request": True,
    }


def production_metrics(rows: list[dict], manifest: dict) -> dict:
    """Additional passive source/quota evidence; never treats cross-clock age as latency."""
    duration = manifest["duration_seconds"]
    source_reasons, measures, budgets = Counter(), defaultdict(list), defaultdict(list)
    basis_blocker_context = Counter()
    post_warm_basis_context = Counter()
    requests, handshakes, audits = {}, {}, {}
    inconsistent = 0
    history_complete_s = exit_capacity_s = 0.0
    unknown_s = max(0.0, rows[0]["elapsed_s"]) if rows else duration
    for i, row in enumerate(rows):
        end = min(duration, rows[i + 1]["elapsed_s"] if i + 1 < len(rows) else duration)
        dt = max(0.0, end - row["elapsed_s"])
        status = row.get("status")
        if not status or not status.get("connected") or dt > 2:
            unknown_s += dt
            continue
        observation = status["diagnostic_observation"]
        assessment = observation["source_freshness"]
        source_reasons[assessment["reason"]] += dt
        if not observation["checks"]["basis_scale"]:
            inputs = status.get("decision_inputs", {})
            context = (
                "preceding_evaluation_data_valid_false"
                if inputs.get("data_valid") is False
                else "preceding_evaluation_valid_or_unknown"
            )
            basis_blocker_context[context] += dt
            if status.get("coverage", {}).get("warmup_complete"):
                post_warm_basis_context[context] += dt
        inconsistent += observation["checks"]["source_freshness"] != assessment["passed"] or (
            not assessment["passed"] and observation["checks"]["market_data"]
        )
        for key in (
            "receipt_age_ms",
            "source_indicator_ms",
            "source_age_low_ms",
            "source_age_high_ms",
            "clock_uncertainty_ms",
            "clock_evidence_age_s",
        ):
            measures[key].append(assessment.get(key))
        for key, value in status.get("request_budget", {}).items():
            budgets[key].append(value)
        budget = status.get("request_budget", {})
        if "used_last_60s" in budget and "reserved_unconsumed" in budget:
            budgets["used_plus_reserved"].append(
                budget["used_last_60s"] + budget["reserved_unconsumed"]
            )
        if status.get("request_budget", {}).get("transaction_slots_remaining", 0) >= 15:
            exit_capacity_s += dt
        if status.get("history_audit", {}).get("complete"):
            history_complete_s += dt
        for name, trace in (
            ("account", status.get("transport_trace", {})),
            ("binance", status.get("binance_transport_trace", {})),
        ):
            for request in trace.get("active", []) + trace.get("recent", []):
                # Handshakes/bootstrap before capture still prove the new process route.
                if name == "binance":
                    handshakes[request["id"]] = request
                if manifest["start_epoch"] <= request["started_at"] < manifest["deadline_epoch"]:
                    if name == "account":
                        requests[request["id"]] = request
        for key in ("account_state_diagnostic", "reconcile_diagnostic"):
            audit = status.get(key) or {}
            at = audit.get("finished_at")
            if at is not None and manifest["start_epoch"] <= at < manifest["deadline_epoch"]:
                audits[key, at] = audit
    endpoints = {}
    for path in sorted({r["path"] for r in requests.values()}):
        values = [r for r in requests.values() if r["path"] == path]
        endpoints[path] = {
            "request_count": len(values),
            "quota_weight": sum(r.get("local_quota_weight", 0) for r in values),
            "http_attempts": sum(r.get("http_attempts", 0) for r in values),
            "reasons": dict(Counter(r.get("reason") for r in values)),
            "outcomes": dict(Counter(r.get("outcome", "in_progress") for r in values)),
            "elapsed_ms": describe([r.get("elapsed_ms") for r in values]),
        }
    return {
        "source_reason_seconds": dict(source_reasons),
        "basis_scale_blocker_context_seconds": dict(basis_blocker_context),
        "post_warmup_basis_scale_blocker_context_seconds": dict(post_warm_basis_context),
        "source_assessment_statistics": {k: describe(v) for k, v in measures.items()},
        "source_consistency_violations": inconsistent,
        "request_budget_statistics": {
            k: describe(v) | {"min": min(v), "mean": sum(v) / len(v)}
            for k, v in budgets.items()
            if v
        },
        "history_complete_seconds": history_complete_s,
        "exit_capacity_at_least_15_seconds": exit_capacity_s,
        "usable_duration_seconds": duration - unknown_s,
        "lighter_read_endpoint_summary": endpoints,
        "lighter_read_request_average_per_minute": len(requests) / (duration / 60),
        "binance_http_trace": sorted(handshakes.values(), key=lambda r: r["started_at"]),
        "audit_outcomes": {
            key: dict(Counter(a.get("outcome") for (k, _), a in audits.items() if k == key))
            for key in sorted({k for k, _ in audits})
        },
        "measurement_limit": "source age includes clock bounds; not network or fill latency",
    }


def analyze(source: Path, market: Path, *, permission_reload_required: bool = True):
    manifest = json.loads((source / "started.json").read_text())
    finish = json.loads((source / "finished.json").read_text())
    duration = manifest["duration_seconds"]
    assert duration == 1200 and finish["analysis_window_seconds"] == duration
    assert finish["configuration_unchanged"] and finish["source_hashes_unchanged"]
    with gzip.open(source / "snapshots.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    seconds, overlaps = Counter(), Counter()
    post_warm_seconds, post_warm_overlaps = Counter(), Counter()
    ages, channels = defaultdict(list), {}
    warm_seconds = ready_seconds = longest = current = 0.0
    unknown = max(0.0, min(duration, rows[0]["elapsed_s"]))
    first_warm = None
    transitions, previous = [], None
    fx_requests = {}
    mismatch_count = 0
    eligible_mismatch_count = actual_input_difference_count = 0
    for i, row in enumerate(rows):
        start = max(0.0, row["elapsed_s"])
        end = min(duration, rows[i + 1]["elapsed_s"] if i + 1 < len(rows) else duration)
        dt = max(0.0, end - start)
        status = row.get("status")
        if not status or not status.get("connected") or dt > 2:
            unknown += dt
            current = 0.0
            continue
        assert not status["allow_live_orders"]
        assert status["strategy_session"]["status"] == "closed"
        assert not status["strategy_session"]["attempts"] and not status["pending_orders"]
        if status.get("account"):
            assert not Decimal(status["account"]["position_qty"])
            assert not status["account"]["exchange_active_order_count"]
            assert not status["account"]["other_exchange_position_count"]
        observation = status["diagnostic_observation"]
        blockers = tuple(sorted(observation["blockers"]))
        overlaps[blockers] += dt
        for blocker in blockers:
            seconds[blocker] += dt
        if blockers != previous:
            transitions.append({"elapsed_s": start, "blockers": list(blockers)})
            previous = blockers
        if not status["coverage"]["warmup_complete"]:
            warm_seconds += dt
        else:
            if first_warm is None:
                first_warm = start
            post_warm_overlaps[blockers] += dt
            for blocker in blockers:
                post_warm_seconds[blocker] += dt
        if observation["technical_ready"]:
            ready_seconds += dt
            current += dt
            longest = max(longest, current)
        else:
            current = 0.0
        mismatch_count += observation.get("decision_input_match") is False
        eligible_mismatch_count += (
            observation.get("decision_input_match") is False and observation["technical_ready"]
        )
        actual_input_difference_count += any(
            (status["decision_inputs"]["quotes"].get(name) or {}).get("monotonic_ns")
            != (observation["quotes"].get(name) or {}).get("monotonic_ns")
            for name in ("binance", "lighter")
        )
        for name, quote in status["quotes"].items():
            if quote:
                ages[name + "_book_ms"].append(quote["age_ms"])
        if observation.get("fx_age_s") is not None:
            ages["fx_conservative_s"].append(observation["fx_age_s"])
        for name, age in (status.get("account") or {}).get("component_ages_s", {}).items():
            ages["account_" + name + "_s"].append(age)
        ages["event_loop_reported_lag_ms"].append(status.get("event_loop_lag_ms", 0))
        ages["queue_backlog"].append(status.get("queue_backlog", 0))
        for record in status["fx_diagnostic"]["recent"]:
            if manifest["start_epoch"] <= record["started_at"] < manifest["deadline_epoch"]:
                fx_requests[record["id"]] = record
        channels = status["feed_channels"]

    counts, receipt_gaps, delays = Counter(), defaultdict(list), defaultdict(list)
    previous_receipt, long_gaps = {}, []
    bootstrap_evidence = []
    clock_attempts = []
    end_seen = False
    files = []
    for path in sorted(market.glob("market-*.jsonl.gz")):
        try:
            with gzip.open(path, "rt") as stream:
                first = json.loads(next(stream))
                if first.get("pid") != manifest["expected_engine_pid"]:
                    continue
                files.append(path.name)
                for record in chain((first,), (json.loads(line) for line in stream)):
                    receipt = record["receipt_ns"] / 1e9
                    recorded = record["recorded_ns"] / 1e9
                    if receipt >= manifest["deadline_epoch"]:
                        end_seen = True
                        break
                    if record["venue"] == "binance" and record["kind"] == "snapshot":
                        bootstrap_evidence.append(
                            {
                                "receipt_epoch": receipt,
                                "sequence": record["payload"]["lastUpdateId"],
                                "T": record.get("exchange_ms"),
                                "E": record.get("exchange_event_ms"),
                                "bid_levels": len(record["payload"]["bids"]),
                                "ask_levels": len(record["payload"]["asks"]),
                                "before_capture": receipt < manifest["start_epoch"],
                            }
                        )
                    if receipt < manifest["start_epoch"]:
                        continue
                    key = record["venue"] + ":" + record["kind"]
                    counts[key] += 1
                    if key == "binance:source_clock":
                        clock_attempts.append(record["payload"]["attempt"])
                    if key in previous_receipt:
                        gap = (record["monotonic_ns"] - previous_receipt[key][1]) / 1e9
                        receipt_gaps[key].append(gap)
                        if gap > 5 and key in {
                            "binance:depthUpdate",
                            "lighter:order_book",
                            "binance:fx",
                        }:
                            long_gaps.append(
                                {
                                    "source": key,
                                    "start_epoch": previous_receipt[key][0],
                                    "end_epoch": receipt,
                                    "gap_s": gap,
                                }
                            )
                    previous_receipt[key] = (receipt, record["monotonic_ns"])
                    processing = record["recorded_monotonic_ns"]
                    delays[key + ":receipt_to_recorder_ms"].append(
                        (processing - record["monotonic_ns"]) / 1e6
                    )
                    queued = record.get("queued_monotonic_ns")
                    if queued is not None:
                        delays[key + ":receive_to_enqueue_ms"].append(
                            (queued - record["monotonic_ns"]) / 1e6
                        )
                        delays[key + ":queue_to_recorder_ms"].append((processing - queued) / 1e6)
                    assert recorded >= receipt
        except EOFError:
            # Engine keeps writing gzip; an observed post-window record proves
            # the requested interval was read without needing to stop the engine.
            if not end_seen:
                raise
    assert end_seen, "no persisted post-window record; recording completeness not proven"
    reload_evidence = None
    if permission_reload_required:
        off = json.loads((source / "permission-off-load.json").read_text())
        before, after = off["before"], off["after"]
        reload_evidence = {
            "command": off["command"],
            "command_id": off["command_id"],
            "result": off["result"],
            "same_pid": before["engine_pid"] == after["engine_pid"],
            "session_record_unchanged": before["strategy_session"] == after["strategy_session"],
            "buffer_epoch_unchanged": before["coverage"]["buffer_started_at"]
            == after["coverage"]["buffer_started_at"],
            "samples_before": before["coverage"]["total_samples"],
            "samples_after": after["coverage"]["total_samples"],
            "config_hash_unchanged": off["config_sha256_before"] == off["config_sha256_after"],
            "positive_authorization_or_activation": False,
        }
    last = next(r["status"] for r in reversed(rows) if r.get("status"))
    return {
        "code_commit": manifest["code_commit"],
        "started_at_utc": manifest["started_at_utc"],
        "capture_finished_at_utc": finish["ended_at_utc"],
        "duration_seconds": duration,
        "snapshot_count": len(rows),
        "capture_unknown_seconds": unknown,
        "warmup_seconds_within_capture": warm_seconds,
        "first_warmup_complete_elapsed_s": first_warm,
        "post_warmup_seconds": duration - (first_warm if first_warm is not None else duration),
        "technical_ready_seconds": ready_seconds,
        "technical_ready_longest_interval_s": longest,
        "blocker_seconds_overlapping": dict(seconds),
        "post_warmup_blocker_seconds_overlapping": dict(post_warm_seconds),
        "post_warmup_blocker_overlap_seconds": [
            {"blockers": list(k), "seconds": v} for k, v in post_warm_overlaps.items()
        ],
        "blocker_overlap_seconds": [
            {"blockers": list(k), "seconds": v} for k, v in overlaps.items()
        ],
        "readiness_transitions": transitions,
        "age_and_local_processing_status": {k: describe(v) for k, v in ages.items()},
        "decision_input_mismatch_samples": mismatch_count,
        "decision_match_false_while_technically_eligible": eligible_mismatch_count,
        "actual_evaluator_quote_timestamp_difference_samples": actual_input_difference_count,
        "decision_match_flag_limit": (
            "flag requires both books to exist; two absent snapshots also return false. "
            "Compare recorded monotonic timestamps separately before inferring stale evaluation."
        ),
        "recorded_market_files": files,
        "bootstrap_snapshots": bootstrap_evidence,
        "clock_probe_summary": {
            "count": len(clock_attempts),
            "conservative_futures_weight": sum(a["weight"] for a in clock_attempts),
            "lighter_weight": 0,
            "outcomes": dict(Counter(a["outcome"] for a in clock_attempts)),
            "duration_ms_known_only": describe([a.get("duration_ms") for a in clock_attempts]),
            "unverified_or_failed": [a for a in clock_attempts if a["outcome"] != "verified"],
        },
        "final_quote_sequence_gaps": {k: v["gaps"] for k, v in last["quotes"].items()},
        "persisted_end_of_window_proven": end_seen,
        "market_event_counts": dict(counts),
        "application_receipt_gap_s": {k: describe(v) for k, v in receipt_gaps.items()},
        "long_book_or_fx_receipt_gaps": long_gaps,
        "receipt_parse_queue_to_recorder_ms": {k: describe(v) for k, v in delays.items()},
        "processing_measurement_limit": (
            "receipt follows application read before parsing; recorder precedes book application; "
            "exact apply timings also in engine market_processing; pre-read network/socket "
            "buffering remains unobserved"
        ),
        "final_market_processing": last["market_processing"],
        "feed_channels_final": channels,
        "fx_request_count": len(fx_requests),
        "fx_request_outcomes": dict(
            Counter(r.get("outcome", "in_progress") for r in fx_requests.values())
        ),
        "fx_request_duration_ms": describe([r.get("elapsed_ms") for r in fx_requests.values()]),
        "fx_failed_requests": [
            r for r in fx_requests.values() if r.get("outcome") not in {None, "verified"}
        ],
        "off_reload_runtime": reload_evidence,
        "production_metrics": production_metrics(rows, manifest)
        if not permission_reload_required
        else None,
        "final_snapshot_account": last.get("account"),
        "final_snapshot_state": last["state"],
        "final_snapshot_order_permission": last["allow_live_orders"],
        "final_snapshot_session_status": last["strategy_session"]["status"],
        "final_snapshot_history_audit": last["history_audit"],
        "no_order_verification": (
            "closed session, order OFF and no attempts/pending orders asserted in every usable "
            "local-status sample; final fresh exchange account read is documented separately"
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--market", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--without-permission-reload", action="store_true")
    parser.add_argument("--clock-reconnect-cutoff", type=float)
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args()
    if args.clock_reconnect_cutoff is not None and args.journal is None:
        parser.error("--clock-reconnect-cutoff requires --journal")
    args.output.write_text(
        json.dumps(
            clock_reconnect_analysis(
                args.source, args.market, args.clock_reconnect_cutoff, args.journal
            )
            if args.clock_reconnect_cutoff is not None
            else analyze(
                args.source,
                args.market,
                permission_reload_required=not args.without_permission_reload,
            ),
            indent=2,
        )
        + "\n"
    )
    print(args.output)
