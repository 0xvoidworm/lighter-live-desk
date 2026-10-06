"""The diagnostic client/replay never imports engine/account/signer or sends REST."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import pytest

from scripts.analyze_depth_comparator import CausalClock, annotate, compare, identity, rows
from scripts.record_depth_comparator import record

ANCHOR_WALL = 1_791_200_000_000_000_000
ANCHOR_MONO = 100_000_000_000


def proof_event(after_s=0, *, clock=True):
    return {
        "monotonic_ns": ANCHOR_MONO + round(after_s * 1e9),
        "kind": "source_clock",
        "payload": {
            "clock": {
                "received_ns": ANCHOR_WALL + round(after_s * 1e9),
                "received_monotonic_ns": ANCHOR_MONO + round(after_s * 1e9),
                "offset_low_ms": -10,
                "offset_high_ms": 10,
                "server_ms": ANCHOR_WALL // 1_000_000 + round(after_s * 1000),
                "rtt_ms": 10,
                "host": {"synchronized": True},
            }
            if clock
            else None
        },
    }


def update(at_s, age_ms=100, *, u=2, extra_ms=0):
    source = ANCHOR_WALL // 1_000_000 + round(at_s * 1000) - age_ms
    return {
        "receipt_ns": ANCHOR_WALL + round(at_s * 1e9) + round(extra_ms * 1e6),
        "receipt_monotonic_ns": ANCHOR_MONO + round(at_s * 1e9) + round(extra_ms * 1e6),
        "payload": {
            "e": "depthUpdate",
            "s": "BTCUSDT",
            "U": u,
            "u": u,
            "pu": u - 1,
            "E": source + 1,
            "T": source,
            "b": [["100", "1"]],
            "a": [],
        },
    }


def test_causal_proof_never_uses_future_and_expired_cannot_prove_late():
    clock = CausalClock([proof_event(2)])
    assert not clock.assess(update(1))["passed"]
    assert clock.assess(update(3))["passed"]
    row = annotate([update(125, 5000)], [proof_event(2)])[0]
    assert row["assessment"]["reason"] == "SOURCE_CLOCK_EVIDENCE_EXPIRED"
    assert not row["certain_late"]


def test_global_invalidation_missing_and_future_remain_unknown():
    clock = CausalClock([proof_event(), proof_event(2, clock=False)])
    assert clock.assess(update(1))["passed"]
    assert not clock.assess(update(3))["passed"]
    missing = update(1)
    del missing["payload"]["T"]
    assert not annotate([missing], [proof_event()])[0]["certain_late"]
    unknown, _ = compare([missing], [missing], [proof_event()])
    assert unknown["categories"] == {"uncertain_or_guard_rejected": 1}
    assert not annotate([update(1, -100)], [proof_event()])[0]["assessment"]["passed"]


@pytest.mark.parametrize(
    "prod_age,comp_age,category",
    [
        (5000, 5000, "both_certain_late"),
        (5000, 100, "production_only_certain_late"),
        (100, 5000, "comparator_only_certain_late"),
        (100, 100, "both_source_valid"),
    ],
)
def test_identical_update_classification(prod_age, comp_age, category):
    # Identical payloads, different receipt times, on the same host monotonic domain.
    a = update(6, prod_age)
    b = a | {
        "receipt_ns": a["receipt_ns"] + round((comp_age - prod_age) * 1e6),
        "receipt_monotonic_ns": a["receipt_monotonic_ns"] + round((comp_age - prod_age) * 1e6),
    }
    summary, pairs = compare([a], [b], [proof_event()])
    assert summary["categories"] == {category: 1}
    assert pairs[0]["production_minus_comparator_ms"] == prod_age - comp_age


def test_payload_mismatch_and_duplicate_never_arbitrarily_pair():
    a = update(1)
    b = a | {"payload": a["payload"] | {"b": [["100", "2"]]}}
    assert identity(a["payload"]) != identity(b["payload"])
    mismatch, _ = compare([a], [b], [proof_event()])
    assert mismatch["pairs"] == 0 and mismatch["identity_payload_mismatch_keys"] == 1
    summary, _ = compare([a, a], [a], [proof_event()])
    assert summary["pairs"] == 0 and summary["ambiguous_common_keys"] == 1


@pytest.mark.asyncio
async def test_one_connection_deadline_closes_and_no_rest(tmp_path):
    calls = []
    closed = []

    class Socket:
        _writer = SimpleNamespace(transport=None)
        delivered = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.delivered:
                await asyncio.Future()
            self.delivered = True
            return SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT, data=json.dumps({"data": update(1)["payload"]})
            )

    @asynccontextmanager
    async def factory(mode, **kwargs):
        calls.append((mode, kwargs))

        def connect(url, **options):
            calls.append((url, options))
            return Socket()

        yield SimpleNamespace(ws_connect=connect)  # No get/request/order method exists.

    result = await record(tmp_path / "capture", 0.03, "doh", session_factory=factory)
    assert result["messages"] == 1 and result["outcome"] == "window_complete"
    assert len(closed) == 1 and len(calls) == 2
    assert calls[1][1] == {"heartbeat": 20}
    assert result["telemetry"]["events_dropped"] == 0
    logged = list(rows(next((tmp_path / "capture").glob("*.gz"))))
    assert sum(r["kind"] == "comparison_depth" for r in logged) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError, TimeoutError])
async def test_failure_never_reconnects(tmp_path, failure):
    calls = []

    @asynccontextmanager
    async def factory(mode, **kwargs):
        calls.append(mode)
        raise failure("controlled connection failure")
        yield  # pragma: no cover

    result = await record(tmp_path / "failed", 0.03, "doh", session_factory=factory)
    assert result["outcome"] == "connection_failed" and calls == ["doh"]
