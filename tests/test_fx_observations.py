"""Official snapshot semantics, deadlines and independent Binance quota; no network."""

import asyncio
import time
from decimal import Decimal

import httpx
import pytest

from lldesk.fx import FxMonitor, next_start
from lldesk.models import Quote, Stamp
from lldesk.strategy import DeviationStrategy

BOOK = {
    "symbol": "USDCUSDT",
    "bidPrice": "1.00030",
    "askPrice": "1.00040",
    "bidQty": "12000",
    "askQty": "14000",
}
INFO = {
    "symbols": [
        {"symbol": "USDCUSDT", "baseAsset": "USDC", "quoteAsset": "USDT", "status": "TRADING"}
    ],
    "rateLimits": [
        {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
        {"rateLimitType": "RAW_REQUESTS", "interval": "MINUTE", "intervalNum": 5, "limit": 300000},
    ],
}


def monitor():
    value = FxMonitor()
    value.pair_verified = True
    value.limits = INFO["rateLimits"]
    return value


@pytest.mark.parametrize("qty_change", [False, True])
def test_same_price_is_a_new_verified_rest_observation(qty_change):
    value = monitor()
    original = value.verify(BOOK, Stamp(), Stamp())
    later = value.verify(BOOK | {"bidQty": "13000" if qty_change else "12000"}, Stamp(), Stamp())
    assert original.mid == later.mid
    assert later.received.monotonic_ns > original.received.monotonic_ns
    assert later.started.monotonic_ns > original.started.monotonic_ns
    assert later.price_changed_ns == original.price_changed_ns
    assert value.counts["same_price_observations"] == 1
    assert value.counts["same_price_qty_changes"] == int(qty_change)


def test_delayed_message_body_or_parser_cannot_refresh_observation():
    value = monitor()
    original = value.verify(BOOK, Stamp(), Stamp())
    delayed = Stamp(monotonic_ns=time.monotonic_ns() - 3_000_000_000)
    with pytest.raises(TimeoutError, match="deadline"):
        value.verify(BOOK, delayed, Stamp())
    assert value.latest is original and value.counts["observations"] == 1


def test_bid_ask_conversion_and_mid_residual_are_not_inverted():
    value = monitor()
    observation = value.verify(BOOK, Stamp(), Stamp())
    usdt = Decimal("85000")
    assert observation.normalized_btc(usdt, "sell") == usdt / observation.ask
    assert observation.normalized_btc(usdt, "buy") == usdt / observation.bid
    assert observation.normalized_btc(usdt, "buy") > observation.normalized_btc(usdt, "sell")
    strategy = DeviationStrategy()
    quote = Quote(usdt - 1, usdt + 1, Decimal(1), Decimal(1), Stamp())
    first = strategy.evaluate(quote, quote, True, observation.mid)
    same = value.verify(BOOK, Stamp(), Stamp())
    second = strategy.evaluate(quote, quote, True, same.mid)
    assert first.deviation_bps == second.deviation_bps == (same.mid - 1) * 10000
    count = len(strategy.samples)
    strategy.evaluate(quote, quote, False, None)
    assert len(strategy.samples) == count  # no historical gap fill


def test_fixed_start_schedule_has_no_ten_second_failure_sleep_or_catchup():
    assert next_start(100, 100.3) == 102
    assert next_start(100, 102.01) == 104
    assert next_start(100, 120.1) == 122  # skip missed starts rather than a burst


async def test_no_response_is_bounded_and_invalidates_but_does_not_retimestamp(monkeypatch):
    value = monitor()
    original = value.verify(BOOK, Stamp(), Stamp())
    monkeypatch.setattr("lldesk.fx.REQUEST_SECONDS", 0.02)

    async def hung(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(hung)) as client:
        assert await value.request(client) is None
    assert value.latest is original and not value.usable
    assert value.counts["weight"] == 2 and value.counts["failures"] == 1
    assert value.recent[-1]["error"] == "TimeoutError"


async def test_failed_snapshot_then_new_connection_verifies_identical_price():
    value = monitor()
    original = value.verify(BOOK, Stamp(), Stamp())

    async def failure(request):
        raise httpx.ReadTimeout("redacted")

    async with httpx.AsyncClient(transport=httpx.MockTransport(failure)) as client:
        assert await value.request(client) is None
    assert not value.usable and value.latest is original
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=BOOK))
    ) as new_client:
        later = await value.request(new_client)
    assert later and value.usable and later.received != original.received
    assert later.mid == original.mid and later.price_changed_ns == original.price_changed_ns
    assert value.counts["requests"] == 2 and value.counts["weight"] == 4


@pytest.mark.parametrize("fault", ["wrong_symbol", "quantity", "cache", "price"])
async def test_invalid_or_cached_snapshot_is_not_a_fresh_observation(fault):
    value = monitor()
    body = BOOK.copy()
    headers = {}
    if fault == "wrong_symbol":
        body["symbol"] = "USDTUSDC"
    elif fault == "quantity":
        body["askQty"] = "0"
    elif fault == "price":
        body["bidPrice"] = "NaN"
    else:
        headers["Age"] = "1"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body, headers=headers)
        )
    ) as client:
        assert await value.request(client) is None
    assert not value.usable and value.latest is None


async def test_official_metadata_proves_orientation_and_limits_before_quotes():
    value = FxMonitor()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=INFO))
    ) as client:
        await value.request(client, metadata=True)
    assert value.pair_verified and value.limits == INFO["rateLimits"]
    assert value.counts["weight"] == 20 and value.latest is None


async def test_429_backoff_and_provider_ip_weight_block_followup_requests():
    value = monitor()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "30"})
        )
    ) as client:
        assert await value.request(client) is None
        assert await value.request(client) is None
    assert value.counts["requests"] == 1 and value.status()["quota"]["backoff_remaining_s"] > 29
    value.blocked_until = 0
    value.ip_weight_1m, value.ip_weight_until = 5998, time.monotonic() + 60
    assert value.claim(2) and not value.claim(2)  # started request consumes last capacity


def test_binance_requests_are_bounded_in_their_own_rolling_bucket(monkeypatch):
    value = monitor()
    now = 100.0
    monkeypatch.setattr("lldesk.fx.time.monotonic", lambda: now)
    for _ in range(50):
        assert value.claim(2)
    assert not value.claim(2)
    now = 160.0
    assert value.claim(2)
    assert value.counts["requests"] == 51 and value.status()["quota"]["local_weight_60s"] == 2


def test_no_new_observations_or_heartbeat_cannot_keep_old_snapshot_fresh(monkeypatch):
    value = monitor()
    original = value.verify(BOOK, Stamp(), Stamp())
    assert value.status()["usable"]
    monkeypatch.setattr(
        "lldesk.fx.time.monotonic_ns", lambda: original.started.monotonic_ns + 16_000_000_000
    )
    assert not value.status()["usable"]
    assert value.latest is original and value.counts["observations"] == 1
