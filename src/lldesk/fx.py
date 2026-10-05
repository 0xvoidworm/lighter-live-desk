"""Bounded official Spot bid/ask observations; a separate Binance quota lane."""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal

import httpx

from .models import Stamp

FX_BOOK = "https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=USDCUSDT"
FX_INFO = "https://data-api.binance.vision/api/v3/exchangeInfo?symbol=USDCUSDT"
PERIOD_SECONDS = 2.0
REQUEST_SECONDS = 2.0
FRESH_SECONDS = 15.0


def next_start(previous: float, now: float) -> float:
    due = previous + PERIOD_SECONDS
    while due < now:
        due += PERIOD_SECONDS
    return due


@dataclass(frozen=True)
class FxObservation:
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    started: Stamp
    received: Stamp
    verified_ns: int
    price_changed_ns: int

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    def normalized_btc(self, usdt_price: Decimal, side: str) -> Decimal:
        # USDCUSDT is USDT per USDC. Sell BTC: buy USDC at FX ask;
        # buy BTC: sell USDC at FX bid to fund its USDT cost.
        if side not in {"buy", "sell"}:
            raise ValueError("BTC side required")
        return usdt_price / (self.bid if side == "buy" else self.ask)


class FxMonitor:
    def __init__(self) -> None:
        self.latest: FxObservation | None = None
        self.usable = False
        self.pair_verified = False
        self.limits: list[dict] = []
        self.calls: deque[tuple[float, int]] = deque()
        self.recent: deque[dict] = deque(maxlen=32)
        self.active: dict | None = None
        self.counts: Counter = Counter()
        self.blocked_until = 0.0
        self.ip_weight_1m: int | None = None
        self.ip_weight_until = 0.0

    def claim(self, weight: int) -> bool:
        now = time.monotonic()
        while self.calls and now - self.calls[0][0] >= 300:
            self.calls.popleft()
        if now < self.blocked_until:
            return False
        # At most 100 weight/rolling minute, independently of Lighter's 40/55.
        if sum(w for at, w in self.calls if now - at < 60) + weight > 100:
            return False
        for limit in self.limits:
            if limit["rateLimitType"] not in {"REQUEST_WEIGHT", "RAW_REQUESTS"}:
                continue
            window = {"SECOND": 1, "MINUTE": 60, "DAY": 86400}[limit["interval"]]
            window *= limit["intervalNum"]
            # This worker has <50 requests/min; retain all relevant local calls.
            if window > 300:
                continue
            relevant = [w for at, w in self.calls if now - at < window]
            used = sum(relevant) if limit["rateLimitType"] == "REQUEST_WEIGHT" else len(relevant)
            cost = weight if limit["rateLimitType"] == "REQUEST_WEIGHT" else 1
            if used + cost > limit["limit"]:
                return False
            if (
                window == 60
                and limit["rateLimitType"] == "REQUEST_WEIGHT"
                and self.ip_weight_1m is not None
                and now < self.ip_weight_until
                and self.ip_weight_1m + weight > limit["limit"]
            ):
                return False
        self.calls.append((now, weight))
        if self.ip_weight_1m is not None and now < self.ip_weight_until:
            self.ip_weight_1m += weight  # failed/in-flight attempts are not free
        self.counts["requests"] += 1
        self.counts["weight"] += weight
        return True

    def verify(self, data: dict, started: Stamp, received: Stamp) -> FxObservation:
        if not self.pair_verified or data.get("symbol") != "USDCUSDT":
            raise ValueError("unverified FX pair")
        values = [Decimal(str(data[k])) for k in ("bidPrice", "askPrice", "bidQty", "askQty")]
        bid, ask, bid_qty, ask_qty = values
        if not all(v.is_finite() and v > 0 for v in values) or ask <= bid:
            raise ValueError("invalid FX book")
        verified = time.monotonic_ns()
        # Include transport, body, parsing and delayed event-loop processing.
        if (verified - started.monotonic_ns) / 1e9 >= REQUEST_SECONDS:
            raise TimeoutError("FX observation exceeded request deadline")
        changed = self.latest is None or (bid, ask) != (self.latest.bid, self.latest.ask)
        self.counts["observations"] += 1
        self.counts["price_changes" if changed else "same_price_observations"] += 1
        if (
            self.latest
            and not changed
            and (bid_qty, ask_qty)
            != (
                self.latest.bid_qty,
                self.latest.ask_qty,
            )
        ):
            self.counts["same_price_qty_changes"] += 1
        observation = FxObservation(
            bid,
            ask,
            bid_qty,
            ask_qty,
            started,
            received,
            verified,
            self.latest.price_changed_ns if self.latest and not changed else received.receipt_ns,
        )
        self.latest, self.usable = observation, True
        return observation

    async def request(
        self, client: httpx.AsyncClient, *, metadata: bool = False
    ) -> FxObservation | None:
        weight = 20 if metadata else 2
        if not self.claim(weight):
            self.counts["quota_backoff"] += 1
            self.usable = False
            return None
        started = Stamp()
        record: dict = {
            "id": self.counts["requests"],
            "path": "/api/v3/exchangeInfo" if metadata else "/api/v3/ticker/bookTicker",
            "source": "Binance Spot REST Memory",
            "started_at": started.receipt_ns / 1e9,
            "weight": weight,
            "last_phase": "pool_or_transport",
            "phase_ms": {},
        }
        self.active = record
        phase_at = time.monotonic()

        async def trace(name: str, info: dict) -> None:
            nonlocal phase_at
            # Only event names/timing. Never persist headers, URLs or exception text.
            if name.endswith(".failed"):
                record["failure_phase"] = name.removesuffix(".failed")
            if name.endswith(".started"):
                now = time.monotonic()
                phase = record["last_phase"]
                record["phase_ms"][phase] = (now - phase_at) * 1000
                record["last_phase"] = name.removesuffix(".started")
                phase_at = now
                if name.endswith("connect_tcp.started"):
                    self.counts["tcp_connections"] += 1

        observation = None
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                response = await client.get(
                    FX_INFO if metadata else FX_BOOK, extensions={"trace": trace}
                )
                received = Stamp()
                record["received_at"] = received.receipt_ns / 1e9
                record["status"] = response.status_code
                header = response.headers.get("x-mbx-used-weight-1m")
                if header and header.isdigit():
                    self.ip_weight_1m = int(header)
                    self.ip_weight_until = time.monotonic() + 60
                record["provider_ip_weight_1m"] = self.ip_weight_1m
                if response.status_code in {418, 429}:
                    retry = response.headers.get("retry-after", "60")
                    seconds = float(retry) if retry.isdigit() else 60.0
                    self.blocked_until = time.monotonic() + max(1, seconds)
                    record["retry_after_s"] = max(1, seconds)
                response.raise_for_status()
                if float(response.headers.get("age", "0")) > 0:
                    raise ValueError("cached FX response cannot verify a new observation")
                record["phase_ms"][record["last_phase"]] = (time.monotonic() - phase_at) * 1000
                record["last_phase"] = "parsing_validation"
                phase_at = time.monotonic()
                data = response.json()
                if metadata:
                    pair = data["symbols"][0]
                    if (pair["symbol"], pair["baseAsset"], pair["quoteAsset"], pair["status"]) != (
                        "USDCUSDT",
                        "USDC",
                        "USDT",
                        "TRADING",
                    ):
                        raise ValueError("wrong FX pair orientation/status")
                    self.limits = data["rateLimits"]
                    if not any(
                        limit["rateLimitType"] == "REQUEST_WEIGHT"
                        and limit["interval"] == "MINUTE"
                        and limit["intervalNum"] == 1
                        for limit in self.limits
                    ):
                        raise ValueError("missing official Binance weight limit")
                    self.pair_verified = True
                    record["outcome"] = "metadata_verified"
                else:
                    observation = self.verify(data, started, received)
                    record["outcome"] = "verified"
        except asyncio.CancelledError:
            record["outcome"] = "canceled"
            self.usable = False
            raise
        except Exception as error:
            self.usable = False
            self.counts["failures"] += 1
            record["outcome"] = "failed"
            record["error"] = type(error).__name__
            record.setdefault("failure_phase", record["last_phase"])
        finally:
            record["phase_ms"][record["last_phase"]] = (time.monotonic() - phase_at) * 1000
            record["elapsed_ms"] = (time.monotonic_ns() - started.monotonic_ns) / 1e6
            record["finished_at"] = time.time()
            self.recent.append(record)
            self.active = None
        return observation

    def status(self) -> dict:
        observation = self.latest
        now = time.monotonic()
        return {
            "source": FX_BOOK,
            "symbol": "USDCUSDT",
            "orientation": "USDT per USDC",
            "pair_verified": self.pair_verified,
            "period_seconds": PERIOD_SECONDS,
            "request_deadline_seconds": REQUEST_SECONDS,
            "freshness_seconds": FRESH_SECONDS,
            "usable": bool(
                self.usable
                and observation
                and (time.monotonic_ns() - observation.started.monotonic_ns) / 1e9 < FRESH_SECONDS
            ),
            "counts": dict(self.counts),
            "quota": {
                "provider": "Binance Spot; separate from Lighter",
                "local_weight_cap_60s": 100,
                "local_weight_60s": sum(w for at, w in self.calls if now - at < 60),
                "provider_ip_weight_1m": self.ip_weight_1m,
                "limits": self.limits,
                "backoff_remaining_s": max(0, self.blocked_until - now),
            },
            "observation": {
                "bid": str(observation.bid),
                "ask": str(observation.ask),
                "bid_qty": str(observation.bid_qty),
                "ask_qty": str(observation.ask_qty),
                "request_started_ns": observation.started.receipt_ns,
                "received_ns": observation.received.receipt_ns,
                "received_monotonic_ns": observation.received.monotonic_ns,
                "verified_monotonic_ns": observation.verified_ns,
                "price_changed_ns": observation.price_changed_ns,
                "exchange_ms": None,
                "update_id": None,
                "observation_age_s": (time.monotonic_ns() - observation.received.monotonic_ns)
                / 1e9,
                "conservative_age_s": (time.monotonic_ns() - observation.started.monotonic_ns)
                / 1e9,
            }
            if observation
            else None,
            "active": self.active,
            "recent": list(self.recent),
        }
