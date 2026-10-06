"""Production proof selection and local HTTP tracing; no trading transport/imports."""

import asyncio
import hashlib
import importlib.util
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

import lldesk.source_time as source
from lldesk.models import Stamp
from lldesk.source_time import BinanceSourceClock, ClockEvidence
from lldesk.websocket_trace import WebsocketTrace

WALL = 1_791_260_000_000_000_000
MONO = 100_000_000_000_000
HOST = {"synchronized": True, "host_error_ms": 10}
BASELINE_SHA = "5eb035308042dd7e71567b9c41cc6f5daf7bcd1069e784c09d1fbf6ef79a97b7"


def proof(age=0, low=-50, high=50):
    delta = round(age * 1e9)
    return ClockEvidence(
        Stamp(receipt_ns=WALL - delta, monotonic_ns=MONO - delta),
        low,
        high,
        (WALL - delta) // 1_000_000,
        80,
        HOST,
    )


def quote(age_ms=100):
    return Stamp(receipt_ns=WALL, monotonic_ns=MONO, exchange_ms=WALL // 1_000_000 - age_ms)


def assessment(clock, stamp=None, delta=0):
    return clock.assess(
        quote() if stamp is None else stamp,
        now_ns=MONO + delta,
        wall_ns=WALL + delta,
    )


def clock_with(*proofs):
    clock = BinanceSourceClock(1500)
    for evidence in proofs:
        clock.evidence = evidence
    return clock


def test_contained_old_selected_latest_preserved_and_expiry_fallback():
    old, latest = proof(119, -10, 10), proof(0, -200, 200)
    clock = clock_with(old, latest)
    result = assessment(clock)
    assert result["passed"] and result["selected_proof_id"] == clock.identity(old)
    assert not assessment(clock_with(latest))["passed"]
    assert clock.evidence is latest
    result = assessment(clock, delta=2_000_000_000)
    assert result["selected_proof_id"] == clock.identity(latest)
    assert result["clock_evidence_age_s"] == 2
    assert result["proof_selection_reason"] == "latest_fallback"


@pytest.mark.parametrize("low,high", [(-100, 10), (-50, 50)])
def test_narrow_shifted_or_equal_interval_does_not_replace_latest(low, high):
    older = proof(0, low, high)
    latest = replace(proof(0, -50, 50), server_ms=WALL // 1_000_000 + 1)
    result = assessment(clock_with(older, latest))
    assert result["selected_proof_id"] == BinanceSourceClock.identity(latest)


def test_selection_independent_of_quote_pass_and_one_time_pair(monkeypatch):
    old, latest = proof(60, -10, 10), proof(0, -200, 200)
    clock = clock_with(old, latest)
    calls = {"wall": 0, "mono": 0}

    def now():
        calls["mono"] += 1
        return MONO

    def wall():
        calls["wall"] += 1
        return WALL

    monkeypatch.setattr(source.time, "monotonic_ns", now)
    monkeypatch.setattr(source.time, "time_ns", wall)
    result = clock.assess(quote(0))
    assert not result["passed"] and result["selected_proof_id"] == clock.identity(old)
    assert calls == {"wall": 1, "mono": 1}
    assert assessment(clock, quote(100))["selected_proof_id"] == result["selected_proof_id"]


def test_future_old_skipped_and_invalid_latest_cannot_be_bypassed():
    future, latest = proof(-1, -10, 10), proof(0, -200, 200)
    result = assessment(clock_with(future, latest))
    assert result["selected_proof_id"] == BinanceSourceClock.identity(latest)
    clock = clock_with(proof(0, -10, 10), proof(-1, -200, 200))
    assert assessment(clock)["reason"] == "SOURCE_CLOCK_EVIDENCE_EXPIRED"
    # Even an injected out-of-order accepted proof cannot bypass latest's original TTL.
    clock = clock_with(proof(0, -10, 10), proof(121, -200, 200))
    assert assessment(clock)["reason"] == "SOURCE_CLOCK_EVIDENCE_EXPIRED"


def test_expiry_boundary_cache_limit_and_restart_unverified():
    clock = clock_with(*(proof(age, -10, 10) for age in (180, 120, 60, 0)))
    assert clock.status()["proof_cache_count"] == 3
    old = clock._proofs[0]
    assert assessment(clock)["selected_proof_id"] != clock.identity(old)
    assert assessment(clock, delta=120_000_000_001)["reason"] == "SOURCE_CLOCK_EVIDENCE_EXPIRED"
    restart = BinanceSourceClock(1500)
    assert restart.status()["proof_cache_count"] == 0
    assert assessment(restart)["reason"] == "SOURCE_CLOCK_NOT_VERIFIED"


@pytest.mark.parametrize("reason", ["http_418", "http_429", "evidence_record_failed", "global"])
def test_invalidation_clears_all_proofs(reason):
    clock = clock_with(proof(60), proof())
    clock.invalidate(reason)
    assert clock.evidence is None and not clock._proofs
    assert assessment(clock)["reason"] == "SOURCE_CLOCK_NOT_VERIFIED"


def test_unsynced_host_global_step_and_bad_quote_are_distinct():
    clock = clock_with(proof(60), proof())
    assert not clock.install(Stamp(), Stamp(), WALL // 1_000_000, {"synchronized": False})
    assert not clock._proofs
    clock = clock_with(proof(60), proof())
    result = clock.assess(quote(), now_ns=MONO, wall_ns=WALL + 26_000_000)
    assert result["global_wall_step"] and not clock._proofs
    clock = clock_with(proof(60), proof())
    result = assessment(clock, replace(quote(), receipt_ns=WALL + 26_000_000))
    assert result["quote_stamp_inconsistent"] and len(clock._proofs) == 2
    for stamp in (replace(quote(), exchange_ms=None), quote(-1000), quote(4000)):
        assert not assessment(clock, stamp)["passed"]
        assert len(clock._proofs) == 2


def test_contained_candidate_never_rejects_latest_pass_same_input():
    for old_age in (0, 60, 119, 120):
        for center in (-150, 0, 150):
            latest = proof(0, center - 200, center + 200)
            older = proof(old_age, center - 10, center + 10)
            for age_ms in range(-500, 2100, 5):
                stamp = quote(age_ms)
                baseline = assessment(clock_with(latest), stamp)
                selected = assessment(clock_with(older, latest), stamp)
                if baseline["passed"]:
                    assert selected["passed"]


def test_immutable_verified_baseline_and_single_proof_validator_equivalence():
    path = Path(__file__).parents[1] / "audits/baselines/source_time_1205d35.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == BASELINE_SHA
    spec = importlib.util.spec_from_file_location("lldesk._baseline_source_time", path)
    assert spec and spec.loader
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    for age in (0, 60, 120, 121):
        for low, high in ((-50, 50), (-260, 260), (1900, 2100)):
            evidence = proof(age, low, high)
            legacy = baseline.BinanceSourceClock(1500)
            legacy.evidence = evidence
            for age_ms in (-500, 0, 100, 1500, 4000):
                before = legacy.assess(quote(age_ms), now_ns=MONO, wall_ns=WALL)
                after = assessment(clock_with(evidence), quote(age_ms))
                for key in ("passed", "reason", "source_age_low_ms", "source_age_high_ms"):
                    assert before.get(key) == after.get(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "slow", "rate_limit", "record_failure"])
async def test_real_aiohttp_passive_phases_and_rejected_server_time(monkeypatch, mode):
    async def handle(request):
        if mode == "slow":
            await asyncio.sleep(0.42)
        return web.json_response(
            {"serverTime": source.time.time_ns() // 1_000_000},
            status=429 if mode == "rate_limit" else 200,
            headers={"Retry-After": "60"},
        )

    app = web.Application()
    app.router.add_get("/time", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(source, "TIME_URL", f"http://127.0.0.1:{port}/time")
    monkeypatch.setattr(source, "chrony_proof", lambda: HOST)
    captures = []
    recorded = asyncio.Event()

    @asynccontextmanager
    async def session(dns_mode, **kwargs):
        assert list(kwargs) == ["trace_configs"]
        async with aiohttp.ClientSession(**kwargs) as client:
            yield client

    monkeypatch.setattr(source, "binance_session", session)
    clock = clock_with(proof(60), proof())

    def record(row):
        captures.append(row)
        recorded.set()
        if mode == "record_failure":
            raise RuntimeError("controlled recorder overflow")

    worker = asyncio.create_task(clock.run("system", record))
    try:
        await asyncio.wait_for(recorded.wait(), 2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    finally:
        await runner.cleanup()
    assert clock.requests == 1 and source.CLOCK_INTERVAL_S == 60
    attempt = captures[0]["attempt"]
    assert attempt["send_monotonic_ns"] >= attempt["started_monotonic_ns"]
    phases = [r["phase"] for r in attempt["transport"]["events"]]
    assert "connection_create_start" in phases and "request_end" in phases
    assert attempt["transport"]["connection_reused"] is False
    assert attempt["transport"]["events_dropped"] == 0
    if mode in {"normal", "slow", "record_failure"}:
        assert attempt["returned_server_ms"] > 1_000_000_000_000
        assert attempt["body_received_monotonic_ns"] <= attempt["decoded_monotonic_ns"]
    if mode == "slow":
        assert not attempt["validation"]["checks"]["rtt"]
        assert clock.evidence is not None  # local calibration rejection keeps latest
    elif mode in {"record_failure", "rate_limit"}:
        assert clock.evidence is None and not clock._proofs
    else:
        assert attempt["validation"]["accepted"]


def test_bounded_clock_recorder_overflow_does_not_block(tmp_path):
    trace = WebsocketTrace(tmp_path, "clock-test", capacity=1)
    trace.event("source_clock", payload={"safe": True})
    with pytest.raises(RuntimeError, match="overflow"):
        trace.event("source_clock", payload={"safe": True})
    assert trace.dropped == 1 and trace.queue.qsize() == 1


@pytest.mark.asyncio
async def test_writer_failure_clears_clock_on_owner_loop_and_slow_disk_is_off_loop(
    tmp_path, monkeypatch
):
    import threading
    import time

    trace = WebsocketTrace(tmp_path, "clock-test")
    clock = clock_with(proof())
    started, release = threading.Event(), threading.Event()

    def slow_failed_write(handle, row):
        started.set()
        release.wait(2)
        raise OSError("controlled disk failure")

    trace.clock_failure = lambda: clock.invalidate("evidence_record_failed")
    monkeypatch.setattr(trace, "_write", slow_failed_write)
    worker = asyncio.create_task(trace.run(lambda: {}))
    trace.event("source_clock", payload={"safe": True})
    try:
        assert await asyncio.to_thread(started.wait, 1)
        before = time.monotonic()
        await asyncio.sleep(0.01)
        assert time.monotonic() - before < 0.5
        assert clock.evidence is not None
        release.set()
        for _ in range(100):
            if clock.evidence is None:
                break
            await asyncio.sleep(0.01)
        assert clock.evidence is None and not clock._proofs
        assert trace.write_errors == 1
    finally:
        release.set()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
