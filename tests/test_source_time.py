"""No network/signing: clock bounds, recorded replay, sampling and risk isolation."""

import json
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_probe_only import engine as engine_fixture

from lldesk.market import Feeds
from lldesk.models import EngineState, Stamp
from lldesk.source_time import BinanceSourceClock, ClockEvidence
from lldesk.strategy import Decision, DeviationStrategy

engine = engine_fixture
WALL = 1_791_213_410_000_000_000
MONO = 99_000_000_000_000


def calibrated(*, low=-50, high=50):
    clock = BinanceSourceClock(1500)
    anchor = Stamp(receipt_ns=WALL, monotonic_ns=MONO)
    clock.evidence = ClockEvidence(anchor, low, high, WALL // 1_000_000, 80, {"synchronized": True})
    return clock


def sample(age_ms=200, receipt_age_ms=10):
    return Stamp(
        exchange_ms=WALL // 1_000_000 - age_ms,
        receipt_ns=WALL - receipt_age_ms * 1_000_000,
        monotonic_ns=MONO - receipt_age_ms * 1_000_000,
    )


def assess(clock, stamp):
    return clock.assess(stamp, now_ns=MONO, wall_ns=WALL)


def test_newly_received_but_source_late():
    result = assess(calibrated(), sample(4464, 732))
    assert result["receipt_age_ms"] == 732
    assert result["reason"] == "SOURCE_DATA_LATE" and not result["passed"]


@pytest.mark.parametrize("timestamp", [None, True, 1791213410, 1791213410000000, "1791213410000"])
def test_missing_or_wrong_units_never_substitute_now(timestamp):
    result = assess(calibrated(), replace(sample(), exchange_ms=timestamp))
    assert result["source_indicator_ms"] is None
    assert result["reason"] == "SOURCE_TIMESTAMP_MISSING_OR_INVALID"


@pytest.mark.parametrize(
    "age,reason", [(-300, "SOURCE_TIMESTAMP_FUTURE"), (0, "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN")]
)
def test_future_or_uncertain_future_blocks(age, reason):
    assert assess(calibrated(), sample(age))["reason"] == reason


def test_clock_unknown_uncertain_expired_and_wall_step_block():
    assert assess(BinanceSourceClock(1500), sample())["reason"] == "SOURCE_CLOCK_NOT_VERIFIED"
    assert (
        assess(calibrated(low=-300, high=300), sample(500))["reason"]
        == "SOURCE_CLOCK_TOO_UNCERTAIN"
    )
    c = calibrated()
    assert (
        c.assess(sample(), now_ns=MONO + 121_000_000_000, wall_ns=WALL + 121_000_000_000)["reason"]
        == "SOURCE_CLOCK_EVIDENCE_EXPIRED"
    )
    assert (
        c.assess(sample(), now_ns=MONO, wall_ns=WALL + 30_000_000)["reason"]
        == "SOURCE_CLOCK_WALL_STEP_OR_INCONSISTENT_STAMP"
    )


def test_normal_and_nonzero_offset():
    assert assess(calibrated(), sample())["passed"]
    # Exchange is 2 seconds ahead; raw cross-clock indicator alone would be misleading.
    r = assess(calibrated(low=1950, high=2050), sample(-1800))
    assert r["passed"] and r["source_age_low_ms"] == pytest.approx(150, abs=0.001)


@pytest.mark.parametrize(
    "receipt,quote_mono,T,anchor_wall,anchor_mono,low,high,server,now,wall",
    [
        (
            1791224747657173183,
            110429896109037,
            1791224747589,
            1791224723792929977,
            110406031864672,
            -68.977709640625,
            130.312670578125,
            1791224723736,
            110429898307950,
            1791224747659373635,
        ),
        (
            1791225492377586803,
            111174616521244,
            1791225492301,
            1791225443825534797,
            111126064469898,
            -113.10077646875,
            128.667426859375,
            1791225443725,
            111174656899302,
            1791225492417965251,
        ),
    ],
)
def test_recorded_second_assessments_are_within_ten_microseconds_of_boundary(
    receipt, quote_mono, T, anchor_wall, anchor_mono, low, high, server, now, wall
):
    clock = BinanceSourceClock(1500)
    clock.evidence = ClockEvidence(
        Stamp(receipt_ns=anchor_wall, monotonic_ns=anchor_mono),
        low,
        high,
        server,
        0,
        {"synchronized": True},
    )
    stamp = Stamp(receipt_ns=receipt, monotonic_ns=quote_mono, exchange_ms=T)
    recorded = clock.assess(stamp, now_ns=now, wall_ns=wall)
    earlier_model = clock.assess(stamp, now_ns=now - 10_000, wall_ns=wall - 10_000)
    assert recorded["passed"] and -1 <= recorded["source_age_low_ms"] < -0.99
    assert earlier_model["reason"] == "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN"
    # Earlier evaluation is a counterfactual boundary regression, not proof of its real time.


def test_rest_interval_bound_and_unsync_revokes_old_proof():
    c = BinanceSourceClock(1500)
    start = Stamp(receipt_ns=WALL, monotonic_ns=MONO)
    end = Stamp(receipt_ns=WALL + 80_000_000, monotonic_ns=MONO + 80_000_000)
    assert c.install(
        start, end, WALL // 1_000_000 + 40, {"synchronized": True, "host_error_ms": 10}
    )
    assert (c.evidence.offset_low_ms, c.evidence.offset_high_ms) == (-51, 51)
    assert not c.install(start, end, WALL // 1_000_000, {"synchronized": False})
    assert c.evidence is None


def test_exact_reviewer_record_is_blocked_without_historical_clock_and_in_conditional_replay():
    f = json.loads(
        (Path(__file__).parent / "fixtures/source-time-reviewer-20261005.json").read_text()
    )
    stamp = Stamp(**f["stamp"])
    now = stamp.monotonic_ns + round(f["receipt_age_ms"] * 1e6)
    wall = round(f["observed_at"] * 1e9)
    c = BinanceSourceClock(1500)
    assert c.assess(stamp, now_ns=now, wall_ns=wall)["reason"] == "SOURCE_CLOCK_NOT_VERIFIED"
    # Model only, not evidence of the historical offset. Source age ~4.46s must fail.
    c.evidence = ClockEvidence(stamp, -100, 100, stamp.exchange_ms, 180, {"synchronized": True})
    r = c.assess(stamp, now_ns=now, wall_ns=wall)
    assert r["reason"] == "SOURCE_DATA_LATE" and r["source_age_high_ms"] > 4500


def live_stamp(age_ms):
    return Stamp(exchange_ms=time.time_ns() // 1_000_000 - age_ms)


def restore_real_guards(engine):
    del engine.data_valid  # Remove the isolated fixture's existing entry stub.
    del engine.source_freshness
    engine.feeds = SimpleNamespace(binance_public_ok=True, binance_trades_ok=True)
    engine.books["binance"].bridged = True
    engine.books["binance"].updated_ns = time.monotonic_ns()
    engine.source_clock.evidence = ClockEvidence(
        Stamp(), -30, 30, time.time_ns() // 1_000_000, 40, {"synchronized": True}
    )
    engine.strategy = DeviationStrategy()


@pytest.mark.parametrize("initial_passed", [False, True])
def test_decision_records_actual_guard_at_future_boundary(engine, monkeypatch, initial_passed):
    restore_real_guards(engine)
    engine.config.allow_live_orders = False
    engine.state = EngineState.HALTED  # Isolate sampling from IDLE's separate fresh check.
    anchor = Stamp()
    stamp = replace(anchor, exchange_ms=anchor.receipt_ns // 1_000_000)
    indicator = stamp.receipt_ns / 1e6 - stamp.exchange_ms
    engine.books["binance"].stamp = stamp
    engine.source_clock.evidence = ClockEvidence(
        anchor, -indicator - 1.005, 100, stamp.exchange_ms, 80, {"synchronized": True}
    )
    real_assess = engine.source_clock.assess
    shifts = [10_000, 0] if initial_passed else [0, 10_000]
    calls = []

    def boundary_assess(stamp):
        shift = shifts[len(calls)]
        result = real_assess(
            stamp,
            now_ns=anchor.monotonic_ns + shift,
            wall_ns=anchor.receipt_ns + shift,
        )
        calls.append(result)
        return result

    monkeypatch.setattr(engine.source_clock, "assess", boundary_assess)
    engine.decide()  # Real Engine.data_valid/market_data_valid and real clock assessment.
    inputs = engine.last_decision_inputs
    assert len(calls) == 1
    assert inputs["data_valid"] is initial_passed
    assert inputs["source_freshness"] is calls[0]
    guard = inputs["data_guard"]
    assert guard["passed"] is initial_passed and guard["market_data_valid"]
    assert guard["source_checked"]
    assert guard["started_monotonic_ns"] <= guard["finished_monotonic_ns"]
    assert calls[0]["clock_proof_id"].endswith(str(anchor.monotonic_ns))
    assert calls[0]["evaluated_wall_ns"] == anchor.receipt_ns + shifts[0]
    assert engine.data_valid() is not initial_passed  # Fresh dispatch check, no cached grant.
    assert len(calls) == 2
    assert inputs["source_freshness"]["passed"] is initial_passed  # Record is immutable by reuse.
    assert engine.account.sent == []  # Isolated fake transport; no signing/network.


def test_decision_records_market_failure_without_inventing_source_assessment(engine):
    restore_real_guards(engine)
    engine.config.allow_live_orders = False
    engine.books["lighter"].updated_ns = time.monotonic_ns() - 2_000_000_000
    engine.decide()
    guard = engine.last_decision_inputs["data_guard"]
    assert not guard["passed"] and not guard["market_data_valid"]
    assert not guard["source_checked"] and guard["source_freshness"] is None
    assert engine.last_decision_inputs["source_freshness"] is None
    assert engine.account.sent == []


@pytest.mark.parametrize("condition", ["late", "missing", "future", "uncertain"])
def test_rejected_source_delta_updates_sequence_but_not_coverage_or_basis(engine, condition):
    restore_real_guards(engine)
    b = engine.books["binance"]
    b.stamp = live_stamp(4500)
    if condition == "missing":
        b.stamp = replace(b.stamp, exchange_ms=None)
    if condition == "future":
        b.stamp = live_stamp(-300)
    if condition == "uncertain":
        b.stamp = live_stamp(200)
        engine.source_clock.evidence = replace(
            engine.source_clock.evidence, offset_low_ms=-300, offset_high_ms=300
        )
    b.binance_delta({"U": 2, "u": 2, "pu": 1, "b": [["83000", "3"]], "a": []}, b.stamp)
    engine.decide()
    assert b.sequence == 2 and b.bids[Decimal(83000)] == 3
    assert engine.market_data_valid() and not engine.data_valid()
    assert len(engine.strategy.samples) == 0 and engine.strategy.last_binance_mid is None
    assert not engine.preflight_valid("recorded-replay")
    engine.source_clock.evidence = replace(
        engine.source_clock.evidence, offset_low_ms=-30, offset_high_ms=30
    )
    b.stamp = live_stamp(200)
    engine.decide()
    assert len(engine.strategy.samples) == 1


async def test_parser_keeps_T_and_E_separate_missing_T_not_fabricated(engine):
    feeds = Feeds(1, engine.queue)
    await feeds._binance_message({"e": "depthUpdate", "s": "BTCUSDT", "E": WALL // 1_000_000})
    event = engine.queue.get_nowait()
    assert event.stamp.exchange_ms is None and event.stamp.exchange_event_ms == WALL // 1_000_000


async def test_source_unknown_does_not_hold_stop_timer_close_or_normal_exit(engine):
    restore_real_guards(engine)
    engine.source_clock.evidence = None
    engine.account_snapshot = replace(engine.account_snapshot, position_qty=Decimal("0.00012"))
    engine.protect_position = AsyncMock()
    engine.close_position = AsyncMock()
    assert not engine.data_valid()
    await engine.manage_position()
    engine.protect_position.assert_awaited_once()
    engine.protection_id = 2
    engine.unprotected_since = time.monotonic() - 9
    await engine.manage_position()
    engine.close_position.assert_awaited_once()
    engine.close_position.reset_mock()
    engine.db.set_flag("close_requested", "0")
    engine.unprotected_since = 0
    engine.protection_confirmed = True
    engine.exit_basis_bps = Decimal(0)
    engine.last_decision = Decision(None, None, None, None, ("DATA_TIDAK_VALID",))
    await engine.manage_position()
    engine.close_position.assert_awaited_once()  # Same reversion rule, without sampling.
