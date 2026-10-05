"""Full-audit scheduling against genuine snapshot age, never a synthetic refresh."""

from __future__ import annotations

FRESHNESS_SECONDS = 15.0
FRESHNESS_MARGIN_SECONDS = 0.5
INITIAL_DURATION_BOUND_SECONDS = 2.0


class AuditScheduler:
    def __init__(self) -> None:
        self.next_due = 0.0
        self.duration_bound = INITIAL_DURATION_BOUND_SECONDS
        self.request_cost = 8
        self.reason = "bootstrap"
        self.refresh_requested = False

    def completed(self, now: float, account_age: float, duration: float, requests: int) -> None:
        # Keep the lifetime high-water mark: a fast response cannot erase evidence
        # of slow transport. A timeout is still fail-closed, not a new timestamp.
        self.duration_bound = max(self.duration_bound, duration)
        self.request_cost = max(self.request_cost, requests)
        self.next_due = now + max(
            0.0,
            FRESHNESS_SECONDS
            - max(0.0, account_age)
            - self.duration_bound
            - FRESHNESS_MARGIN_SECONDS,
        )
        self.reason = "account freshness deadline minus measured duration and 0.5s margin"

    def request_now(self, reason: str) -> None:
        self.next_due = 0.0
        self.refresh_requested = True
        self.reason = reason

    def timeout(self, account_age: float | None) -> float:
        if account_age is None:
            return 8.0
        # Stop an incomplete routine audit before the old account expires. A hung
        # exchange can still leave genuinely stale data: existing guards block it.
        return min(8.0, max(0.05, FRESHNESS_SECONDS - account_age - FRESHNESS_MARGIN_SECONDS))

    def status(self, now: float) -> dict:
        return {
            "next_due_in_s": max(0.0, self.next_due - now),
            "duration_bound_s": self.duration_bound,
            "planned_request_cost": self.request_cost,
            "freshness_limit_s": FRESHNESS_SECONDS,
            "freshness_margin_s": FRESHNESS_MARGIN_SECONDS,
            "reason": self.reason,
        }
