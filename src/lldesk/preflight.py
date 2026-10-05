"""Process-local, one-attempt audit evidence; never an order authorization."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from .account import AccountSnapshot

HISTORY_IDLE_SECONDS = 120.0
PREFLIGHT_TTL_SECONDS = 5.0


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def state_digest(snap: AccountSnapshot) -> str:
    return digest(
        {
            "equity": snap.equity,
            "available": snap.available,
            "collateral": snap.collateral,
            "position": snap.position_qty,
            "orders": snap.active_orders,
            "order_count": snap.exchange_order_count,
            "pending": snap.exchange_pending_count,
            "transaction_time": snap.transaction_time,
            "other_positions": snap.other_positions,
            "assets": snap.other_assets,
            "tier": snap.account_type_code,
            "mode": snap.account_trading_mode,
            "margin": snap.margin_mode,
            "initial_margin": snap.initial_margin_fraction,
        }
    )


def component_ages(snap: AccountSnapshot | None) -> dict[str, float | None]:
    now = time.monotonic()
    return {
        "account": max(0.0, now - snap.received_monotonic)
        if snap and snap.received_monotonic
        else None,
        "active_orders": max(0.0, now - snap.orders_received_monotonic)
        if snap and snap.orders_received_monotonic
        else None,
    }


def state_complete(snap: AccountSnapshot | None) -> bool:
    ages = component_ages(snap)
    return bool(
        snap
        and time.time() - snap.received_at < 15
        and time.time() - snap.orders_received_at < 15
        and all(age is not None and age < 15 for age in ages.values())
        and snap.collateral is not None
        and snap.exchange_order_count is not None
        and snap.exchange_pending_count is not None
        and snap.exchange_order_count == len(snap.active_orders)
        and snap.exchange_pending_count
        == sum(str(o.get("status")) == "pending" for o in snap.active_orders)
    )


@dataclass(frozen=True)
class Evidence:
    attempt: str
    generation: int
    binding: str
    issued: float
    expires: float
    sendable: bool

    def valid(self, attempt: str, generation: int, binding: str, *, sending: bool) -> bool:
        return bool(
            attempt == self.attempt
            and generation == self.generation
            and binding == self.binding
            and time.monotonic() < self.expires
            and (not sending or self.sendable)
        )
