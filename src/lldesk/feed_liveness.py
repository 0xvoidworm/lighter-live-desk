"""Transport-only liveness: never grants source/sample/order eligibility."""

from __future__ import annotations

from .source_time import CLOCK_TTL_S, MAX_UNCERTAINTY_MS, timestamp_ms

PUBLIC_RECOVERY_COOLDOWN_S = 60


class StalePublicDetector:
    def __init__(self) -> None:
        self.generation: str | None = None
        self.previous: tuple[int, int] | None = None
        self.requested_generation: str | None = None
        self.next_allowed = 0.0

    def observe(
        self,
        source: dict | None,
        generation: str,
        receipt_ns: int,
        sequence: int,
        now: float,
        max_age_ms: int,
    ) -> bool:
        if generation != self.generation:
            self.generation, self.previous = generation, None
        source = source or {}
        latest = source.get("latest_proof") or {}
        definite = (
            source.get("reason") == "SOURCE_DATA_LATE"
            and timestamp_ms(source.get("transaction_ms"))
            and latest.get("valid") is True
            and 0 <= source.get("clock_evidence_age_s", -1) <= CLOCK_TTL_S
            and 0 <= source.get("clock_uncertainty_ms", -1) <= MAX_UNCERTAINTY_MS
            and source.get("source_age_low_ms", -1) >= max_age_ms
        )
        if not definite:
            self.previous = None
            return False
        previous = self.previous
        if previous == (receipt_ns, sequence):
            return False  # Re-evaluating the same quote is not a second depth receipt.
        self.previous = (receipt_ns, sequence)
        ordered = previous is not None and receipt_ns > previous[0] and sequence > previous[1]
        if not ordered or generation == self.requested_generation or now < self.next_allowed:
            return False
        self.requested_generation = generation
        self.next_allowed = now + PUBLIC_RECOVERY_COOLDOWN_S
        return True
