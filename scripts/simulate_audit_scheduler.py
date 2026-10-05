"""Replay sanitized real timings through the production scheduler/budget; no I/O to exchanges."""

import collections
import json
from pathlib import Path

import lldesk.account as account
from lldesk.audit_scheduler import AuditScheduler


def main():
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/audit-eight-get-trace-20261005.json"
    rows = json.loads(fixture.read_text())["records"]
    original_clock = account.time.monotonic
    results = {}
    try:
        for mode in ["old", "deadline"]:
            now = 0.0
            account.time.monotonic = lambda: now  # noqa: B023 - synchronous replay, restored below
            rate = account.RateBudget()
            scheduler = AuditScheduler()
            starts, cycles = [], []
            next_due = 0.0
            prior_account = None
            prior_complete = None
            for i in range(200):
                now = max(now, next_due)
                if now >= 1200:
                    break
                while not rate.can_claim("reconcile", 8):
                    now = rate.calls[0] + 60.000001
                start = now
                row = rows[i % len(rows)]
                ordering = range(8) if mode == "old" or i == 0 else [2, 3, 4, 5, 6, 1, 0, 7]
                with rate.reserve("reconcile", 8):
                    for index in ordering:
                        if now < 1200:
                            starts.append(now)
                        rate.claim("reconcile")
                        now += row["request_durations_s"][index]
                        if index == 0:
                            received = now
                        if index == 6:
                            snapshot_complete = now
                    now = max(now, start + row["duration_s"])
                cycles.append(
                    {
                        "start": start,
                        "complete": now,
                        "account_received": received,
                        "age_before_publication": None
                        if prior_account is None
                        else now - prior_account,
                        "audit_age_before_publication": None
                        if prior_complete is None
                        else now - prior_complete,
                    }
                )
                prior_account, prior_complete = received, now
                scheduler.completed(now, now - received, now - start, 8)
                next_due = snapshot_complete + 12 if mode == "old" else scheduler.next_due
            pending = collections.deque()
            j = 0
            blocked = 0
            max_used = 0
            for tick in range(24000):
                t = tick * 0.05
                while j < len(starts) and starts[j] <= t:
                    pending.append(starts[j])
                    j += 1
                while pending and t - pending[0] >= 60:
                    pending.popleft()
                max_used = max(max_used, len(pending))
                blocked += len(pending) > 33
            ages = [
                r["age_before_publication"]
                for r in cycles
                if r["age_before_publication"] is not None
            ]
            results[mode] = {
                "cycles": len(cycles),
                "requests_started": len(starts),
                "mean_gets_per_minute": len(starts) / 20,
                "rolling_peak_at_20Hz": max_used,
                "quota_blocked_s_at_20Hz": blocked * 0.05,
                "maximum_account_age_before_publication_s": max(ages),
                "freshness_violations": sum(age >= 15 for age in ages),
                "minimum_exit_slots_before_new_position": 55 - max_used,
            }
    finally:
        account.time.monotonic = original_clock
    result = {
        "source": str(fixture.relative_to(root)),
        "simulation_only": True,
        "model": (
            "Serial full eight-GET audits; true account-response timestamps; "
            "all request starts count before awaiting; 20Hz quota sampling; "
            "no network/signing/orders"
        ),
        "deadline_bound_s": 2,
        "freshness_margin_s": 0.5,
        "results": results,
        "limitations": (
            "Replays 93 observed successes, not a latency guarantee; longer pagination/retries "
            "or faults must fail closed. Unconsumed reservations affect live in-flight checks "
            "and are separately measured at runtime; these quota durations count claims only."
        ),
    }
    path = root / "audits/audit-scheduler-simulation-20261005.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
