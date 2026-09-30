from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from time import monotonic_ns, time_ns


class EngineState(StrEnum):
    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    WARMING_UP = "WARMING_UP"
    READY = "READY"
    LIVE = "LIVE"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    HALTED = "HALTED"


class OrderState(StrEnum):
    INTENT = "INTENT"
    SENT = "SENT"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Stamp:
    exchange_ms: int | None = None
    receipt_ns: int = field(default_factory=time_ns)
    monotonic_ns: int = field(default_factory=monotonic_ns)


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    stamp: Stamp

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class MarketEvent:
    venue: str
    kind: str
    payload: dict
    stamp: Stamp


@dataclass(frozen=True)
class AccountEvent:
    account_index: int
    payload: dict
    stamp: Stamp


@dataclass(frozen=True)
class SignalEvent:
    side: str | None
    deviation_bps: Decimal
    reasons: tuple[str, ...]
    stamp: Stamp


@dataclass(frozen=True)
class OrderIntent:
    client_id: int
    kind: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    stamp: Stamp


@dataclass(frozen=True)
class Acknowledgement:
    client_id: int
    tx_hash: str | None
    accepted: bool | None
    stamp: Stamp


@dataclass(frozen=True)
class Fill:
    fill_id: str
    client_id: int
    quantity: Decimal
    price: Decimal
    fee: Decimal
    stamp: Stamp


@dataclass(frozen=True)
class Position:
    quantity: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    funding: Decimal
    stamp: Stamp


@dataclass(frozen=True)
class RiskEvent:
    code: str
    detail: str
    stamp: Stamp
