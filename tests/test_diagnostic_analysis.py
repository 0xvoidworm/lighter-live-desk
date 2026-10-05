"""Offline economic estimates must not invent fees or double-count spread."""

import importlib.util
from decimal import Decimal as D
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "diagnostic_analysis", Path(__file__).parents[1] / "scripts/analyze_diagnostic_recording.py"
)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


@pytest.mark.parametrize("residual", [D(-1), D(1)])
def test_no_reversion_loses_one_full_spread_and_keeps_fee_unknown(residual):
    result = analysis.book_reversion_estimate(D(101), D(99), D(100), D(100), residual, D(0), D(0))
    entry = D(101) if residual < 0 else D(99)
    assert result["edge_after_two_local_book_legs_bps"] == -D(2) / entry * 10000
    assert result["roundtrip_fee_assumption_bps"] == 0
    assert result["fee_exact"] is None


def test_depth_fee_and_buffer_can_eliminate_positive_mid_reversion():
    # Mid 100 -> 100.1 looks positive, but same-size VWAP/depth consumes it.
    result = analysis.book_reversion_estimate(
        D("100.08"), D("99.92"), D(100), D("100.1"), D(-10), D("0.3"), D(5)
    )
    assert result["edge_after_two_local_book_legs_bps"] < 0
    assert result["edge_after_fee_assumption_and_5bps_buffer"] < D("-5.6")
    assert result["roundtrip_fee_assumption_bps"] == D("0.6")
    assert result["fee_exact"] is None
