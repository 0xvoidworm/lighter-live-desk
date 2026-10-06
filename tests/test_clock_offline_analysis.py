"""Causal proof-comparison tests only; no engine, signer, or real transport."""

import importlib.util
import sys
from pathlib import Path

from lldesk.models import Stamp
from lldesk.source_time import ClockEvidence

spec = importlib.util.spec_from_file_location(
    "clock_offline_analysis", Path(__file__).parents[1] / "scripts/analyze_source_clock_session.py"
)
assert spec is not None and spec.loader is not None
analysis = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = analysis
spec.loader.exec_module(analysis)

WALL = 1_791_260_000_000_000_000
MONO = 100_000_000_000_000


def proof(*, age=0, low=-50, high=50, available=None, host=None):
    received = Stamp(receipt_ns=WALL - age * 1_000_000_000, monotonic_ns=MONO - age * 1_000_000_000)
    return analysis.RecordedProof(
        received.monotonic_ns if available is None else available,
        ClockEvidence(
            received,
            low,
            high,
            received.receipt_ns // 1_000_000,
            100,
            host or {"synchronized": True, "host_error_ms": 10},
        ),
    )


def quote(indicator_ms=200):
    return Stamp(receipt_ns=WALL, monotonic_ns=MONO, exchange_ms=WALL // 1_000_000 - indicator_ms)


def select(proofs, stamp=None, now=MONO, wall=WALL):
    return analysis.select_narrowest(proofs, stamp or quote(), now, wall, 1500)


def test_future_proof_is_unavailable_even_when_narrower():
    old = proof(low=-80, high=80)
    later = proof(low=-1, high=1, available=MONO + 1)
    assert select([later, old]) is old


def test_original_ttl_with_drift_and_fallback_to_newer():
    old = proof(age=119, low=-10, high=10)
    new = proof(low=-100, high=100)
    assert select([old, new]) is old
    # Same quote becomes older; do not manufacture an observation or reset TTL.
    assert select([old, new], now=MONO + 2_000_000_000, wall=WALL + 2_000_000_000) is new
    result = analysis.assess(
        old.evidence, quote(), MONO + 2_000_000_000, WALL + 2_000_000_000, 1500
    )
    assert result["reason"] == "SOURCE_CLOCK_EVIDENCE_EXPIRED"


def test_wall_step_host_invalid_and_uncertainty_are_not_bypassed():
    assert select([proof()], wall=WALL + 26_000_000) is None
    assert select([proof(host={"synchronized": False, "host_error_ms": 0})]) is None
    assert select([proof(host={"synchronized": True, "host_error_ms": 51})]) is None
    assert select([proof(low=-260, high=260)]) is None


def test_quality_selection_does_not_select_by_source_pass():
    narrow = proof(low=-100, high=10)
    wide = proof(low=-10, high=120)
    stamp = quote(50)
    assert select([wide, narrow], stamp) is narrow
    assert analysis.assess(wide.evidence, stamp, MONO, WALL, 1500)["passed"]
    assert (
        analysis.assess(narrow.evidence, stamp, MONO, WALL, 1500)["reason"]
        == "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN"
    )


def test_analysis_never_starts_a_clock_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Transport must not start during offline analysis")

    monkeypatch.setattr(analysis.BinanceSourceClock, "run", forbidden)
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    assert select([proof()]) is not None
    assert analysis.assess(proof().evidence, quote(), MONO, WALL, 1500)["passed"]


def test_containment_avoids_shifted_narrower_interval_regression():
    narrow = proof(low=-100, high=10)
    latest = proof(low=-10, high=120)
    stamp = quote(50)
    result = analysis.assess(latest.evidence, stamp, MONO, WALL, 1500)
    assert result["passed"]
    chosen = analysis.select_contained([narrow, latest], stamp, MONO, WALL, 1500, result)
    assert chosen is latest


def test_contained_old_interval_original_expiry_falls_back_to_new():
    old = proof(age=119, low=-10, high=10)
    latest = proof(low=-100, high=100)
    stamp = quote(80)
    result = analysis.assess(latest.evidence, stamp, MONO, WALL, 1500)
    assert not result["passed"]
    chosen = analysis.select_contained([old, latest], stamp, MONO, WALL, 1500, result)
    assert chosen is old
    assert analysis.assess(chosen.evidence, stamp, MONO, WALL, 1500)["passed"]
    now, wall = MONO + 2_000_000_000, WALL + 2_000_000_000
    result = analysis.assess(latest.evidence, stamp, now, wall, 1500)
    assert analysis.select_contained([old, latest], stamp, now, wall, 1500, result) is latest
