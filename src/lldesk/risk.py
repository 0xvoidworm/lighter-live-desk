from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import Config


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def limit_price(best: Decimal, side: str, tick: Decimal, slip_bps: Decimal) -> Decimal:
    raw = best * (1 + slip_bps / 10000) if side == "buy" else best * (1 - slip_bps / 10000)
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    return (raw / tick).to_integral_value(rounding=rounding) * tick


@dataclass(frozen=True)
class Sizing:
    quantity: Decimal
    price: Decimal
    notional: Decimal
    planned_risk: Decimal


def size_entry(
    config: Config,
    equity: Decimal,
    effective_exposure: Decimal,
    pending_notional: Decimal,
    price: Decimal,
    side: str,
    min_base: Decimal,
    min_quote: Decimal,
    size_decimals: int,
    price_decimals: int,
    stop_bps: Decimal,
    fee_bps: Decimal,
    slip_bps: Decimal = Decimal("2"),
) -> Sizing:
    if equity <= 0 or price <= 0 or stop_bps <= 0:
        raise ValueError("invalid equity, price or stop distance")
    tick = Decimal(1).scaleb(-price_decimals)
    step = Decimal(1).scaleb(-size_decimals)
    bounded_price = limit_price(price, side, tick, slip_bps)
    cap = min(
        config.max_entry_notional_usd,
        (equity - config.exit_reserve_usd) * config.max_effective_leverage,
        equity * config.max_effective_leverage - effective_exposure - pending_notional,
    )
    if cap <= 0:
        raise ValueError("no entry capacity after reserve and pending orders")
    per_base_loss = bounded_price * (stop_bps + 2 * fee_bps + slip_bps) / 10000
    qty = floor_step(min(cap / bounded_price, config.max_trade_risk_usd / per_base_loss), step)
    notional = qty * bounded_price
    if qty < min_base or notional < min_quote:
        raise ValueError("legal minimum order exceeds configured risk or notional cap")
    planned = qty * per_base_loss
    if notional > config.max_entry_notional_usd or planned > config.max_trade_risk_usd:
        raise AssertionError("risk rounding increased exposure")
    return Sizing(qty, bounded_price, notional, planned)


@dataclass
class EquityBaseline:
    start_equity: Decimal
    start_flow: Decimal
    day_equity: Decimal
    day_flow: Decimal
    day: str

    def total_loss(self, equity: Decimal, flow: Decimal) -> Decimal:
        return max(Decimal(0), self.start_equity + (flow - self.start_flow) - equity)

    def daily_loss(self, equity: Decimal, flow: Decimal) -> Decimal:
        return max(Decimal(0), self.day_equity + (flow - self.day_flow) - equity)

    def check(self, config: Config, equity: Decimal, flow: Decimal) -> list[str]:
        reasons = []
        if self.daily_loss(equity, flow) >= config.daily_loss_limit_usd:
            reasons.append("DAILY_LOSS")
        if self.total_loss(equity, flow) >= config.max_total_loss_usd:
            reasons.append("TOTAL_LOSS")
        return reasons
