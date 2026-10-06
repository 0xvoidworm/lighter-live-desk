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

from .clock_trace import calibration_request, clock_trace, transport_record
from .models import Stamp
from .network import DnsMode, binance_session

CLOCK_TTL_S = 120
CLOCK_INTERVAL_S = 60
# 60s cadence + unchanged 2s request budget + 13s scheduling/idle headroom.
# Idle retention is transport policy, never an extension of DNS or clock evidence TTL.
CALIBRATION_KEEPALIVE_S = CLOCK_INTERVAL_S + 15
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
        self._latest: ClockEvidence | None = None
        self._proofs: deque[ClockEvidence] = deque(maxlen=3)
        self.last_assessment: dict | None = None
        self.last_validation: dict | None = None
        self.last_invalidation: dict | None = None
        self.last_attempt: dict | None = None
        self.recent: deque[dict] = deque(maxlen=32)
        self.requests = 0
        self.request_weight = 0

    @property
    def evidence(self) -> ClockEvidence | None:
        """Latest accepted fallback, never the proof selected for a particular quote."""
        return self._latest

    @evidence.setter
    def evidence(self, value: ClockEvidence | None) -> None:
        if value is None:
            self.invalidate("evidence_cleared")
        else:
            self._latest = value
            self._proofs.append(value)

    def invalidate(self, reason: str) -> None:
        self._latest = None
        self._proofs.clear()
        self.last_assessment = None
        self.last_invalidation = {"reason": reason, "monotonic_ns": time.monotonic_ns()}

    @staticmethod
    def identity(evidence: ClockEvidence) -> str:
        return f"{evidence.server_ms}:{evidence.received.monotonic_ns}"

    def install(self, started: Stamp, received: Stamp, server_ms: object, host: dict) -> bool:
        rtt_ms = (received.monotonic_ns - started.monotonic_ns) / 1e6
        wall_elapsed_ms = (received.receipt_ns - started.receipt_ns) / 1e6
        checks = {
            "server_timestamp": timestamp_ms(server_ms),
            "host_synchronized": bool(host.get("synchronized")),
            "rtt": 0 <= rtt_ms <= 400,
            "wall_elapsed": abs(rtt_ms - wall_elapsed_ms) <= 5,
            "host_error": 0 <= host.get("host_error_ms", math.inf) <= MAX_HOST_ERROR_MS,
        }
        self.last_validation = {
            "accepted": all(checks.values()),
            "checks": checks,
            "rtt_ms": rtt_ms,
            "wall_elapsed_ms": wall_elapsed_ms,
        }
        if not all(checks.values()):
            if not host.get("synchronized"):
                self.invalidate("host_unsynchronized")
            return False
        assert timestamp_ms(server_ms)
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

    def _bounds(self, evidence: ClockEvidence, stamp: Stamp, now: int, wall: int) -> dict:
        """Clock validity only; never chooses a proof by whether the quote passes."""
        age = (now - evidence.received.monotonic_ns) / 1e9
        result = {
            "clock_proof_id": self.identity(evidence),
            "clock_received_monotonic_ns": evidence.received.monotonic_ns,
            "clock_received_wall_ns": evidence.received.receipt_ns,
            "clock_base_offset_low_ms": evidence.offset_low_ms,
            "clock_base_offset_high_ms": evidence.offset_high_ms,
            "clock_evidence_age_s": age,
            "valid": False,
        }
        if not evidence.host.get("synchronized"):
            return result | {"reason": "SOURCE_CLOCK_HOST_UNSYNCHRONIZED"}
        if not 0 <= age <= CLOCK_TTL_S:
            return result | {"reason": "SOURCE_CLOCK_EVIDENCE_EXPIRED"}
        expected_wall = evidence.received.receipt_ns + now - evidence.received.monotonic_ns
        stamp_wall = (
            evidence.received.receipt_ns + stamp.monotonic_ns - evidence.received.monotonic_ns
        )
        global_step = abs(wall - expected_wall) > MAX_WALL_STEP_MS * 1e6
        quote_inconsistent = abs(stamp.receipt_ns - stamp_wall) > MAX_WALL_STEP_MS * 1e6
        result.update(global_wall_step=global_step, quote_stamp_inconsistent=quote_inconsistent)
        if global_step or quote_inconsistent:
            return result | {"reason": "SOURCE_CLOCK_WALL_STEP_OR_INCONSISTENT_STAMP"}
        drift = age * DRIFT_PPM / 1000
        low, high = evidence.offset_low_ms - drift, evidence.offset_high_ms + drift
        uncertainty = (high - low) / 2
        result.update(
            clock_drift_allowance_ms=drift,
            clock_offset_low_ms=low,
            clock_offset_high_ms=high,
            clock_uncertainty_ms=uncertainty,
        )
        if uncertainty > MAX_UNCERTAINTY_MS:
            return result | {"reason": "SOURCE_CLOCK_TOO_UNCERTAIN"}
        return result | {"valid": True}

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
            "evaluated_wall_ns": wall_ns,
            "clock_ttl_seconds": CLOCK_TTL_S,
        }
        evidence = self.evidence
        if evidence is None:
            if indicator_ms is not None:
                result["reason"] = "SOURCE_CLOCK_NOT_VERIFIED"
            return self._remember(result)
        latest = self._bounds(evidence, stamp, now_ns, wall_ns)
        selected = latest
        selection = "latest_fallback"
        if latest["valid"]:
            for candidate in reversed(self._proofs):
                bounds = self._bounds(candidate, stamp, now_ns, wall_ns)
                if (
                    bounds["valid"]
                    and 0 <= candidate.host.get("host_error_ms", math.inf) <= MAX_HOST_ERROR_MS
                    and bounds["clock_offset_low_ms"] >= latest["clock_offset_low_ms"]
                    and bounds["clock_offset_high_ms"] <= latest["clock_offset_high_ms"]
                    and bounds["clock_uncertainty_ms"] < selected["clock_uncertainty_ms"]
                ):
                    selected = bounds
                    selection = "contained_valid_older_interval"
        result.update({k: v for k, v in selected.items() if k not in {"valid", "reason"}})
        result.update(
            latest_proof=latest,
            latest_proof_id=latest["clock_proof_id"],
            selected_proof_id=selected["clock_proof_id"],
            proof_selection_reason=selection,
        )
        if latest.get("global_wall_step") or latest.get("reason") == (
            "SOURCE_CLOCK_HOST_UNSYNCHRONIZED"
        ):
            self.invalidate(latest["reason"])
        if indicator_ms is None:
            return self._remember(result)
        if "clock_offset_low_ms" in selected:
            result.update(
                source_age_low_ms=indicator_ms + selected["clock_offset_low_ms"],
                source_age_high_ms=indicator_ms + selected["clock_offset_high_ms"],
            )
        if not latest["valid"]:
            result["reason"] = latest["reason"]
            return self._remember(result)
        if receipt_age_ms < 0 or result["source_age_high_ms"] < -ROUNDING_MS:
            result["reason"] = "SOURCE_TIMESTAMP_FUTURE"
        elif result["source_age_low_ms"] < -ROUNDING_MS:
            result["reason"] = "SOURCE_TIMESTAMP_FUTURE_UNCERTAIN"
        elif result["source_age_high_ms"] >= self.max_age_ms:
            result["reason"] = "SOURCE_DATA_LATE"
        else:
            result.update(passed=True, reason="SOURCE_FRESH_WITH_CLOCK_BOUND")
        return self._remember(result)

    def _remember(self, result: dict) -> dict:
        # Evidence only; entry/dispatch always calls assess again with a new time pair.
        self.last_assessment = result
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
            "futures_request_weight": self.request_weight,
            "request_count_definition": "scheduled calibrations; sends counted by headers hook",
            "keepalive_timeout_seconds": CALIBRATION_KEEPALIVE_S,
            "lighter_request_weight": 0,
            "proof_cache_count": len(self._proofs),
            "proof_cache_limit": 3,
            "proof_cache_persisted": False,
            "last_assessment": self.last_assessment,
            "last_validation": self.last_validation,
            "last_invalidation": self.last_invalidation,
            "last_attempt": self.last_attempt,
            "recent": list(self.recent),
        }

    async def run(self, dns_mode: DnsMode, record: Callable[[dict], None]) -> None:
        # Independent worker: no order/nonce locks; no account requests; no blind retry.
        async with binance_session(
            dns_mode,
            trace_configs=[clock_trace()],
            keepalive_timeout=CALIBRATION_KEEPALIVE_S,
        ) as client:
            due = time.monotonic()
            while True:
                await asyncio.sleep(max(0, due - time.monotonic()))
                woke_ns = time.monotonic_ns()
                host = await asyncio.to_thread(chrony_proof)
                if not host.get("synchronized"):
                    self.invalidate("host_unsynchronized")
                started = Stamp()
                self.requests += 1
                attempt: dict[str, Any] = {
                    "id": self.requests,
                    "started_ns": started.receipt_ns,
                    "weight": 1,
                    "host": host,
                    "scheduled_monotonic_ns": int(due * 1e9),
                    "woke_monotonic_ns": woke_ns,
                    "started_monotonic_ns": started.monotonic_ns,
                    "send_monotonic_ns": time.monotonic_ns(),
                    "send_marker": "request invocation, not wire arrival; headers hook separately",
                    "stage": "request_headers",
                    "transport": transport_record(),
                }
                retry_after_s = 0.0
                cancelled = False
                token = calibration_request.set(attempt)
                try:
                    async with client.get(
                        TIME_URL,
                        headers={"Cache-Control": "no-cache"},
                        timeout=aiohttp.ClientTimeout(total=2),
                        trace_request_ctx=attempt,
                    ) as response:
                        attempt.update(
                            headers_monotonic_ns=time.monotonic_ns(),
                            status=response.status,
                            provider_used_weight_1m=response.headers.get("X-MBX-USED-WEIGHT-1M"),
                        )
                        if response.status in {418, 429}:
                            self.invalidate(f"http_{response.status}")
                            try:
                                retry_after_s = max(
                                    0, float(response.headers.get("Retry-After", "60"))
                                )
                            except ValueError:
                                retry_after_s = 60
                            attempt["retry_after_s"] = retry_after_s
                        response.raise_for_status()
                        attempt["stage"] = "body"
                        attempt["body_started_monotonic_ns"] = time.monotonic_ns()
                        # read once; native json() retains its content-type/decode semantics.
                        await response.read()
                        attempt["body_received_monotonic_ns"] = time.monotonic_ns()
                        attempt["stage"] = "decode"
                        body = await response.json()
                        attempt["decoded_monotonic_ns"] = time.monotonic_ns()
                        received = Stamp()
                        server_ms = body.get("serverTime")
                        attempt["returned_server_ms"] = server_ms
                        attempt["stage"] = "validation"
                        ok = self.install(started, received, server_ms, host)
                        attempt.update(
                            received_monotonic_ns=received.monotonic_ns,
                            validation=self.last_validation,
                            status=response.status,
                            received_ns=received.receipt_ns,
                            duration_ms=(received.monotonic_ns - started.monotonic_ns) / 1e6,
                            provider_used_weight_1m=response.headers.get("X-MBX-USED-WEIGHT-1M"),
                            outcome="verified" if ok else "clock_unverified",
                        )
                except asyncio.CancelledError:
                    cancelled = True
                    attempt.update(outcome="cancelled", failed_stage=attempt["stage"])
                except Exception as exc:
                    attempt.update(
                        outcome="failed",
                        exception=type(exc).__name__,
                        message=str(exc),
                        failed_stage=attempt["stage"],
                    )
                finally:
                    calibration_request.reset(token)
                attempt["finished_monotonic_ns"] = time.monotonic_ns()
                sends = attempt["transport"]["http_send_attempts"]
                attempt["http_attempts"] = sends
                attempt["http_retries"] = max(0, sends - 1)
                # Conservatively budget one even if failure occurs before headers;
                # an automatic idempotent retry consumes another provider request.
                attempt["weight"] = max(1, sends)
                self.request_weight += attempt["weight"]
                attempt["latest_proof_id"] = self.identity(self.evidence) if self.evidence else None
                attempt["selected_assessment"] = self.last_assessment
                self.last_attempt = attempt
                self.recent.append(attempt)
                try:
                    record({"attempt": attempt, "clock": self.status()["evidence"]})
                except Exception:
                    self.invalidate("evidence_record_failed")
                    attempt["outcome"] = "evidence_record_failed"
                if cancelled:
                    raise asyncio.CancelledError
                # Skip missed deadlines; never catch up with a request burst.
                due += CLOCK_INTERVAL_S
                if due < time.monotonic():
                    due = time.monotonic() + CLOCK_INTERVAL_S
                due = max(due, time.monotonic() + retry_after_s)
