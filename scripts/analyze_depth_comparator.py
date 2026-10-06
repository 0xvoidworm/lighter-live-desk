"""Causal, read-only replay of identical public updates; never calls an exchange."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from lldesk.models import Stamp
from lldesk.source_time import BinanceSourceClock, ClockEvidence


def rows(path: Path):
    """Open gzip prefixes retain complete rows; missing trailers are reported elsewhere."""
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    break  # An incomplete final line cannot be evidence.
    except EOFError:
        pass


def quantiles(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    values = sorted(values)

    def at(q):
        x = (len(values) - 1) * q
        a = int(x)
        return values[a] + (values[min(a + 1, len(values) - 1)] - values[a]) * (x - a)

    return {
        "count": len(values),
        "min": values[0],
        "p50": at(0.5),
        "p95": at(0.95),
        "max": values[-1],
    }


def identity(payload: dict) -> tuple:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return tuple(payload.get(k) for k in ["U", "u", "pu", "E", "T"]) + (digest,)


def depth(row: dict) -> dict | None:
    if row.get("kind") == "comparison_depth":
        return row
    if row.get("venue") == "binance" and row.get("kind") == "depthUpdate":
        return row | {"receipt_monotonic_ns": row["monotonic_ns"]}
    return None


class CausalClock:
    def __init__(self, events: list[dict]):
        self.events = sorted(events, key=lambda row: row["monotonic_ns"])
        self.index = 0
        self.clock = BinanceSourceClock(1500)
        self.previous_receipt = -1

    def assess(self, row: dict) -> dict:
        mono, wall = row["receipt_monotonic_ns"], row["receipt_ns"]
        if mono < self.previous_receipt:
            raise ValueError("clock replay requires chronological receipts")
        self.previous_receipt = mono
        while self.index < len(self.events) and self.events[self.index]["monotonic_ns"] <= mono:
            event = self.events[self.index]
            self.index += 1
            e = event["payload"]["clock"]
            if e is None:
                self.clock.invalidate("recorded_global_or_unverified_clock")
            else:
                proof = ClockEvidence(
                    Stamp(receipt_ns=e["received_ns"], monotonic_ns=e["received_monotonic_ns"]),
                    e["offset_low_ms"],
                    e["offset_high_ms"],
                    e["server_ms"],
                    e["rtt_ms"],
                    e["host"],
                )
                old = self.clock.evidence
                if old is None or self.clock.identity(old) != self.clock.identity(proof):
                    self.clock.evidence = proof
        p = row["payload"]
        stamp = Stamp(
            exchange_ms=p.get("T"), exchange_event_ms=p.get("E"), receipt_ns=wall, monotonic_ns=mono
        )
        return self.clock.assess(stamp, now_ns=mono, wall_ns=wall)


def annotate(records: list[dict], clocks: list[dict]) -> list[dict]:
    clock = CausalClock(clocks)
    result = []
    for row in sorted(records, key=lambda x: x["receipt_monotonic_ns"]):
        assessment = clock.assess(row)
        low = assessment.get("source_age_low_ms")
        high = assessment.get("source_age_high_ms")
        # Invalid clocks cannot prove absolute lateness, even if an indicator is large.
        proof_valid = assessment.get("latest_proof", {}).get("valid", False)
        certain = proof_valid and low is not None and low >= 1500
        result.append(
            row
            | {
                "assessment": assessment,
                "certain_late": certain,
                "possibly_late": proof_valid and high is not None and high >= 1500,
            }
        )
    return result


def late_runs(records: list[dict]) -> list[dict]:
    runs, current = [], []
    for row in records + [None]:
        if row is not None and row["certain_late"]:
            current.append(row)
        elif current:
            runs.append(
                {
                    "count": len(current),
                    "start_receipt_monotonic_ns": current[0]["receipt_monotonic_ns"],
                    "end_receipt_monotonic_ns": current[-1]["receipt_monotonic_ns"],
                    "receipt_span_s": (
                        current[-1]["receipt_monotonic_ns"] - current[0]["receipt_monotonic_ns"]
                    )
                    / 1e9,
                    "max_source_age_lower_ms": max(
                        x["assessment"]["source_age_low_ms"] for x in current
                    ),
                    "max_source_age_upper_ms": max(
                        x["assessment"]["source_age_high_ms"] for x in current
                    ),
                }
            )
            current = []
    return runs


def compare(
    production: list[dict], comparator: list[dict], clocks: list[dict]
) -> tuple[dict, list[dict]]:
    prod, comp = annotate(production, clocks), annotate(comparator, clocks)
    lookup = []
    for records in (prod, comp):
        table = defaultdict(list)
        for row in records:
            table[identity(row["payload"])].append(row)
        lookup.append(table)
    common = lookup[0].keys() & lookup[1].keys()
    payloads = []
    for table in lookup:
        grouped = defaultdict(set)
        for key in table:
            grouped[key[:-1]].add(key[-1])
        payloads.append(grouped)
    payload_mismatches = sum(
        not (payloads[0][key] & payloads[1][key])
        for key in payloads[0].keys() & payloads[1].keys()
    )
    ambiguous = sum(len(lookup[0][k]) != 1 or len(lookup[1][k]) != 1 for k in common)
    pairs, differences, outcomes = [], [], Counter()
    for key in common:
        if len(lookup[0][key]) != 1 or len(lookup[1][key]) != 1:
            continue
        a, b = lookup[0][key][0], lookup[1][key][0]
        delta = (a["receipt_monotonic_ns"] - b["receipt_monotonic_ns"]) / 1e6
        if a["certain_late"] and b["certain_late"]:
            category = "both_certain_late"
        elif a["certain_late"] and b["assessment"]["passed"]:
            category = "production_only_certain_late"
        elif b["certain_late"] and a["assessment"]["passed"]:
            category = "comparator_only_certain_late"
        elif a["assessment"]["passed"] and b["assessment"]["passed"]:
            category = "both_source_valid"
        else:
            category = "uncertain_or_guard_rejected"
        outcomes[category] += 1
        differences.append(delta)
        pairs.append(
            {
                "identity": key,
                "production_minus_comparator_ms": delta,
                "category": category,
                "production": a,
                "comparator": b,
            }
        )
    pairs.sort(key=lambda x: x["production"]["receipt_monotonic_ns"])
    summary = {
        "production_count": len(prod),
        "comparator_count": len(comp),
        "pairs": len(pairs),
        "ambiguous_common_keys": ambiguous,
        "identity_payload_mismatch_keys": payload_mismatches,
        "production_unmatched_keys": len(lookup[0].keys() - lookup[1].keys()),
        "comparator_unmatched_keys": len(lookup[1].keys() - lookup[0].keys()),
        "production_minus_comparator_ms": quantiles(differences),
        "absolute_receipt_difference_ms": quantiles([abs(x) for x in differences]),
        "categories": dict(outcomes),
    }
    for name, records in [("production", prod), ("comparator", comp)]:
        summary[name] = {
            "raw_receipt_minus_E_ms": quantiles(
                [
                    r["receipt_ns"] / 1e6 - r["payload"]["E"]
                    for r in records
                    if type(r["payload"].get("E")) is int
                ]
            ),
            "corrected_source_age_lower_ms": quantiles(
                [
                    r["assessment"]["source_age_low_ms"]
                    for r in records
                    if "source_age_low_ms" in r["assessment"]
                    and r["assessment"].get("latest_proof", {}).get("valid", False)
                ]
            ),
            "corrected_source_age_upper_ms": quantiles(
                [
                    r["assessment"]["source_age_high_ms"]
                    for r in records
                    if "source_age_high_ms" in r["assessment"]
                    and r["assessment"].get("latest_proof", {}).get("valid", False)
                ]
            ),
            "source_reasons": dict(Counter(r["assessment"]["reason"] for r in records)),
            "certain_late_runs": late_runs(records),
        }
    return summary, pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("production", type=Path)
    parser.add_argument("comparator", type=Path)
    parser.add_argument("clock", type=Path)
    parser.add_argument("marker", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    marker = json.loads(args.marker.read_text())
    begin, end = marker["started_monotonic_ns"], marker["deadline_monotonic_ns"]

    def records(path):
        return [
            d
            for r in rows(path)
            if (d := depth(r)) is not None and begin <= d["receipt_monotonic_ns"] < end
        ]

    clocks = [r for r in rows(args.clock) if r.get("kind") == "source_clock"]
    summary, pairs = compare(records(args.production), records(args.comparator), clocks)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    with gzip.open(args.output / "pairs.jsonl.gz", "wt") as handle:
        for pair in pairs:
            handle.write(json.dumps(pair, separators=(",", ":")) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in ["production", "comparator"]}))


if __name__ == "__main__":
    main()
