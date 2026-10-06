"""Causal offline replay: immutable hash-verified old validator vs production selection.

No engine, signer, network or historical evidence mutations. Existing Python pipeline.
"""

import argparse
import collections
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import analyze_source_clock_session as historical

from lldesk.models import Stamp
from lldesk.source_time import BinanceSourceClock, ClockEvidence

BASELINE_SHA = "5eb035308042dd7e71567b9c41cc6f5daf7bcd1069e784c09d1fbf6ef79a97b7"
ANALYZER_SHA = "bb8aca03ae2a671b26b39f3d5c565600c5941daf1caeb1d49da392226b2eb236"


def baseline():
    root = Path(__file__).parents[1]
    path = root / "audits/baselines/source_time_1205d35.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == BASELINE_SHA
    assert hashlib.sha256(Path(historical.__file__).read_bytes()).hexdigest() == ANALYZER_SHA
    spec = importlib.util.spec_from_file_location("lldesk._baseline_source_time", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def replay(directory: Path, manifest: Path) -> dict:
    legacy = baseline()
    # Original analyzer/hash assertions unchanged; bind it to its exact historical validator.
    historical.BinanceSourceClock = legacy.BinanceSourceClock
    original = historical.analyze(directory, manifest)
    start = json.loads((directory / "started.json").read_text())
    assert start["source_hashes"]["src/lldesk/source_time.py"] == BASELINE_SHA
    session = json.loads((directory / "finished.json").read_text())["session"]
    begin, end = session["started_at"], session["deadline_epoch"]
    proofs = []
    for path in sorted((directory / "raw").glob("market*.gz")):
        for row in historical.read_prefix(path):
            if row["kind"] != "source_clock":
                continue
            payload = row["payload"]
            if payload["attempt"]["outcome"] != "verified":
                continue
            e = payload["clock"]
            proofs.append(
                (
                    row["monotonic_ns"],
                    ClockEvidence(
                        Stamp(receipt_ns=e["received_ns"], monotonic_ns=e["received_monotonic_ns"]),
                        e["offset_low_ms"],
                        e["offset_high_ms"],
                        e["server_ms"],
                        e["rtt_ms"],
                        e["host"],
                    ),
                )
            )
    proofs.sort(key=lambda p: p[0])
    durations, transitions = collections.Counter(), collections.Counter()
    previous = None
    samples = selected_old = baseline_mismatches = latest_regressions = 0
    for row in historical.read_prefix(directory / "snapshots.jsonl.gz"):
        if "status" not in row:
            continue
        observation = row["status"]["diagnostic_observation"]
        actual, q = observation["source_freshness"], observation["quotes"]["binance"]
        now, wall = actual["evaluated_monotonic_ns"], actual["evaluated_wall_ns"]
        stamp = Stamp(
            receipt_ns=q["receipt_ns"], monotonic_ns=q["monotonic_ns"], exchange_ms=q["exchange_ms"]
        )
        available = [e for at, e in proofs if at <= now]
        clock = BinanceSourceClock(actual["max_source_age_ms"])
        for evidence in available[-3:]:
            clock.evidence = evidence
        before = legacy.BinanceSourceClock(actual["max_source_age_ms"])
        before.evidence = clock.evidence
        before_result = before.assess(stamp, now_ns=now, wall_ns=wall)
        result = clock.assess(stamp, now_ns=now, wall_ns=wall)
        if begin <= row["epoch"] < end:
            samples += 1
            baseline_mismatches += (before_result["reason"], before_result["passed"]) != (
                actual["reason"],
                actual["passed"],
            )
            selected_old += result.get("selected_proof_id") != result.get("latest_proof_id")
            latest_regressions += before_result["passed"] and not result["passed"]
        if previous:
            dt = max(0, min(end, row["epoch"]) - max(begin, previous["epoch"]))
            durations["baseline:" + previous["before"]["reason"]] += dt
            durations["production_policy_offline:" + previous["after"]["reason"]] += dt
            transitions[(previous["before"]["passed"], previous["after"]["passed"])] += dt
        previous = {"epoch": row["epoch"], "before": before_result, "after": result}
    assert baseline_mismatches == latest_regressions == 0
    return {
        "scope": "offline counterfactual, not runtime/coverage/live readiness",
        "baseline_validator_sha256": BASELINE_SHA,
        "historical_analyzer_sha256": ANALYZER_SHA,
        "historical_files_verified": original["input_files_hash_verified"],
        "baseline_replay_mismatches": original["production_assessment_mismatches"],
        "new_replay_baseline_mismatches": baseline_mismatches,
        "samples": samples,
        "selected_old_samples": selected_old,
        "latest_pass_selection_reject_samples": latest_regressions,
        "baseline_reject_selection_pass_seconds": transitions[(False, True)],
        "baseline_pass_selection_reject_seconds": transitions[(True, False)],
        "source_reason_seconds": dict(durations),
        "coverage_alternative": None,
        "limits": "Original TTL, drift, anchors and timestamp pairs; only already available "
        "evidence, max three accepted proofs. No synthesized strategy samples/gap removal.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(replay(args.source, args.manifest), indent=2) + "\n")
    print(args.output)
