from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mode: str = "observe"
    binance_dns_mode: Literal["system", "doh"] = "system"
    account_index: int | None = None
    expected_account_type_code: int | None = None
    expected_account_trading_mode: int | None = None
    expected_margin_mode: int | None = None
    expected_initial_margin_fraction: Decimal | None = None
    account_type: str = "standard"
    market_symbol: str = "BTC"
    max_funding_usd: Decimal = Decimal("50")
    max_entry_notional_usd: Decimal = Decimal("25")
    max_trade_risk_usd: Decimal = Decimal("0.50")
    daily_loss_limit_usd: Decimal = Decimal("5")
    max_total_loss_usd: Decimal = Decimal("50")
    max_effective_leverage: Decimal = Decimal("1")
    exit_reserve_usd: Decimal = Decimal("2")
    max_quote_basis_bps: Decimal = Decimal("10")
    max_data_age_ms: int = Field(default=1500, ge=250, le=10000)
    warmup_seconds: int = Field(default=600, ge=600)
    max_hold_seconds: int = Field(default=180, ge=30, le=180)
    session_seconds: int = Field(default=1800, ge=60, le=18000)
    max_session_positions: int = Field(default=3, ge=1, le=3)
    allow_live_orders: bool = False
    live_entry_scope: Literal["probe_only", "strategy"] = "probe_only"
    probe_accept_reported_fees: bool = False
    session_accept_reported_fees: bool = False
    source_funding_spent_usd: Decimal | None = None

    @model_validator(mode="after")
    def limits(self) -> Config:
        if self.probe_accept_reported_fees and self.live_entry_scope != "probe_only":
            raise ValueError("reported fee evidence is restricted to one probe")
        if self.session_accept_reported_fees and self.live_entry_scope != "strategy":
            raise ValueError("session fee evidence requires separately authorized strategy scope")
        if self.source_funding_spent_usd is not None and not (
            0 <= self.source_funding_spent_usd <= self.max_funding_usd
        ):
            raise ValueError("source funding spent must stay within the source budget")
        if self.schema_version != 1 or self.mode not in {"observe", "live_capable"}:
            raise ValueError("unsupported config version or mode")
        if self.account_type != "standard" or self.market_symbol != "BTC":
            raise ValueError("only Standard BTC is supported")
        if self.max_funding_usd != 50 or self.max_total_loss_usd > 50:
            raise ValueError("funding cap must remain $50 and loss cap <= $50")
        if not (0 < self.max_entry_notional_usd <= 25):
            raise ValueError("entry notional must be at most $25")
        if not (0 < self.max_trade_risk_usd <= Decimal("0.50")):
            raise ValueError("per trade risk exceeds $0.50")
        if not (0 < self.daily_loss_limit_usd <= 5):
            raise ValueError("daily loss exceeds $5")
        if not (0 < self.max_effective_leverage <= 1):
            raise ValueError("effective exposure exceeds 1x")
        if self.exit_reserve_usd < 0:
            raise ValueError("exit reserve must be nonnegative")
        if self.mode == "live_capable" and self.account_index is None:
            raise ValueError("live_capable requires a dedicated account index")
        if self.allow_live_orders and self.mode != "live_capable":
            raise ValueError("allow_live_orders requires live_capable mode")
        if self.allow_live_orders and self.expected_account_type_code is None:
            raise ValueError("verify the account tier code locally before enabling live orders")
        if self.allow_live_orders and any(
            value is None
            for value in (
                self.expected_account_trading_mode,
                self.expected_margin_mode,
                self.expected_initial_margin_fraction,
            )
        ):
            raise ValueError("verify account trading and BTC margin settings before live orders")
        return self


class Secrets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_private_key: str
    api_key_index: int = Field(ge=0)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> Config:
    path = path or repo_root() / "config.json"
    return Config.model_validate_json(path.read_text())


def load_secrets(path: Path | None = None) -> Secrets:
    path = path or repo_root() / "secrets.json"
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise PermissionError("secrets.json must be owned by this user and mode 0600")
    # This object is never logged or returned by the API.
    return Secrets.model_validate(json.loads(path.read_text()))
