"""One frozen no-order FX window, using existing engine snapshots only."""

import argparse
import gzip
import json
from collections import Counter, deque
from itertools import chain, pairwise
from pathlib import Path

from analyze_split_audit import analyze, describe, market_evidence


def fx_metrics(source: Path, manifest: dict):
    recent, received, ages, conservative = {}, {}, [], []
    blocked = longest = current = 0.0
    initial = final = None
    for stream in [gzip.open(source / "snapshots.jsonl.gz", "rt")]:
        with stream:
            previous = None
            for line in stream:
                row = json.loads(line)
                status = row.get("status")
                if previous:
                    dt = row["elapsed_s"] - previous["elapsed_s"]
                    before = previous.get("status")
                    if before:
                        if not before["fx_diagnostic"]["usable"] or before["fx_usdcusdt"] is None:
                            blocked += dt
                            current += dt
                            longest = max(longest, current)
                        else:
                            current = 0
                previous = row
                if not status:
                    continue
                fx = status["fx_diagnostic"]
                initial = initial or fx
                final = fx
                observation = fx.get("observation")
                if observation:
                    ages.append(observation["observation_age_s"])
                    conservative.append(observation["conservative_age_s"])
                    received[observation["received_ns"]] = observation
                for record in fx["recent"]:
                    recent[record["id"]] = record
                if fx["active"]:
                    recent.setdefault(fx["active"]["id"], fx["active"])
            if previous and previous.get("status"):
                dt = manifest["duration_seconds"] - previous["elapsed_s"]
                before = previous["status"]
                if not before["fx_diagnostic"]["usable"] or before["fx_usdcusdt"] is None:
                    blocked += dt
                    longest = max(longest, current + dt)
    start, end = manifest["start_epoch"], manifest["deadline_epoch"]
    requests = [r for r in recent.values() if start <= r["started_at"] < end]
    observations = sorted(received.values(), key=lambda o: o["received_ns"])
    in_window = [o for o in observations if start <= o["received_ns"] / 1e9 < end]
    gaps = [
        (b["received_ns"] - a["received_ns"]) / 1e9
        for a, b in pairwise(observations)
        if start <= b["received_ns"] / 1e9 < end
    ]
    rolling, peak_count, peak_weight = deque(), 0, 0
    for record in sorted(recent.values(), key=lambda r: r["started_at"]):
        while rolling and record["started_at"] - rolling[0]["started_at"] >= 60:
            rolling.popleft()
        rolling.append(record)
        if start <= record["started_at"] < end:
            peak_count = max(peak_count, len(rolling))
            peak_weight = max(peak_weight, sum(r["weight"] for r in rolling))
    same_price = sum((a["bid"], a["ask"]) == (b["bid"], b["ask"]) for a, b in pairwise(in_window))
    same_price_size_changed = sum(
        (a["bid"], a["ask"]) == (b["bid"], b["ask"])
        and (a["bid_qty"], a["ask_qty"]) != (b["bid_qty"], b["ask_qty"])
        for a, b in pairwise(in_window)
    )
    return {
        "source": final["source"],
        "pair_verified": final["pair_verified"],
        "provider_limits": final["quota"]["limits"],
        "additional_statistics_polling": 0,
        "requests_in_window": len(requests),
        "request_weight_in_window": sum(r["weight"] for r in requests),
        "requests_per_minute": len(requests) * 60 / manifest["duration_seconds"],
        "request_rolling_60s_peak": peak_count,
        "weight_rolling_60s_peak": peak_weight,
        "outcomes": dict(Counter(r.get("outcome", "in_flight") for r in requests)),
        "elapsed_ms": describe([r.get("elapsed_ms") for r in requests]),
        "failure_phases": dict(
            Counter(r.get("failure_phase") for r in requests if r.get("outcome") == "failed")
        ),
        "errors": [r for r in requests if r.get("outcome") == "failed"],
        "observations_in_window": len(in_window),
        "same_price_consecutive_observations": same_price,
        "same_price_size_changed_observations": same_price_size_changed,
        "observation_age_s": describe(ages),
        "conservative_request_start_age_s": describe(conservative),
        "receipt_gaps_s": describe(gaps),
        "verified_fx_block_s": blocked,
        "verified_fx_block_longest_s": longest,
        "counter_deltas": {
            k: final["counts"].get(k, 0) - initial["counts"].get(k, 0)
            for k in set(initial["counts"]) | set(final["counts"])
        },
        "provider_ip_weight_1m_final": final["quota"]["provider_ip_weight_1m"],
        "shared_ip_limit": (
            "header is provider aggregate; local counter observes this FX worker only"
        ),
        "requests": requests,
    }


def confirm_tail(directory: Path, manifest: dict, requests: list[dict]):
    # Local status can stop just before a completed response is next published.
    # Match already-written, verified FX events; never infer success from send/start.
    pending = {r["id"]: r for r in requests if r.get("outcome") is None}
    proofs = []
    for path in sorted(directory.glob("market-*.jsonl.gz")):
        with gzip.open(path, "rt") as stream:
            first = json.loads(next(stream))
            if first.get("pid") != manifest["expected_engine_pid"]:
                continue
            if first["receipt_ns"] / 1e9 >= manifest["deadline_epoch"]:
                continue
            for event in chain((first,), (json.loads(line) for line in stream)):
                if event["receipt_ns"] / 1e9 >= manifest["deadline_epoch"]:
                    break
                if event["kind"] != "fx":
                    continue
                started = event["payload"]["request_started_ns"] / 1e9
                for request_id, request in list(pending.items()):
                    if abs(started - request["started_at"]) < 0.000001:
                        proofs.append(
                            {
                                "request_id": request_id,
                                "request_started_at": request["started_at"],
                                "received_ns": event["receipt_ns"],
                                "verified_monotonic_ns": event["payload"]["verified_monotonic_ns"],
                                "recorded_ns": event["recorded_ns"],
                                "source": event["payload"]["source"],
                                "file": path.name,
                                "within_original_window": True,
                                "evidence": (
                                    "verified observation, exact HTTP status absent in snapshot"
                                ),
                            }
                        )
                        del pending[request_id]
    return proofs


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--market-directory", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    result = analyze(args.source)
    result["fx"] = fx_metrics(args.source, result["manifest"])
    if args.market_directory:
        result["market_evidence"] = market_evidence(args.market_directory, result["manifest"])
        result["fx"]["tail_verified_observation_proofs"] = confirm_tail(
            args.market_directory, result["manifest"], result["fx"]["requests"]
        )
        result["fx"]["verified_observations_proved"] = result["fx"]["outcomes"].get(
            "verified", 0
        ) + len(result["fx"]["tail_verified_observation_proofs"])
    if args.baseline:
        before = json.loads(args.baseline.read_text())
        result["before"] = {
            "source_file": str(args.baseline),
            "code_commit": before["manifest"]["code_commit"],
            "technical_ready_s": before["durations_s"].get("signal_evaluation_ready", 0),
            "technical_longest_s": before["signal_evaluation_longest_s"],
            "coverage_block_s": before["durations_s"].get("coverage", 0),
            "fx_block_s": before["durations_s"].get("fx_normalization", 0),
            "fx_successful_receipt_max_gap_s": before["market_evidence"]["max_inter_event_gap_s"][
                "binance/fx"
            ],
            "lighter_requests_per_minute": before["requests_per_minute"],
            "lighter_peak_60s": before["sdk_rolling_60s_peak"],
            "notes": "before included one readonly preflight; startup/feed conditions differ",
        }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "technical_ready_s": result["durations_s"].get("signal_evaluation_ready", 0),
                "technical_longest_s": result["signal_evaluation_longest_s"],
                "fx_requests": result["fx"]["requests_in_window"],
                "fx_gap_max_s": result["fx"]["receipt_gaps_s"]["max"]
                if result["fx"]["receipt_gaps_s"]
                else None,
                "fx_block_s": result["fx"]["verified_fx_block_s"],
            }
        )
    )
