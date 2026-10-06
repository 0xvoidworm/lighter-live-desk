"""Offline clock-evidence review; never opens a transport or alters engine state.

Uses the installed production validator rather than reimplementing its guards.
The alternative is an analysis of proof selection, not a production policy.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import inspect
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lldesk.models import Stamp
from lldesk.source_time import (
    CLOCK_TTL_S,
    DRIFT_PPM,
    MAX_HOST_ERROR_MS,
    BinanceSourceClock,
    ClockEvidence,
)

STRUCTURAL_FAILURES = {
    "SOURCE_CLOCK_NOT_VERIFIED",
    "SOURCE_CLOCK_EVIDENCE_EXPIRED",
    "SOURCE_CLOCK_WALL_STEP_OR_INCONSISTENT_STAMP",
    "SOURCE_CLOCK_TOO_UNCERTAIN",
    "SOURCE_TIMESTAMP_MISSING_OR_INVALID",
}


@dataclass(frozen=True)
class RecordedProof:
    available_ns: int
    evidence: ClockEvidence

    @property
    def identity(self) -> str:
        return f"{self.evidence.server_ms}:{self.evidence.received.monotonic_ns}"


def read_prefix(path: Path):
    """Existing gzip prefixes can lack a trailer; only complete JSON lines count."""
    try:
        with gzip.open(path, "rt") as source:
            for line in source:
                if not line.endswith("\n"):
                    continue
                yield json.loads(line)
    except EOFError:
        pass


def assess(evidence: ClockEvidence | None, stamp: Stamp, now: int, wall: int, max_age: int):
    clock = BinanceSourceClock(max_age)
    clock.evidence = evidence
    return clock.assess(stamp, now_ns=now, wall_ns=wall)


def select_narrowest(
    proofs: list[RecordedProof], stamp: Stamp, now: int, wall: int, max_age: int
) -> RecordedProof | None:
    """Analysis only: minimum drift-expanded width, independent of source-age pass.

    Availability, each proof's original TTL, original host validation, wall/stamp
    consistency, and the production uncertainty ceiling all remain mandatory.
    No intersections, extended TTL, future calibration, or price-based selection.
    """
    candidates = []
    for proof in proofs:
        evidence = proof.evidence
        if proof.available_ns > now or not evidence.host.get("synchronized"):
            continue
        if not 0 <= evidence.host.get("host_error_ms", math.inf) <= MAX_HOST_ERROR_MS:
            continue
        result = assess(evidence, stamp, now, wall, max_age)
        if result["reason"] in STRUCTURAL_FAILURES:
            continue
        candidates.append((result["clock_uncertainty_ms"], -proof.available_ns, proof))
    return min(candidates, key=lambda item: item[:2])[2] if candidates else None


def select_contained(
    proofs: list[RecordedProof],
    stamp: Stamp,
    now: int,
    wall: int,
    max_age: int,
    latest_assessment: dict,
) -> RecordedProof | None:
    """Analysis only: use old proof only if its complete interval is contained.

    An old narrow interval shifted across the future boundary cannot displace
    the latest interval. New accepted proof remains the fallback at old TTL.
    """
    if latest_assessment["reason"] in STRUCTURAL_FAILURES:
        return None
    candidates = []
    for proof in proofs:
        if proof.available_ns > now or not proof.evidence.host.get("synchronized"):
            continue
        if not 0 <= proof.evidence.host.get("host_error_ms", math.inf) <= MAX_HOST_ERROR_MS:
            continue
        result = assess(proof.evidence, stamp, now, wall, max_age)
        if result["reason"] in STRUCTURAL_FAILURES:
            continue
        if (
            result["clock_offset_low_ms"] >= latest_assessment["clock_offset_low_ms"]
            and result["clock_offset_high_ms"] <= latest_assessment["clock_offset_high_ms"]
        ):
            candidates.append((result["clock_uncertainty_ms"], -proof.available_ns, proof))
    return min(candidates, key=lambda item: item[:2])[2] if candidates else None


def verify_manifest(directory: Path, manifest: Path) -> int:
    lines = manifest.read_text().splitlines()
    for line in lines:
        expected, name = line.split("  ", 1)
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Manifest escapes evidence directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Evidence hash mismatch: {name}")
    return len(lines)


def wib(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone(timedelta(hours=7))).isoformat()


def analyze(directory: Path, manifest: Path) -> dict:
    verified = verify_manifest(directory, manifest)
    validator_file = Path(inspect.getfile(BinanceSourceClock))
    validator_hash = hashlib.sha256(validator_file.read_bytes()).hexdigest()
    start = json.loads((directory / "started.json").read_text())
    assert validator_hash == start["source_hashes"]["src/lldesk/source_time.py"]
    finish = json.loads((directory / "finished.json").read_text())
    session = finish["session"]
    begin, end = session["started_at"], session["deadline_epoch"]
    records, proofs = [], []
    for path in sorted((directory / "raw").glob("market*.gz")):
        for row in read_prefix(path):
            if row["kind"] != "source_clock":
                continue
            at = row["payload"]["attempt"]["started_ns"] / 1e9
            if begin - 180 <= at < end:
                records.append(row)
    records.sort(key=lambda row: row["monotonic_ns"])
    timeline, previous_evidence = [], None
    for row in records:
        attempt, current = row["payload"]["attempt"], row["payload"]["clock"]
        accepted = attempt["outcome"] == "verified"
        host = attempt["host"]
        # This bounded dataset has no unsynchronized-host invalidation. Do not
        # generalize a retrospective proof cache over an unrecorded invalidation.
        assert host["synchronized"] and host["host_error_ms"] <= MAX_HOST_ERROR_MS
        evidence = None
        if accepted:
            assert current["received_ns"] == attempt["received_ns"]
            received = Stamp(
                receipt_ns=current["received_ns"],
                monotonic_ns=current["received_monotonic_ns"],
            )
            started = Stamp(
                receipt_ns=attempt["started_ns"],
                monotonic_ns=received.monotonic_ns - round(attempt["duration_ms"] * 1e6),
            )
            validator = BinanceSourceClock(1500)
            assert validator.install(started, received, current["server_ms"], host)
            evidence = validator.evidence
            assert evidence is not None
            assert abs(evidence.offset_low_ms - current["offset_low_ms"]) < 1e-8
            assert abs(evidence.offset_high_ms - current["offset_high_ms"]) < 1e-8
            proofs.append(RecordedProof(row["monotonic_ns"], evidence))
        received_ns = attempt["received_ns"]
        wall_elapsed_ms = (received_ns - attempt["started_ns"]) / 1e6
        comparison_ns = evidence.received.monotonic_ns if evidence else row["monotonic_ns"]
        old_age = (
            (comparison_ns - previous_evidence.received.monotonic_ns) / 1e9
            if previous_evidence
            else None
        )
        old_valid = bool(previous_evidence and old_age is not None and 0 <= old_age <= CLOCK_TTL_S)
        if old_valid:
            # Clock validity only: reuse its own actual calibration observation,
            # including original receipt and returned serverTime. A stale source
            # result here is not a strategy sample or a fresh market observation.
            comparison_stamp = Stamp(
                receipt_ns=previous_evidence.received.receipt_ns,
                monotonic_ns=previous_evidence.received.monotonic_ns,
                exchange_ms=previous_evidence.server_ms,
            )
            old_check = assess(
                previous_evidence, comparison_stamp, comparison_ns, received_ns, 1500
            )
            old_valid = old_check["reason"] not in STRUCTURAL_FAILURES
        old_uncertainty = (
            (previous_evidence.offset_high_ms - previous_evidence.offset_low_ms) / 2
            + old_age * DRIFT_PPM / 1000
            if old_valid and previous_evidence and old_age is not None
            else None
        )
        new_uncertainty = (
            (evidence.offset_high_ms - evidence.offset_low_ms) / 2 if evidence else None
        )
        if begin <= attempt["started_ns"] / 1e9 < end:
            if not accepted:
                assert attempt["outcome"] == "clock_unverified"
                assert attempt["duration_ms"] > 400
            timeline.append(
                {
                    "request_id": attempt["id"],
                    "started_wall_ns": attempt["started_ns"],
                    "received_wall_ns": received_ns,
                    "received_monotonic_ns": evidence.received.monotonic_ns if evidence else None,
                    "started_monotonic_ns_reconstructed": started.monotonic_ns
                    if evidence
                    else None,
                    "post_response_record_monotonic_ns": row["monotonic_ns"],
                    "started_wib": wib(attempt["started_ns"]),
                    "received_wib": wib(received_ns),
                    "request_full_rtt_ms": attempt["duration_ms"],
                    "wall_elapsed_minus_rtt_ms": wall_elapsed_ms - attempt["duration_ms"],
                    "http_status": attempt["status"],
                    "outcome": attempt["outcome"],
                    "server_time_ms": evidence.server_ms if evidence else None,
                    "server_time_missing_reason": None
                    if evidence
                    else "new response not saved; clock object is retained old proof",
                    "chrony": host,
                    "new_offset_low_ms": evidence.offset_low_ms if evidence else None,
                    "new_offset_high_ms": evidence.offset_high_ms if evidence else None,
                    "new_uncertainty_ms": new_uncertainty,
                    "new_drift_ms_at_receive": 0 if evidence else None,
                    "previous_proof_id": (
                        f"{previous_evidence.server_ms}:{previous_evidence.received.monotonic_ns}"
                        if previous_evidence
                        else None
                    ),
                    "previous_base_offset_low_ms": previous_evidence.offset_low_ms
                    if previous_evidence
                    else None,
                    "previous_base_offset_high_ms": previous_evidence.offset_high_ms
                    if previous_evidence
                    else None,
                    "previous_proof_still_within_ttl": old_valid,
                    "previous_age_s_at_comparison": old_age,
                    "previous_drift_ms": old_age * DRIFT_PPM / 1000
                    if old_age is not None
                    else None,
                    "previous_uncertainty_ms_if_valid": old_uncertainty,
                    "new_wider_than_valid_previous": (
                        new_uncertainty > old_uncertainty
                        if new_uncertainty is not None and old_uncertainty is not None
                        else None
                    ),
                    "new_contains_valid_previous": (
                        evidence.offset_low_ms
                        <= previous_evidence.offset_low_ms - old_age * DRIFT_PPM / 1000
                        and evidence.offset_high_ms
                        >= previous_evidence.offset_high_ms + old_age * DRIFT_PPM / 1000
                        if evidence and previous_evidence and old_valid and old_age is not None
                        else None
                    ),
                    "original_ttl_s": CLOCK_TTL_S,
                    "new_monotonic_ttl_deadline_ns": (
                        evidence.received.monotonic_ns + CLOCK_TTL_S * 1_000_000_000
                        if evidence
                        else None
                    ),
                    "previous_ttl_remaining_s": max(0, CLOCK_TTL_S - old_age)
                    if old_age is not None
                    else None,
                    "retained_proof_received_wall_ns": current["received_ns"] if current else None,
                    "acceptance_reason": "all production install guards replayed"
                    if accepted
                    else "RTT exceeds 400ms; sufficient rejection, new serverTime unavailable",
                    "request_phase_dns_tcp_tls_headers_body": "not recorded",
                    "comparison_clock": "response receive monotonic"
                    if accepted
                    else (
                        "post-response record monotonic; "
                        "exact request receive monotonic unavailable"
                    ),
                }
            )
        if evidence:
            previous_evidence = evidence
    assert len(timeline) == 30
    lookup = {proof.identity: proof for proof in proofs}
    durations, transition_seconds = collections.Counter(), collections.Counter()
    contained_transitions, contained_switches = collections.Counter(), 0
    counts, proof_switches = collections.Counter(), 0
    mismatches, previous, covered, replay_count = [], None, 0.0, 0
    selected_changes = collections.Counter()
    for row in read_prefix(directory / "snapshots.jsonl.gz"):
        if "status" not in row:
            continue
        observation = row["status"]["diagnostic_observation"]
        actual = observation["source_freshness"]
        quote = observation["quotes"]["binance"]
        stamp = Stamp(
            receipt_ns=quote["receipt_ns"],
            monotonic_ns=quote["monotonic_ns"],
            exchange_ms=quote["exchange_ms"],
        )
        now, wall = actual["evaluated_monotonic_ns"], actual["evaluated_wall_ns"]
        installed = lookup.get(actual.get("clock_proof_id"))
        replay = assess(
            installed.evidence if installed else None, stamp, now, wall, actual["max_source_age_ms"]
        )
        if (replay["reason"], replay["passed"]) != (actual["reason"], actual["passed"]):
            mismatches.append(row["epoch"])
        for key in ["clock_uncertainty_ms", "source_age_low_ms", "source_age_high_ms"]:
            if key in actual:
                assert abs(replay[key] - actual[key]) < 1e-8
        selected = select_narrowest(proofs, stamp, now, wall, actual["max_source_age_ms"])
        # Preserve global invalidation/wall-step failure; this study must never
        # select another proof to bypass the production wall-step rejection.
        if actual["reason"] in {
            "SOURCE_CLOCK_NOT_VERIFIED",
            "SOURCE_CLOCK_WALL_STEP_OR_INCONSISTENT_STAMP",
        }:
            selected = None
        alternative = (
            assess(selected.evidence, stamp, now, wall, actual["max_source_age_ms"])
            if selected
            else replay
        )
        contained = select_contained(proofs, stamp, now, wall, actual["max_source_age_ms"], replay)
        contained_result = (
            assess(contained.evidence, stamp, now, wall, actual["max_source_age_ms"])
            if contained
            else replay
        )
        if previous:
            dt = max(0.0, min(end, row["epoch"]) - max(begin, previous["epoch"]))
            if dt:
                covered += dt
                for name in ["actual", "alternative", "contained"]:
                    durations[name + ":" + previous[name]["reason"]] += dt
                transition_seconds[
                    (previous["actual"]["passed"], previous["alternative"]["passed"])
                ] += dt
                contained_transitions[
                    (previous["actual"]["passed"], previous["contained"]["passed"])
                ] += dt
        if begin <= row["epoch"] < end:
            replay_count += 1
            counts[(actual["reason"], alternative["reason"])] += 1
            if selected and selected.identity != actual.get("clock_proof_id"):
                proof_switches += 1
                selected_changes[selected.identity] += 1
            if contained and contained.identity != actual.get("clock_proof_id"):
                contained_switches += 1
        previous = {
            "epoch": row["epoch"],
            "actual": actual,
            "alternative": alternative,
            "contained": contained_result,
        }
    assert not mismatches
    assert abs(covered - 1800) < 1e-6
    return {
        "input_files_hash_verified": verified,
        "runtime_source_commit": "1205d35",
        "native_validator_source_sha256": validator_hash,
        "session_id": session["id"],
        "active_seconds": covered,
        "timeline": timeline,
        "accepted": sum(row["outcome"] == "verified" for row in timeline),
        "rejected": sum(row["outcome"] != "verified" for row in timeline),
        "comparable_replacements": sum(
            row["new_wider_than_valid_previous"] is not None for row in timeline
        ),
        "wider_replacements": sum(row["new_wider_than_valid_previous"] is True for row in timeline),
        "nested_wider_replacements": sum(
            row["new_contains_valid_previous"] is True for row in timeline
        ),
        "production_assessment_replay_samples": replay_count,
        "production_assessment_mismatches": len(mismatches),
        "source_reason_seconds": dict(durations),
        "actual_reject_alternative_pass_seconds": transition_seconds[(False, True)],
        "actual_pass_alternative_reject_seconds": transition_seconds[(True, False)],
        "alternative_proof_switch_samples": proof_switches,
        "contained_policy": {
            "description": (
                "offline only: keep a previously accepted proof only if its entire "
                "drift-expanded interval is contained in latest valid proof interval; "
                "native guards and original TTL apply; retain latest as fallback; "
                "no interval intersection or selection by source-age pass"
            ),
            "actual_reject_alternative_pass_seconds": contained_transitions[(False, True)],
            "actual_pass_alternative_reject_seconds": contained_transitions[(True, False)],
            "proof_switch_samples": contained_switches,
        },
        "transition_samples": [
            {"actual_reason": key[0], "alternative_reason": key[1], "count": val}
            for key, val in counts.items()
        ],
        "alternative_policy": (
            "offline only: minimum drift-expanded uncertainty among previously received "
            "accepted proofs, each within its original TTL, native wall/stamp/host/uncertainty "
            "checks; no source-price/pass-based selection, interval intersection, "
            "future information, or extended lifetime"
        ),
        "coverage": {
            "production_blocked_seconds": 586.6707012653351,
            "production_max_valid_sample_gap_s": 60.291,
            "alternative_coverage_computed": False,
            "reason": (
                "sample-time clock assessment/proof and exact engine processing timestamp "
                "are absent; no missing strategy samples generated or gap removed; "
                "unchanged proof-expiry interval cannot be repaired by retention"
            ),
        },
        "limitations": [
            "No DNS/TCP/TLS/reuse/response phase timings in this time-client trace.",
            "Rejected response serverTime and exact request receive monotonic are not saved.",
            "Sampled durations hold captured assessments between local polls, "
            "not all source evaluations.",
            "Counterfactual only re-evaluates already-recorded diagnostic quote/stamp inputs; "
            "no live readiness, strategy sample, basis, signal, fill or profitability result.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = analyze(args.evidence_dir, args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{args.output}: verified timeline and causal offline source assessment comparison")


if __name__ == "__main__":
    main()
