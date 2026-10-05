"""Entry/sampling source age, with an explicit exchange/local clock interval.

No clock adjustment, trading SDK, order/exit dependency, or timestamp substitution.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeGuard

import aiohttp

from .models import Stamp
from .network import DnsMode, binance_session

CLOCK_TTL_S = 120
CLOCK_INTERVAL_S = 60
MAX_UNCERTAINTY_MS = 250
MAX_HOST_ERROR_MS = 50
MAX_WALL_STEP_MS = 25
DRIFT_PPM = 100
ROUNDING_MS = 1
TIME_URL = "https://fapi.binance.com/fapi/v1/time"


def timestamp_ms(value: object) -> TypeGuard[int]:
    # USD-M uses Unix milliseconds. Seconds/microseconds/bools are not silently converted.
    return type(value) is int and 1_000_000_000_000 <= value < 10_000_000_000_000


def chrony_proof() -> dict:
    """Read-only local evidence; chronyc tracking does not change the system clock."""
    try:
        result = subprocess.run(
            ["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=1, check=True
        )
        fields = result.stdout.strip().split(",")
        error_ms = (abs(float(fields[4])) + abs(float(fields[10])) / 2 + float(fields[11])) * 1000
        reference_age = time.time() - float(fields[3])
        synchronized = (
            fields[13] == "Normal"
            and 0 < int(fields[2]) < 16
            and -1 <= reference_age <= 3600
            and math.isfinite(error_ms)
            and 0 <= error_ms <= MAX_HOST_ERROR_MS
        )
        return {
            "synchronized": synchronized,
            "host_error_ms": error_ms,
            "reference_age_s": reference_age,
            "leap_status": fields[13],
            "system_time_correction_s": float(fields[4]),
            "root_delay_s": float(fields[10]),
            "root_dispersion_s": float(fields[11]),
            "source": "chronyc -c tracking, read-only",
        }
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return {"synchronized": False, "reason": type(exc).__name__}


@dataclass(frozen=True)
class ClockEvidence:
    received: Stamp
    offset_low_ms: float
    offset_high_ms: float
    server_ms: int
    rtt_ms: float
    host: dict


class BinanceSourceClock:
    def __init__(self, max_age_ms: int):
        self.max_age_ms = max_age_ms
        self.evidence: ClockEvidence | None = None
        self.last_attempt: dict | None = None
        self.recent: deque[dict] = deque(maxlen=32)
        self.requests = 0

    def install(self, started: Stamp, received: Stamp, server_ms: object, host: dict) -> bool:
        rtt_ms = (received.monotonic_ns - started.monotonic_ns) / 1e6
        wall_elapsed_ms = (received.receipt_ns - started.receipt_ns) / 1e6
        if (
            not timestamp_ms(server_ms)
            or not host.get("synchronized")
            or not 0 <= rtt_ms <= 400
            or abs(rtt_ms - wall_elapsed_ms) > 5
            or not 0 <= host.get("host_error_ms", math.inf) <= MAX_HOST_ERROR_MS
        ):
            if not host.get("synchronized"):
                self.evidence = None
            return False
        # serverTime was generated somewhere within the successful TLS request.
        # Offset is Binance clock minus local clock; never assume it is zero.
        padding_ms = host["host_error_ms"] + ROUNDING_MS
        self.evidence = ClockEvidence(
            received,
            server_ms - received.receipt_ns / 1e6 - padding_ms,
            server_ms - started.receipt_ns / 1e6 + padding_ms,
            server_ms,
            rtt_ms,
            host,
        )
        return True

    def assess(
        self, stamp: Stamp, *, now_ns: int | None = None, wall_ns: int | None = None
    ) -> dict:
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        wall_ns = time.time_ns() if wall_ns is None else wall_ns
        receipt_age_ms = (now_ns - stamp.monotonic_ns) / 1e6
        indicator_ms = (
            stamp.receipt_ns / 1e6 - stamp.exchange_ms + receipt_age_ms
            if timestamp_ms(stamp.exchange_ms)
            else None
        )
        result: dict[str, Any] = {
            "passed": False,
            "reason": "SOURCE_TIMESTAMP_MISSING_OR_INVALID",
            "receipt_age_ms": receipt_age_ms,
            "source_indicator_ms": indicator_ms,
            "transaction_ms": stamp.exchange_ms,
            "max_source_age_ms": self.max_age_ms,
            "max_clock_uncertainty_ms": MAX_UNCERTAINTY_MS,
            "evaluated_monotonic_ns": now_ns,
        }
        if indicator_ms is None:
            return result
        evidence = self.evidence
        if evidence is None:
            result["reason"] = "SOURCE_CLOCK_NOT_VERIFIED"
            return result
        age_s = (now_ns - evidence.received.monotonic_ns) / 1e9
        result["clock_evidence_age_s"] = age_s
        if not 0 <= age_s <= CLOCK_TTL_S:
            result["reason"] = "SOURCE_CLOCK_EVIDENCE_EXPIRED"
            return result
        expected_wall = evidence.received.receipt_ns + now_ns - evidence.received.monotonic_ns
        stamp_wall = (
            evidence.received.receipt_ns + stamp.monotonic_ns - evidence.received.monotonic_ns
        )
        if (
            max(abs(wall_ns - expected_wall), abs(stamp.receipt_ns - stamp_wall))
            > MAX_WALL_STEP_MS * 1e6
        ):
            result["reason"] = "SOURCE_CLOCK_WALL_STEP_OR_INCONSISTENT_STAMP"
            return result
        drift_ms = age_s * DRIFT_PPM / 1000
        low, high = evidence.offset_low_ms - drift_ms, evidence.offset_high_ms + drift_ms
        uncertainty_ms = (high - low) / 2
        result.update(
            clock_offset_low_ms=low,
            clock_offset_high_ms=high,
            clock_uncertainty_ms=uncertainty_ms,
            source_age_low_ms=indicator_ms + low,
            source_age_high_ms=indicator_ms + high,
        )
        if uncertainty_ms > MAX_UNCERTAINTY_MS:
            result["reason"] = "SOURCE_CLOCK_TOO_UNCERTAIN"
        elif receipt_age_ms < 0 or result["source_age_high_ms"] < -ROUNDING_MS:
            result["reason"] = "SOURCE_TIMESTAMP_FUTURE"
        elif result["source_age_low_ms"] < -ROUNDING_MS:
            result["reason"] = "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN"
        elif result["source_age_high_ms"] >= self.max_age_ms:
            result["reason"] = "SOURCE_DATA_LATE"
        else:
            result.update(passed=True, reason="SOURCE_FRESH_WITH_CLOCK_BOUND")
        return result

    def status(self) -> dict:
        evidence = self.evidence
        return {
            "reference": "Binance USD-M /fapi/v1/time; server-local offset interval",
            "evidence": {
                "received_ns": evidence.received.receipt_ns,
                "received_monotonic_ns": evidence.received.monotonic_ns,
                "offset_low_ms": evidence.offset_low_ms,
                "offset_high_ms": evidence.offset_high_ms,
                "server_ms": evidence.server_ms,
                "rtt_ms": evidence.rtt_ms,
                "host": evidence.host,
            }
            if evidence
            else None,
            "ttl_seconds": CLOCK_TTL_S,
            "interval_seconds": CLOCK_INTERVAL_S,
            "drift_allowance_ppm": DRIFT_PPM,
            "request_count": self.requests,
            "futures_request_weight": self.requests,
            "lighter_request_weight": 0,
            "last_attempt": self.last_attempt,
            "recent": list(self.recent),
        }

    async def run(self, dns_mode: DnsMode, record: Callable[[dict], None]) -> None:
        # Independent worker: no order/nonce locks; no account requests; no blind retry.
        async with binance_session(dns_mode) as client:
            due = time.monotonic()
            while True:
                await asyncio.sleep(max(0, due - time.monotonic()))
                host = await asyncio.to_thread(chrony_proof)
                if not host.get("synchronized"):
                    self.evidence = None
                started = Stamp()
                self.requests += 1
                attempt = {
                    "id": self.requests,
                    "started_ns": started.receipt_ns,
                    "weight": 1,
                    "host": host,
                }
                retry_after_s = 0.0
                try:
                    async with client.get(
                        TIME_URL,
                        headers={"Cache-Control": "no-cache"},
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as response:
                        attempt.update(
                            status=response.status,
                            provider_used_weight_1m=response.headers.get("X-MBX-USED-WEIGHT-1M"),
                        )
                        if response.status in {418, 429}:
                            self.evidence = None
                            try:
                                retry_after_s = max(
                                    0, float(response.headers.get("Retry-After", "60"))
                                )
                            except ValueError:
                                retry_after_s = 60
                            attempt["retry_after_s"] = retry_after_s
                        response.raise_for_status()
                        body = await response.json()
                        received = Stamp()
                        ok = self.install(started, received, body.get("serverTime"), host)
                        attempt.update(
                            status=response.status,
                            received_ns=received.receipt_ns,
                            duration_ms=(received.monotonic_ns - started.monotonic_ns) / 1e6,
                            provider_used_weight_1m=response.headers.get("X-MBX-USED-WEIGHT-1M"),
                            outcome="verified" if ok else "clock_unverified",
                        )
                except Exception as exc:
                    attempt.update(outcome="failed", exception=type(exc).__name__, message=str(exc))
                self.last_attempt = attempt
                self.recent.append(attempt)
                try:
                    record({"attempt": attempt, "clock": self.status()["evidence"]})
                except Exception:
                    self.evidence = None
                    attempt["outcome"] = "evidence_record_failed"
                # Skip missed deadlines; never catch up with a request burst.
                due += CLOCK_INTERVAL_S
                if due < time.monotonic():
                    due = time.monotonic() + CLOCK_INTERVAL_S
                due = max(due, time.monotonic() + retry_after_s)
