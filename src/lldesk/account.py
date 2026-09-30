from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import lighter
import websockets

from .config import Config, load_secrets
from .models import AccountEvent, OrderIntent, Stamp

BASE_URL = "https://mainnet.zklighter.elliot.ai"


class RateBudget:
    """Local Standard-account budget. Keep 20/min for cancel, reconcile, exit."""

    def __init__(self) -> None:
        self.calls: deque[float] = deque()

    def claim(self, priority: str) -> None:
        now = time.monotonic()
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        cap = 35 if priority == "entry" else 55
        if len(self.calls) >= cap:
            raise RuntimeError("local Standard API rate reserve exhausted")
        self.calls.append(now)


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    available: Decimal
    account_type_code: int
    position_qty: Decimal
    position_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    funding: Decimal
    active_orders: tuple[dict[str, Any], ...]
    other_positions: tuple[dict[str, Any], ...]
    other_assets: tuple[dict[str, Any], ...]
    received_at: float
    funding_flows: tuple[tuple[str, str], ...] = ()


class LighterAccount:
    """One signer, one nonce owner, one account. No secret leaves this process."""

    def __init__(self, config: Config):
        if config.account_index is None:
            raise ValueError("dedicated account_index is required")
        self.config = config
        self.account_index = config.account_index
        self.signer: Any = None
        self.api: Any = None
        self.account_api: Any = None
        self.order_api: Any = None
        self.transaction_api: Any = None
        self.lock = asyncio.Lock()
        self.rate = RateBudget()

    async def open(self) -> None:
        secrets = load_secrets()
        self.signer = lighter.SignerClient(
            url=BASE_URL,
            account_index=self.account_index,
            api_private_keys={secrets.api_key_index: secrets.api_private_key},
            nonce_management_type=lighter.nonce_manager.NonceManagerType.API,
        )
        # Drop the local reference; the SDK owns the private key thereafter.
        del secrets
        error = self.signer.check_client()
        if error is not None:
            await self.signer.close()
            self.signer = None
            raise RuntimeError("Lighter signer key/account check failed")
        self.api = lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL))
        self.account_api = lighter.AccountApi(self.api)
        self.order_api = lighter.OrderApi(self.api)
        self.transaction_api = lighter.TransactionApi(self.api)

    async def close(self) -> None:
        if self.signer:
            await self.signer.close()
        if self.api:
            await self.api.close()

    def auth(self) -> str:
        token, error = self.signer.create_auth_token_with_expiry()
        if error is not None or not token:
            raise RuntimeError("Lighter auth token creation failed")
        return token

    async def snapshot(self, market_id: int) -> AccountSnapshot:
        self.rate.claim("reconcile")
        response = await self.account_api.account(
            by="index", value=str(self.account_index), active_only=True
        )
        if response.code != 200 or len(response.accounts) != 1:
            raise RuntimeError("dedicated Lighter account response invalid")
        acct = response.accounts[0]
        if acct.index != self.account_index:
            raise RuntimeError("Lighter returned a different account")
        self.rate.claim("reconcile")
        orders = await self.order_api.account_active_orders(
            authorization=self.auth(), account_index=self.account_index
        )
        if orders.code != 200:
            raise RuntimeError("Lighter active orders request failed")
        flows = await self.funding_history(acct.l1_address)
        position = next((p for p in acct.positions if p.market_id == market_id), None)
        qty = (
            Decimal(position.position) * (1 if position.sign >= 0 else -1)
            if position
            else Decimal(0)
        )
        return AccountSnapshot(
            equity=Decimal(acct.total_asset_value),
            available=Decimal(acct.available_balance),
            account_type_code=acct.account_type,
            position_qty=qty,
            position_price=Decimal(position.avg_entry_price) if position else Decimal(0),
            unrealized_pnl=Decimal(position.unrealized_pnl) if position else Decimal(0),
            realized_pnl=Decimal(position.realized_pnl) if position else Decimal(0),
            funding=Decimal(position.total_funding_paid_out or "0") if position else Decimal(0),
            active_orders=tuple(o.to_dict() for o in orders.orders),
            other_positions=tuple(
                p.to_dict()
                for p in acct.positions
                if p.market_id != market_id and Decimal(p.position) != 0
            ),
            other_assets=tuple(
                a.to_dict() for a in acct.assets if a.symbol != "USDC" and Decimal(a.balance) != 0
            ),
            received_at=time.time(),
            funding_flows=flows,
        )

    async def funding_history(self, l1_address: str) -> tuple[tuple[str, str], ...]:
        """Read all dedicated-account flows; unknown statuses or truncated history freeze entry."""
        result: list[tuple[str, str]] = []
        for kind in ("deposit", "withdraw"):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(5):
                self.rate.claim("reconcile")
                if kind == "deposit":
                    page = await self.transaction_api.deposit_history(
                        authorization=self.auth(),
                        account_index=self.account_index,
                        l1_address=l1_address,
                        cursor=cursor,
                    )
                    items = page.deposits
                else:
                    page = await self.transaction_api.withdraw_history(
                        authorization=self.auth(),
                        account_index=self.account_index,
                        cursor=cursor,
                    )
                    items = page.withdraws
                if page.code != 200:
                    raise RuntimeError("Lighter funding history unavailable")
                for item in items:
                    if item.status.lower() != "completed" or item.asset_id != 3:
                        raise RuntimeError("unsettled or non-USDC funding flow requires review")
                    amount = Decimal(item.amount)
                    if amount <= 0:
                        raise RuntimeError("invalid funding flow amount")
                    signed = amount if kind == "deposit" else -amount
                    result.append((f"{kind}:{item.id}", str(signed)))
                next_cursor = page.cursor or None
                if not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise RuntimeError("funding pagination loop")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise RuntimeError("funding history exceeds verified page limit")
        return tuple(result)

    async def order_statuses(self, client_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not client_ids:
            return {}
        self.rate.claim("reconcile")
        response = await self.order_api.account_orders(
            authorization=self.auth(),
            client_order_indexes=",".join(map(str, client_ids[:20])),
            account_index=self.account_index,
        )
        if response.code != 200:
            raise RuntimeError("Lighter order status request failed")
        return {o.client_order_index: o.to_dict() for o in response.orders}

    async def send_ioc(
        self, intent: OrderIntent, market_id: int, size_decimals: int, price_decimals: int
    ) -> str:
        self.rate.claim("entry" if intent.kind in {"ENTRY", "PROBE"} else "exit")
        base = int(intent.quantity * (10**size_decimals))
        price = int(intent.limit_price * (10**price_decimals))
        if base <= 0 or price <= 0:
            raise ValueError("invalid scaled order quantity or price")
        async with self.lock:
            _tx, response, error = await asyncio.wait_for(
                self.signer.create_order(
                    market_index=market_id,
                    client_order_index=intent.client_id,
                    base_amount=base,
                    price=price,
                    is_ask=intent.side == "sell",
                    order_type=self.signer.ORDER_TYPE_LIMIT,
                    time_in_force=self.signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                    reduce_only=intent.kind not in {"ENTRY", "PROBE"},
                ),
                timeout=10,
            )
        if error or response is None or response.code != 200:
            raise RuntimeError("Lighter rejected or did not acknowledge IOC")
        return str(response.tx_hash)

    async def send_stop(
        self,
        intent: OrderIntent,
        market_id: int,
        size_decimals: int,
        price_decimals: int,
        trigger_price: Decimal,
    ) -> str:
        self.rate.claim("exit")
        async with self.lock:
            _tx, response, error = await asyncio.wait_for(
                self.signer.create_sl_order(
                    market_index=market_id,
                    client_order_index=intent.client_id,
                    base_amount=int(intent.quantity * (10**size_decimals)),
                    trigger_price=int(trigger_price * (10**price_decimals)),
                    price=int(intent.limit_price * (10**price_decimals)),
                    is_ask=intent.side == "sell",
                    reduce_only=True,
                ),
                timeout=10,
            )
        if error or response is None or response.code != 200:
            raise RuntimeError("Lighter stop not acknowledged")
        return str(response.tx_hash)

    async def cancel(self, market_id: int, exchange_order_index: int) -> str:
        if exchange_order_index <= 0:
            raise ValueError("exchange order index required for cancel")
        self.rate.claim("exit")
        async with self.lock:
            _tx, response, error = await asyncio.wait_for(
                self.signer.cancel_order(market_index=market_id, order_index=exchange_order_index),
                timeout=10,
            )
        if error or response is None or response.code != 200:
            raise RuntimeError("Lighter cancel not acknowledged")
        return str(response.tx_hash)

    async def stream(self, market_id: int, queue: asyncio.Queue[AccountEvent]) -> None:
        """Private events speed up fill detection; REST remains reconciliation authority."""
        channel = f"account_market/{market_id}/{self.account_index}"
        while True:
            try:
                async with websockets.connect(
                    BASE_URL.replace("https://", "wss://") + "/stream",
                    ping_interval=30,
                    max_queue=128,
                    open_timeout=10,
                ) as ws:
                    await ws.send(
                        json.dumps({"type": "subscribe", "channel": channel, "auth": self.auth()})
                    )
                    # Reconnect before the SDK's 10-minute auth token expires.
                    deadline = time.monotonic() + 480
                    while time.monotonic() < deadline:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=35)
                        except TimeoutError:
                            continue
                        data = json.loads(raw)
                        if data.get("type") == "update/account_market":
                            event = AccountEvent(
                                self.account_index, data, Stamp(exchange_ms=data.get("timestamp"))
                            )
                            queue.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Dropped account events invalidate incremental state. REST must reconcile.
                while not queue.empty():
                    queue.get_nowait()
                    queue.task_done()
                queue.put_nowait(AccountEvent(self.account_index, {"type": "disconnect"}, Stamp()))
                await asyncio.sleep(3)
