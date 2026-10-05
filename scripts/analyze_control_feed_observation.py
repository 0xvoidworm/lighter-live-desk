"""Offline timing analysis of one passive recording; no network/order imports."""

import argparse
import gzip
import json
from collections import Counter, defaultdict
from decimal import Decimal
from itertools import chain
from pathlib import Path

from analyze_split_audit import describe


def analyze(source: Path, market: Path):
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
                    if receipt < manifest["start_epoch"]:
                        continue
                    key = record["venue"] + ":" + record["kind"]
                    counts[key] += 1
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
    off = json.loads((source / "permission-off-load.json").read_text())
    before, after = off["before"], off["after"]
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
        "off_reload_runtime": {
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
        },
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
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.source, args.market), indent=2) + "\n")
    print(args.output)
