from __future__ import annotations

import asyncio
import fcntl
import io
import logging
import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .account import AccountSnapshot, DispatchBlocked, LighterAccount, LocalOrderRejected
from .config import Config, load_config, repo_root
from .db import Database
from .market import FX_BOOK, Book, Feeds, GapError, Recorder, dec, market_metadata
from .models import (
    TERMINAL_ORDER_STATES,
    AccountEvent,
    EngineState,
    MarketEvent,
    OrderIntent,
    OrderState,
    Stamp,
    normalize_order_state,
)
from .risk import EquityBaseline, limit_price, size_entry
from .strategy import Decision, DeviationStrategy

LOG = logging.getLogger("lldesk.engine")
JAKARTA = ZoneInfo("Asia/Jakarta")
# Eight reads per flat-account reconcile fit the 40/min local read budget.
ACCOUNT_RECONCILE_INTERVAL_SECONDS = 12


class Engine:
    def __init__(self, config: Config, root: Path | None = None):
        self.config = config
        self.root = root or repo_root()
        self.db = Database(self.root / "state" / "desk.db")
        self.lock_file: io.TextIOWrapper | None = None
        self.state = EngineState.STARTING
        self.reason = "memulai"
        self.market: dict | None = None
        self.books = {"binance": Book("binance"), "lighter": Book("lighter")}
        self.queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=2048)
        self.account_queue: asyncio.Queue[AccountEvent] = asyncio.Queue(maxsize=256)
        self.feeds: Feeds | None = None
        self.recorder = Recorder(self.root / "state" / "market")
        self.strategy = DeviationStrategy(config.warmup_seconds)
        self.account: LighterAccount | None = None
        self.account_snapshot: AccountSnapshot | None = None
        self.baseline: EquityBaseline | None = None
        self.current_flow = Decimal(0)
        self.fx: Decimal | None = None
        self.fx_at = 0.0
        self.last_decision = Decision(None, None, None, None, ("BELUM_ADA_DATA",))
        self.last_account_poll = 0.0
        self.last_reconcile_ok = 0.0
        self.reconcile_failed = True
        self.session_end = 0.0
        self.session_positions = 0
        self.entries_stopped = True
        self.probe_active = self.db.flag("probe_pending") == "1"
        self.last_position_qty = Decimal(0)
        self.position_opened_at = 0.0
        self.protection_id: int | None = None
        self.protection_confirmed = False
        self.protect_sent_at = 0.0
        self.exit_id: int | None = None
        self.manual_exposure = False
        self.lag_ms = 0.0
        self.last_order_ack_ms: float | None = None
        self.last_cancel_ack_ms: float | None = None
        self.journal_ok = True
        self.tasks: list[asyncio.Task] = []
        self.reconcile_task: asyncio.Task | None = None
        self.reconcile_lock = asyncio.Lock()
        self.entry_epoch = 0
        self.entry_tasks: set[asyncio.Task] = set()
        self.dispatching_entries: set[int] = set()
        self._restore_order_ids()

    def _restore_order_ids(self) -> None:
        for item in self.db.unresolved():
            if item["kind"] == "STOP":
                self.protection_id = item["client_id"]
            elif item["kind"] == "EXIT":
                self.exit_id = item["client_id"]
        opened = self.db.flag("position_opened_epoch")
        if opened:
            self.position_opened_at = time.monotonic() - max(0, time.time() - float(opened))

    def margin_settings_valid(self, snap: AccountSnapshot) -> bool:
        return (
            self.config.expected_account_trading_mode is not None
            and self.config.expected_margin_mode is not None
            and self.config.expected_initial_margin_fraction is not None
            and snap.account_trading_mode == self.config.expected_account_trading_mode
            and snap.margin_mode == self.config.expected_margin_mode
            and snap.initial_margin_fraction == self.config.expected_initial_margin_fraction
        )

    def exclusive(self) -> None:
        path = self.root / "state" / "engine.lock"
        lock_file = path.open("w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise RuntimeError("engine lain sudah berjalan") from exc
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        self.lock_file = lock_file

    async def run(self) -> None:
        self.exclusive()
        self.state = EngineState.RECONCILING
        self.entries_stopped = True
        try:
            self.market = await market_metadata()
            self._check_market()
            self.feeds = Feeds(
                int(self.market["market_id"]), self.queue, self.config.binance_dns_mode
            )
            if self.config.mode == "live_capable":
                try:
                    self.account = LighterAccount(self.config)
                    await self.account.open()
                    await self.safe_reconcile()
                except Exception as exc:
                    self.state = EngineState.PAUSED
                    self.reason = f"akun belum siap: {type(exc).__name__}"
                    LOG.warning("account setup blocked: %s", type(exc).__name__)
            else:
                self.state = EngineState.WARMING_UP
                self.reason = "mode observasi; order dinonaktifkan"
            self.tasks = [
                asyncio.create_task(self.feeds.binance()),
                asyncio.create_task(self.feeds.lighter()),
                asyncio.create_task(self.fx_loop()),
            ]
            if self.account:
                self.tasks.append(
                    asyncio.create_task(
                        self.account.stream(int(self.market["market_id"]), self.account_queue)
                    )
                )
            await self.loop()
        finally:
            self.entry_epoch += 1
            self.entries_stopped = True
            if self.reconcile_task:
                self.tasks.append(self.reconcile_task)
            self.tasks.extend(self.entry_tasks)
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.account:
                await self.account.close()
            self.recorder.close()
            self.db.close()
            if self.lock_file:
                self.lock_file.close()

    def _check_market(self) -> None:
        assert self.market is not None
        m = self.market
        if m["symbol"] != "BTC" or m["market_type"] != "perp" or m["status"] != "active":
            raise RuntimeError("BTC perpetual market changed")
        if int(m["market_id"]) < 0 or int(m["size_decimals"]) > 8:
            raise RuntimeError("invalid market metadata")
        if Decimal(str(m["min_quote_amount"])) > self.config.max_entry_notional_usd:
            raise RuntimeError("minimum Lighter order exceeds entry cap")
        if Decimal(str(m["taker_fee"])) != 0:
            raise RuntimeError("Standard fee changed; stop entries")

    async def fx_loop(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                try:
                    response = await client.get(FX_BOOK)
                    response.raise_for_status()
                    data = response.json()
                    if data.get("symbol") != "USDCUSDT":
                        raise ValueError("wrong FX symbol")
                    bid, ask = dec(data["bidPrice"]), dec(data["askPrice"])
                    if bid <= 0 or ask <= bid:
                        raise ValueError("bad FX quote")
                    self.fx = (bid + ask) / 2
                    self.fx_at = time.monotonic()
                except Exception:
                    self.fx = None
                await asyncio.sleep(10)

    async def loop(self) -> None:
        try:
            await self._loop()
        finally:
            if self.reconcile_task:
                self.reconcile_task.cancel()
                await asyncio.gather(self.reconcile_task, return_exceptions=True)

    async def _loop(self) -> None:
        last_status_at = 0.0
        while True:
            start = time.monotonic()
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=0.05)
                self.on_market(event)
                self.queue.task_done()
                # Preserve every event's order while amortizing coordinator work.
                # A time bound keeps commands responsive even under a sustained burst.
                batch_end = time.monotonic() + 0.01
                for _ in range(255):
                    if self.queue.empty() or time.monotonic() >= batch_end:
                        break
                    self.on_market(self.queue.get_nowait())
                    self.queue.task_done()
            except TimeoutError:
                pass
            except GapError as exc:
                self.state = EngineState.RECOVERING
                self.reason = str(exc)
                self.entries_stopped = True
                self.db.event("BOOK_GAP", {"reason": str(exc)})
                # The feed reconnects on transport failure; a sequence gap here
                # requires explicit task restart to obtain a fresh snapshot.
                await self.restart_feeds()
            except Exception as exc:
                self.state = EngineState.HALTED
                self.entries_stopped = True
                self.reason = f"market handler error: {type(exc).__name__}"
                LOG.exception("market handler failed")
            try:
                while not self.account_queue.empty():
                    self.on_account(self.account_queue.get_nowait())
                    self.account_queue.task_done()
            except Exception as exc:
                self.reconcile_failed = True
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = f"feed akun perlu rekonsiliasi: {type(exc).__name__}"
            await self.commands()
            if (
                self.account
                and not (self.probe_active and self.entry_tasks)
                and time.monotonic() - self.last_account_poll
                >= (2 if self.probe_active else ACCOUNT_RECONCILE_INTERVAL_SECONDS)
                and self.probe_read_ready()
                and (self.reconcile_task is None or self.reconcile_task.done())
            ):
                self.reconcile_task = asyncio.create_task(self.safe_reconcile())
            self.decide()
            await self.manage_position()
            if (
                (self.state == EngineState.LIVE or self.probe_active)
                and self.session_end
                and time.monotonic() >= self.session_end
            ):
                self.entries_stopped = True
                await self.cancel_entries()
                if self.probe_active:
                    self.session_end = 0.0
                self.state = EngineState.PAUSED
                self.reason = "batas waktu sesi; posisi tetap dikelola"
            self.lag_ms = max(0.0, (time.monotonic() - start - 0.05) * 1000)
            try:
                if time.monotonic() - last_status_at >= 0.25:
                    self.db.save_status(self.status())
                    last_status_at = time.monotonic()
                    self.journal_ok = True
            except Exception:
                self.journal_ok = False
                self.entries_stopped = True
                self.state = EngineState.HALTED
                self.reason = "jurnal/database gagal ditulis"
                LOG.exception("journal failure")
            await asyncio.sleep(0)

    async def restart_feeds(self) -> None:
        if not self.feeds:
            return
        for task in self.tasks[:2]:
            task.cancel()
        await asyncio.gather(*self.tasks[:2], return_exceptions=True)
        self.books["binance"].invalidate()
        self.books["lighter"].invalidate()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.tasks[:2] = [
            asyncio.create_task(self.feeds.binance()),
            asyncio.create_task(self.feeds.lighter()),
        ]

    def on_market(self, event: MarketEvent) -> None:
        self.recorder.write(event)
        book = self.books.get(event.venue)
        if not book:
            return
        if event.kind == "disconnect":
            book.invalidate()
            self.entries_stopped = True
            self.state = EngineState.RECOVERING
            self.reason = f"{event.venue} terputus; menunggu snapshot baru"
            return
        if event.venue == "binance":
            if event.kind == "snapshot":
                book.binance_snapshot(event.payload, event.stamp)
            elif event.kind == "depthUpdate":
                book.binance_delta(event.payload, event.stamp)
        elif event.venue == "lighter":
            if event.kind == "order_book":
                if book.sequence is None:
                    book.lighter_snapshot(event.payload, event.stamp)
                else:
                    book.lighter_delta(event.payload, event.stamp)
            elif event.kind == "trade":
                for trade in event.payload.get("trades", []):
                    self.strategy.trade(
                        dec(trade["price"]), dec(trade["size"]), bool(trade.get("is_maker_ask"))
                    )

    def on_account(self, event: AccountEvent) -> None:
        if event.payload.get("type") == "disconnect":
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "feed akun terputus; menunggu rekonsiliasi REST"
            return
        trades = event.payload.get("trades", [])
        if isinstance(trades, dict):
            trades = [
                x
                for group in trades.values()
                for x in (group if isinstance(group, list) else [group])
            ]
        for trade in trades:
            self._record_trade(trade, event.account_index)

    def _record_trade(self, trade: dict, account_index: int) -> None:
        ask_owned = str(trade.get("ask_account_id")) == str(account_index)
        bid_owned = str(trade.get("bid_account_id")) == str(account_index)
        if not ask_owned and not bid_owned:
            self.db.set_flag("account_incident_pending", "1")
            raise ValueError("trade from another account")
        if ask_owned and bid_owned:
            self.db.set_flag("account_incident_pending", "1")
            raise ValueError("self trade needs manual review")
        client_id = trade.get("ask_client_id" if ask_owned else "bid_client_id")
        if client_id is None:
            self.db.set_flag("account_incident_pending", "1")
            raise ValueError("trade missing client ID")
        known = self.db.conn.execute(
            "SELECT 1 FROM intents WHERE client_id=?", (int(client_id),)
        ).fetchone()
        if not known:
            self.db.set_flag("account_incident_pending", "1")
            raise ValueError("trade without local intent")
        maker = bool(trade["is_maker_ask"]) == ask_owned
        fee_micro = trade.get("maker_fee" if maker else "taker_fee")
        if fee_micro is None:
            self.db.set_flag("account_incident_pending", "1")
            raise ValueError("trade fee unavailable")
        inserted = self.db.add_fill(
            str(trade["trade_id"]),
            int(client_id),
            str(trade["size"]),
            str(trade["price"]),
            str(dec(fee_micro) / Decimal(1_000_000)),
            trade.get("timestamp"),
        )
        if inserted:
            self.db.event(
                "FILL",
                {
                    "trade_id": trade["trade_id"],
                    "client_id": client_id,
                    "size": trade["size"],
                    "price": trade["price"],
                },
            )
            self.entries_stopped = True

    def data_valid(self) -> bool:
        now = time.monotonic_ns()
        max_age = self.config.max_data_age_ms * 1_000_000
        return (
            all(book.valid and now - book.updated_ns < max_age for book in self.books.values())
            and self.books["binance"].bridged
            and self.feeds is not None
            and self.feeds.binance_public_ok
            and self.feeds.binance_trades_ok
            and self.fx is not None
            and time.monotonic() - self.fx_at < 15
            and self.queue.qsize() < 1500
        )

    def idle_valid(self) -> bool:
        """Healthy flat monitoring; live margin verification remains a separate gate."""
        snap = self.account_snapshot
        now = time.monotonic()
        return bool(
            not self.config.allow_live_orders
            and self.entries_stopped
            and not self.probe_active
            and now >= self.session_end
            and self.account
            and snap
            and not self.reconcile_failed
            and now - self.last_reconcile_ok < 15
            and time.time() - snap.received_at < 15
            and self.config.expected_account_type_code is not None
            and snap.account_type_code == self.config.expected_account_type_code
            and self.config.expected_account_trading_mode is not None
            and snap.account_trading_mode == self.config.expected_account_trading_mode
            and not snap.position_qty
            and not snap.active_orders
            and not snap.other_positions
            and not snap.other_assets
            and not self.manual_exposure
            and self.exit_id is None
            and self.protection_id is None
            and not self.db.unresolved()
            and self.db.flag("close_requested") != "1"
            and self.db.flag("daily_stop_day") != datetime.now(JAKARTA).date().isoformat()
            and all(
                self.db.flag(flag) != "1"
                for flag in (
                    "total_loss_stop",
                    "funding_change_pending",
                    "account_incident_pending",
                    "probe_pending",
                )
            )
            and Decimal(self.db.gross_deposits()) <= self.config.max_funding_usd
            and 0 < snap.equity <= self.config.max_funding_usd
            and self.journal_ok
            and self.data_valid()
            and self.last_decision.basis_bps is not None
        )

    def decide(self) -> None:
        self.last_decision = self.strategy.evaluate(
            self.books["lighter"].quote(), self.books["binance"].quote(), self.data_valid(), self.fx
        )
        if self.state == EngineState.RECOVERING and self.data_valid():
            self.state = EngineState.WARMING_UP
            self.reason = "feed tersambung kembali; evaluasi coverage"
        if self.state == EngineState.WARMING_UP and self.last_decision.basis_bps is not None:
            self.state = EngineState.READY
            self.reason = "data siap; mode observasi" if not self.account else "siap untuk sesi"
        if self.state not in {
            EngineState.STARTING,
            EngineState.HALTED,
            EngineState.LIVE,
            EngineState.PROBING,
        }:
            if self.idle_valid():
                self.state = EngineState.IDLE
                self.reason = "standby; akun flat, data segar, entry dinonaktifkan"
            elif self.state == EngineState.IDLE:
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = "idle ditahan; akun, pasar, atau jurnal belum valid"
        if (
            self.state == EngineState.LIVE
            and not self.entries_stopped
            and self.session_positions < self.config.max_session_positions
            and self.last_decision.side
        ):
            if not self.db.unresolved() and (
                not self.account_snapshot or not self.account_snapshot.position_qty
            ):
                task = asyncio.create_task(
                    self.enter(self.last_decision.side, scheduled_epoch=self.entry_epoch)
                )
                self.entry_tasks.add(task)
                task.add_done_callback(self.entry_tasks.discard)
                self.entries_stopped = True  # prevent duplicate signal while send is in flight

    async def safe_reconcile(self) -> None:
        try:
            async with self.reconcile_lock:
                self.last_account_poll = time.monotonic()
                async with asyncio.timeout(8):
                    await self.reconcile()
                self.reconcile_failed = False
        except Exception as exc:
            self.reconcile_failed = True
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"rekonsiliasi gagal: {type(exc).__name__}"
            LOG.warning("reconciliation blocked: %s", type(exc).__name__)

    async def reconcile(self) -> None:
        if not self.account or not self.market:
            return
        market_id = int(self.market["market_id"])
        exposure_only = bool(
            self.probe_active
            and self.account_snapshot
            and (
                self.account_snapshot.position_qty
                or self.db.unresolved()
                or self.protection_id is not None
                or self.exit_id is not None
                or self.entry_tasks
                or self.dispatching_entries
            )
        )
        if self.probe_active:
            flows = (
                self.account_snapshot.funding_flows
                if exposure_only and self.account_snapshot
                else None
            )
            snapshot = await self.account.probe_snapshot(market_id, flows)
        else:
            snapshot = await self.account.snapshot(market_id)
        self.last_account_poll = time.monotonic()
        if (
            self.config.mode == "live_capable"
            and self.config.expected_account_type_code is not None
        ):
            if snapshot.account_type_code != self.config.expected_account_type_code:
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = "tier akun berubah; entry diblok"
        # A flat account can monitor while the API omits its BTC margin settings.
        # Live entry still requires those settings in live_blockers and dispatch.
        if (
            self.config.mode == "live_capable"
            and not self.margin_settings_valid(snapshot)
            and (self.config.allow_live_orders or snapshot.position_qty)
        ):
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "mode akun atau margin BTC belum terverifikasi"
        if snapshot.other_positions or snapshot.other_assets:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "akun memiliki aset/posisi lain; entry diblok"
        if (
            snapshot.position_qty
            and abs(snapshot.position_qty * snapshot.position_price)
            > self.config.max_entry_notional_usd
        ):
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "posisi melampaui pagu; entry diblok"
        unresolved = self.db.unresolved()
        known = {x["client_id"] for x in unresolved}
        for order in snapshot.active_orders:
            if int(order["client_order_index"]) not in known:
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = "order manual/tidak dikenal; perlu rekonsiliasi"
        client_ids = [x["client_id"] for x in unresolved]
        after_id = int(self.db.flag("last_trade_id") or "0")
        if self.probe_active:
            statuses = await self.account.order_statuses(client_ids, priority="probe_read")
            trades = await self.account.recent_trades(market_id, after_id, priority="probe_read")
        else:
            statuses = await self.account.order_statuses(client_ids)
            trades = await self.account.recent_trades(market_id, after_id)
        for trade in reversed(trades):
            self._record_trade(trade, self.account.account_index)
        if trades:
            self.db.set_flag("last_trade_id", str(max(int(t["trade_id"]) for t in trades)))
        for intent in unresolved:
            matched_order = statuses.get(intent["client_id"])
            if not matched_order:
                continue  # uncertain intent stays frozen
            status = str(matched_order.get("status", ""))
            filled = dec(matched_order.get("filled_base_amount", "0"))
            new_state = normalize_order_state(status, filled)
            self.db.order_state(intent["client_id"], new_state)
            self.db.event(
                "ORDER_STATUS",
                {
                    "client_id": intent["client_id"],
                    "order": {
                        key: matched_order.get(key)
                        for key in (
                            "order_index",
                            "client_order_index",
                            "market_index",
                            "status",
                            "type",
                            "is_ask",
                            "reduce_only",
                            "remaining_base_amount",
                            "filled_base_amount",
                            "price",
                            "trigger_price",
                        )
                    },
                },
            )
        last_flat = float(self.db.flag("last_flat_epoch") or "0")
        own_qty = sum(
            (
                dec(qty) * (1 if side == "buy" else -1)
                for qty, side in self.db.conn.execute(
                    "SELECT f.qty,i.side FROM fills f JOIN intents i ON i.client_id=f.client_id "
                    "WHERE i.created_at>?",
                    (last_flat,),
                )
            ),
            Decimal(0),
        )
        # Publish account exposure only after ownership evidence is available.
        # The foreground loop keeps managing the last fully reconciled snapshot.
        self.account_snapshot = snapshot
        self.manual_exposure = bool(snapshot.position_qty and own_qty != snapshot.position_qty)
        if self.probe_active and own_qty != snapshot.position_qty:
            # A fill can arrive after the account GET. Do not declare flat or
            # retry a filled close using that older snapshot; fetch again promptly.
            self.last_account_poll = time.monotonic() - ACCOUNT_RECONCILE_INTERVAL_SECONDS
        if self.manual_exposure:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "posisi tidak cocok dengan fill lokal; periksa intervensi manual"
        if self.protection_id is not None:
            protected = statuses.get(self.protection_id)
            previously_confirmed = self.protection_confirmed
            self.protection_confirmed = self.stop_covers_position(protected, snapshot)
            if self.protection_confirmed and not previously_confirmed:
                self.db.event(
                    "PROTECTION_CONFIRMED",
                    {
                        "client_id": self.protection_id,
                        "position_qty": str(snapshot.position_qty),
                        "snapshot_received_at": snapshot.received_at,
                        "order_index": protected.get("order_index") if protected else None,
                    },
                )
            if previously_confirmed and not self.protection_confirmed and not self.manual_exposure:
                self.entries_stopped = True
                self.db.set_flag("close_requested", "1")
        if not exposure_only:
            self._update_baseline(snapshot)
        if self.db.flag("close_requested") == "1":
            await self.cancel_entries()
        if self.exit_id is not None and snapshot.position_qty and own_qty == snapshot.position_qty:
            exit_status = statuses.get(self.exit_id)
            if (
                exit_status
                and normalize_order_state(str(exit_status.get("status", "")))
                in TERMINAL_ORDER_STATES
            ):
                self.exit_id = None
        if snapshot.position_qty != self.last_position_qty:
            if not self.last_position_qty and snapshot.position_qty:
                if not self.position_opened_at:
                    self.position_opened_at = time.monotonic()
                    self.db.set_flag("position_opened_epoch", str(time.time()))
                    if not self.db.flag("exit_attempts"):
                        self.db.set_flag("exit_attempts", "0")
            if self.last_position_qty and not snapshot.position_qty:
                self.db.set_flag("close_requested", "0")
                if self.db.flag("probe_pending") != "1":
                    self.session_positions += 1
                self.position_opened_at = 0.0
                self.db.set_flag("position_opened_epoch", "")
                self.db.set_flag("exit_attempts", "0")
            self.last_position_qty = snapshot.position_qty
        if (
            snapshot.position_qty
            and not self.manual_exposure
            and self.protection_id is None
            and self.exit_id is None
        ):
            await self.protect_position(snapshot)
        elif (
            snapshot.position_qty
            and self.protection_id is not None
            and not self.protection_confirmed
            and self.exit_id is None
            and not self.manual_exposure
            and time.monotonic() - self.protect_sent_at > 8
        ):
            self.reason = "stop tidak terkonfirmasi; meminta tutup posisi"
            await self.close_position()
        elif not snapshot.position_qty and self.exit_id is not None:
            self.reason = "posisi flat menurut bursa; entry tetap berhenti"
            self.exit_id = None
            self.db.set_flag("close_requested", "0")
        if not snapshot.position_qty and self.protection_id is not None:
            stop_status = statuses.get(self.protection_id)
            if (
                stop_status
                and normalize_order_state(str(stop_status.get("status", "")))
                in TERMINAL_ORDER_STATES
            ):
                self.protection_id = None
                self.protection_confirmed = False
            elif stop_status and stop_status.get("status") in {"pending", "open"}:
                state_row = self.db.conn.execute(
                    "SELECT state FROM intents WHERE client_id=?", (self.protection_id,)
                ).fetchone()
                if state_row and state_row[0] != OrderState.CANCEL_REQUESTED:
                    stop_id = self.protection_id
                    self.db.order_state(stop_id, OrderState.CANCEL_REQUESTED)
                    try:
                        order_index = int(stop_status.get("order_index") or 0)
                        if order_index <= 0:
                            raise ValueError("stop exchange order index unavailable")
                        sent_at = time.monotonic()
                        tx = await self.account.cancel(market_id, order_index)
                        self.db.event(
                            "CANCEL_ACK",
                            {
                                "client_id": stop_id,
                                "order_index": order_index,
                                "tx_hash": tx,
                            },
                        )
                        self.last_cancel_ack_ms = (time.monotonic() - sent_at) * 1000
                    except Exception:
                        self.db.order_state(stop_id, OrderState.UNKNOWN)
                        self.reason = "posisi flat; cancel stop belum pasti"
        if (
            not snapshot.position_qty
            and not own_qty
            and not any(x["kind"] in {"ENTRY", "PROBE"} for x in self.db.unresolved())
        ):
            self.db.set_flag("last_flat_epoch", str(time.time()))
        if (
            self.probe_active
            and not exposure_only
            and not snapshot.position_qty
            and not own_qty
            and not snapshot.active_orders
            and not self.db.unresolved()
            and self.protection_id is None
            and self.exit_id is None
            and not self.entry_tasks
            and not self.dispatching_entries
        ):
            self.probe_active = False
            self.db.set_flag("probe_pending", "0")
            self.session_end = 0.0
            self.entries_stopped = True
            if self.state != EngineState.HALTED:
                self.state = EngineState.PAUSED
                self.reason = "probe selesai menurut rekonsiliasi; entry strategi tetap berhenti"
        if self.state == EngineState.RECONCILING:
            self.state = EngineState.WARMING_UP
            self.reason = "rekonsiliasi selesai; menunggu data valid"
        if not exposure_only:
            self.last_reconcile_ok = time.monotonic()
        if (
            self.state == EngineState.PAUSED
            and not snapshot.position_qty
            and not snapshot.active_orders
            and not self.db.unresolved()
            and self.db.flag("daily_stop_day") != datetime.now(JAKARTA).date().isoformat()
            and self.db.flag("total_loss_stop") != "1"
            and self.db.flag("funding_change_pending") != "1"
            and self.db.flag("account_incident_pending") != "1"
            and Decimal(self.db.gross_deposits()) <= self.config.max_funding_usd
            and snapshot.account_type_code == self.config.expected_account_type_code
            and self.margin_settings_valid(snapshot)
            and self.data_valid()
            and self.last_decision.basis_bps is not None
        ):
            self.state = EngineState.READY
            self.reason = "akun dan pasar tersinkron; entry menunggu perintah"

    def stop_covers_position(self, order: dict | None, snap: AccountSnapshot) -> bool:
        if not order or not self.market or not snap.position_qty:
            return False
        try:
            short = snap.position_qty < 0
            trigger = dec(order["trigger_price"])
            side = "buy" if short else "sell"
            expected = snap.position_price * (Decimal("1.0035") if short else Decimal("0.9965"))
            expected = limit_price(
                expected, side, Decimal(1).scaleb(-int(self.market["price_decimals"])), Decimal(0)
            )
            return (
                order.get("status") in {"pending", "open"}
                and order.get("reduce_only") is True
                and order.get("is_ask") is (not short)
                and int(order["market_index"]) == int(self.market["market_id"])
                and order.get("type") in {"stop-loss", "stop-loss-limit"}
                and dec(order["remaining_base_amount"]) == abs(snap.position_qty)
                and trigger > 0
                and trigger == expected
            )
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return False

    def _update_baseline(self, snap: AccountSnapshot) -> None:
        day = datetime.now(JAKARTA).date().isoformat()
        known_flows = {row[0] for row in self.db.conn.execute("SELECT flow_id FROM funding_flows")}
        new_flows = {flow_id for flow_id, _ in snap.funding_flows} - known_flows
        flow = Decimal(self.db.record_flows(snap.funding_flows))
        self.current_flow = flow
        if new_flows and self.db.flag("live_ever") == "1":
            self.db.set_flag("funding_change_pending", "1")
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "arus dana berubah setelah sesi live; perlu audit manual"
        if Decimal(self.db.gross_deposits()) > self.config.max_funding_usd:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "total deposit melampaui pagu eksperimen"
        first = self.db.conn.execute(
            "SELECT day,equity,cumulative_flow FROM equity_marks ORDER BY id LIMIT 1"
        ).fetchone()
        today = self.db.conn.execute(
            "SELECT day,equity,cumulative_flow FROM equity_marks WHERE day=? ORDER BY id LIMIT 1",
            (day,),
        ).fetchone()
        if not first or not today:
            self.db.conn.execute(
                "INSERT INTO equity_marks(day,equity,cumulative_flow,recorded_at) VALUES(?,?,?,?)",
                (day, str(snap.equity), str(flow), time.time()),
            )
            first = first or (day, str(snap.equity), str(flow))
            today = today or (day, str(snap.equity), str(flow))
        self.baseline = EquityBaseline(
            dec(first[1]), dec(first[2]), dec(today[1]), dec(today[2]), day
        )
        self.db.conn.execute(
            "INSERT INTO equity_marks(day,equity,cumulative_flow,recorded_at) VALUES(?,?,?,?)",
            (day, str(snap.equity), str(flow), time.time()),
        )
        loss_reasons = self.baseline.check(self.config, snap.equity, flow)
        if "DAILY_LOSS" in loss_reasons:
            self.db.set_flag("daily_stop_day", day)
        if "TOTAL_LOSS" in loss_reasons:
            self.db.set_flag("total_loss_stop", "1")
        if loss_reasons:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "batas loss equity tercapai"
            self.db.set_flag("close_requested", "1")

    async def protect_position(self, snap: AccountSnapshot) -> None:
        if not self.account or not self.market:
            return
        side = "sell" if snap.position_qty > 0 else "buy"
        trigger = snap.position_price * (Decimal("0.9965") if side == "sell" else Decimal("1.0035"))
        limit = trigger * (Decimal("0.997") if side == "sell" else Decimal("1.003"))
        tick = Decimal(1).scaleb(-int(self.market["price_decimals"]))
        qty = abs(snap.position_qty)
        client_id = self.db.next_client_id()
        trigger = limit_price(trigger, side, tick, Decimal(0))
        limit = limit_price(limit, side, tick, Decimal(0))
        try:
            self.db.intent(client_id, "STOP", side, str(qty), str(limit))
            self.protection_id = client_id
            self.protect_sent_at = time.monotonic()
            self.db.order_state(client_id, OrderState.SENT)
            sent_at = time.monotonic()
            self.db.event(
                "STOP_DISPATCH",
                {
                    "client_id": client_id,
                    "quantity": str(qty),
                    "side": side,
                    "trigger_price": str(trigger),
                    "limit_price": str(limit),
                    "reduce_only": True,
                },
            )
            tx = await self.account.send_stop(
                OrderIntent(client_id, "STOP", side, qty, limit, Stamp()),
                int(self.market["market_id"]),
                int(self.market["size_decimals"]),
                int(self.market["price_decimals"]),
                trigger,
            )
            self.db.order_state(client_id, OrderState.SENT, tx)
            self.last_order_ack_ms = (time.monotonic() - sent_at) * 1000
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "stop dikirim; menunggu konfirmasi bursa"
        except Exception:
            self.db.order_state(client_id, OrderState.UNKNOWN)
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "proteksi posisi belum pasti; perlu tutup posisi"
            self.db.set_flag("close_requested", "1")

    async def enter(
        self, side: str, kind: str = "ENTRY", *, scheduled_epoch: int | None = None
    ) -> None:
        epoch = self.entry_epoch if scheduled_epoch is None else scheduled_epoch
        entry_state = EngineState.PROBING if kind == "PROBE" else EngineState.LIVE
        if (
            epoch != self.entry_epoch
            or self.state != entry_state
            or (kind == "PROBE" and not self.probe_active)
            or self.config.mode != "live_capable"
            or not self.config.allow_live_orders
            or (kind != "PROBE" and self.config.live_entry_scope != "strategy")
            or not self.session_end
            or time.monotonic() >= self.session_end
            or (kind != "PROBE" and self.session_positions >= self.config.max_session_positions)
        ):
            return
        if (
            not self.account
            or not self.market
            or not self.account_snapshot
            or not self.data_valid()
        ):
            return
        snap = self.account_snapshot
        if (
            time.time() - snap.received_at > 15
            or snap.account_type_code != self.config.expected_account_type_code
            or not self.margin_settings_valid(snap)
            or snap.active_orders
            or self.db.flag("total_loss_stop") == "1"
            or self.db.flag("daily_stop_day") == datetime.now(JAKARTA).date().isoformat()
        ):
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "akun, tier, atau batas loss tidak siap saat entry"
            return
        if snap.position_qty or self.db.unresolved():
            return
        quote = self.books["lighter"].quote()
        if not quote:
            return
        best = quote.ask if side == "buy" else quote.bid
        try:
            sizing = size_entry(
                self.config,
                min(snap.equity, snap.available),
                Decimal(0),
                Decimal(0),
                best,
                side,
                dec(self.market["min_base_amount"]),
                dec(self.market["min_quote_amount"]),
                int(self.market["size_decimals"]),
                int(self.market["price_decimals"]),
                Decimal("35"),
                Decimal("0"),
                minimum_only=kind == "PROBE",
            )
            depth_price = self.books["lighter"].price_for(side, sizing.quantity)
            if depth_price is None:
                raise ValueError("insufficient order book depth")
            impact_bps = abs(depth_price / best - 1) * 10000
            residual = abs(
                (self.last_decision.deviation_bps or Decimal(0))
                - (self.last_decision.basis_bps or Decimal(0))
            )
            spread_bps = (quote.ask - quote.bid) / quote.mid * 10000
            if kind != "PROBE" and residual <= impact_bps + spread_bps + 5:
                raise ValueError("depth, spread and uncertainty erase expected edge")
            client_id = self.db.next_client_id()
            self.db.intent(client_id, kind, side, str(sizing.quantity), str(sizing.price))

            def before_dispatch() -> bool:
                current = self.account_snapshot
                row = self.db.conn.execute(
                    "SELECT state FROM intents WHERE client_id=?", (client_id,)
                ).fetchone()
                if (
                    self.entry_epoch != epoch
                    or self.state != entry_state
                    or (kind == "PROBE" and not self.probe_active)
                    or (kind == "PROBE" and not self.entry_rate_ready())
                    or self.config.mode != "live_capable"
                    or not self.config.allow_live_orders
                    or (kind != "PROBE" and self.config.live_entry_scope != "strategy")
                    or time.monotonic() >= self.session_end
                    or not self.data_valid()
                    or not self.account
                    or not self.market
                    or current is None
                    or time.time() - current.received_at > 15
                    or current.account_type_code != self.config.expected_account_type_code
                    or not self.margin_settings_valid(current)
                    or current.position_qty
                    or current.active_orders
                    or current.other_positions
                    or current.other_assets
                    or self.manual_exposure
                    or not self.journal_ok
                    or (
                        kind == "PROBE"
                        and (
                            self.reconcile_failed
                            or time.monotonic() - self.last_reconcile_ok > 15
                            or self.last_decision.basis_bps is None
                        )
                    )
                    or sizing.quantity * sizing.price
                    > min(current.equity, current.available) - self.config.exit_reserve_usd
                    or sizing.quantity * sizing.price
                    > current.equity * self.config.max_effective_leverage
                    or not row
                    or row[0] != OrderState.INTENT
                    or self.db.flag("total_loss_stop") == "1"
                    or self.db.flag("daily_stop_day") == datetime.now(JAKARTA).date().isoformat()
                    or self.db.flag("funding_change_pending") == "1"
                    or self.db.flag("account_incident_pending") == "1"
                    or self.db.flag("close_requested") == "1"
                ):
                    return False
                self.db.order_state(client_id, OrderState.SENT)
                self.dispatching_entries.add(client_id)
                self.db.event(
                    "ENTRY_DISPATCH",
                    {
                        "client_id": client_id,
                        "kind": kind,
                        "side": side,
                        "quantity": str(sizing.quantity),
                        "quote_reference": str(best),
                        "limit_price": str(sizing.price),
                        "planned_risk": str(sizing.planned_risk),
                        "equity_before": str(current.equity),
                        "available_before": str(current.available),
                        "slippage_bps": "2",
                        "stop_distance_bps": "35",
                        "stop_limit_slippage_bps": "30",
                        "max_hold_seconds": self.config.max_hold_seconds,
                        "side_reason": "fixed BUY probe; lifecycle test, no strategy signal"
                        if kind == "PROBE"
                        else "existing strategy",
                        "spread_bps": str(spread_bps),
                        "depth_price": str(depth_price),
                        "min_base_amount": self.market["min_base_amount"],
                        "min_quote_amount": self.market["min_quote_amount"],
                        "size_decimals": self.market["size_decimals"],
                        "price_decimals": self.market["price_decimals"],
                    },
                )
                return True

            sent_at = time.monotonic()
            tx = await self.account.send_ioc(
                OrderIntent(client_id, kind, side, sizing.quantity, sizing.price, Stamp()),
                int(self.market["market_id"]),
                int(self.market["size_decimals"]),
                int(self.market["price_decimals"]),
                dispatch_check=before_dispatch,
            )
            self.db.order_state(client_id, OrderState.SENT, tx)
            self.last_order_ack_ms = (time.monotonic() - sent_at) * 1000
            await self.safe_reconcile()
        except LocalOrderRejected as exc:
            self.db.order_state(client_id, OrderState.REJECTED, reason=str(exc))
            self.db.event(
                "LOCAL_ORDER_REJECTION",
                {
                    "client_id": client_id,
                    "reason": str(exc),
                    "send_tx_called": False,
                },
            )
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "signing lokal ditolak sebelum sendTx; entry tidak diulang"
        except DispatchBlocked:
            if "client_id" in locals():
                self.db.order_state(client_id, OrderState.CANCELED, reason="local dispatch blocked")
            self.entries_stopped = True
            self.reason = "entry dibatalkan sebelum signer"
        except asyncio.CancelledError:
            if "client_id" in locals():
                state = (
                    OrderState.UNKNOWN
                    if client_id in self.dispatching_entries
                    else OrderState.CANCELED
                )
                self.db.order_state(client_id, state, reason="entry task canceled")
            raise
        except Exception as exc:
            if "client_id" in locals():
                self.db.order_state(client_id, OrderState.UNKNOWN, reason=type(exc).__name__)
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"entry tidak pasti: {type(exc).__name__}"
        finally:
            if "client_id" in locals():
                self.dispatching_entries.discard(client_id)

    async def manage_position(self) -> None:
        if not self.account_snapshot or not self.account_snapshot.position_qty:
            return
        if self.manual_exposure and self.db.flag("close_requested") != "1":
            return
        timed_out = self.position_opened_at and (
            time.monotonic() - self.position_opened_at >= self.config.max_hold_seconds
        )
        session_over = self.session_end and time.monotonic() >= self.session_end
        probing = self.probe_active or self.db.flag("probe_pending") == "1"
        probe_fill = probing and self.protection_confirmed
        protection_timeout = bool(
            not self.protection_confirmed
            and self.protection_id is not None
            and time.monotonic() - self.protect_sent_at > 8
        )
        recovered = False
        if not probing and self.data_valid() and self.last_decision.basis_bps is not None:
            deviation = self.last_decision.deviation_bps
            if deviation is not None:
                recovered = abs(deviation - self.last_decision.basis_bps) <= 2
        close_requested = self.db.flag("close_requested") == "1"
        if (
            timed_out
            or session_over
            or recovered
            or probe_fill
            or close_requested
            or protection_timeout
        ) and self.exit_id is None:
            await self.close_position()

    async def close_position(self) -> str:
        if not self.account or not self.market or not self.account_snapshot:
            return "akun tidak tersedia"
        snap = self.account_snapshot
        if not snap.position_qty:
            self.db.set_flag("close_requested", "0")
            return "tidak ada posisi menurut snapshot terakhir"
        if self.exit_id is not None:
            return "permintaan close sudah ada; menunggu rekonsiliasi"
        side = "sell" if snap.position_qty > 0 else "buy"
        quote = self.books["lighter"].quote()
        if not quote:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "book Lighter tidak valid; close belum dikirim"
            return "book Lighter tidak valid; close belum dikirim"
        best = quote.bid if side == "sell" else quote.ask
        tick = Decimal(1).scaleb(-int(self.market["price_decimals"]))
        attempts = int(self.db.flag("exit_attempts") or "0")
        if attempts >= 3:
            self.entries_stopped = True
            self.state = EngineState.HALTED
            self.reason = "tiga upaya exit selesai/tidak pasti; perlu intervensi manual"
            return self.reason
        price = limit_price(best, side, tick, (Decimal(30), Decimal(60), Decimal(100))[attempts])
        client_id = self.db.next_client_id()
        qty = abs(snap.position_qty)
        try:
            self.db.intent(client_id, "EXIT", side, str(qty), str(price))
            self.db.set_flag("exit_attempts", str(attempts + 1))
            self.db.set_flag("close_requested", "1")
            self.exit_id = client_id
            self.db.order_state(client_id, OrderState.SENT)
            sent_at = time.monotonic()
            self.db.event(
                "EXIT_DISPATCH",
                {
                    "client_id": client_id,
                    "side": side,
                    "quantity": str(qty),
                    "reference_price": str(best),
                    "limit_price": str(price),
                    "slippage_limit_bps": str((Decimal(30), Decimal(60), Decimal(100))[attempts]),
                    "reduce_only": True,
                    "attempt": attempts + 1,
                },
            )
            tx = await self.account.send_ioc(
                OrderIntent(client_id, "EXIT", side, qty, price, Stamp()),
                int(self.market["market_id"]),
                int(self.market["size_decimals"]),
                int(self.market["price_decimals"]),
            )
            self.db.order_state(client_id, OrderState.SENT, tx)
            self.last_order_ack_ms = (time.monotonic() - sent_at) * 1000
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "close dikirim; belum dikonfirmasi bursa"
            return self.reason
        except Exception as exc:
            self.db.order_state(client_id, OrderState.UNKNOWN)
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"close belum pasti: {type(exc).__name__}"
            return self.reason

    async def commands(self) -> None:
        for command_id, kind in self.db.pending_commands():
            result = ""
            if kind == "STOP_ENTRIES":
                self.entry_epoch += 1
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                result = "entry dihentikan"
            elif kind == "CANCEL_ENTRIES":
                self.entry_epoch += 1
                self.entries_stopped = True
                result = await self.cancel_entries()
            elif kind == "CLOSE_POSITION":
                self.entry_epoch += 1
                self.entries_stopped = True
                result = await self.close_position()
            elif kind == "START_LIVE":
                result = self.start_session()
            elif kind == "PROBE_ONCE":
                result = await self.probe_once()
            else:
                result = "perintah tidak dikenal"
            self.db.finish_command(command_id, result)
            self.db.event("COMMAND", {"kind": kind, "result": result})

    async def cancel_entries(self) -> str:
        self.entry_epoch += 1
        self.entries_stopped = True
        if not self.account or not self.market:
            return "akun tidak tersedia"
        active = [
            x
            for x in self.db.unresolved()
            if x["kind"] in {"ENTRY", "PROBE"}
            and x["state"] not in {OrderState.CANCEL_REQUESTED, OrderState.UNKNOWN}
        ]
        if not active:
            return "tidak ada entry aktif lokal"
        for item in active:
            client_id = item["client_id"]
            if item["state"] == OrderState.INTENT and client_id not in self.dispatching_entries:
                self.db.order_state(
                    client_id, OrderState.CANCELED, reason="local queued entry canceled"
                )
                continue
            try:
                if self.probe_active:
                    status = await self.account.order_statuses([client_id], priority="probe_read")
                else:
                    status = await self.account.order_statuses([client_id])
                order_index = int(status.get(client_id, {}).get("order_index") or 0)
                if order_index <= 0:
                    raise ValueError("entry exchange order index unavailable")
                self.db.order_state(client_id, OrderState.CANCEL_REQUESTED)
                sent_at = time.monotonic()
                await self.account.cancel(int(self.market["market_id"]), order_index)
                self.last_cancel_ack_ms = (time.monotonic() - sent_at) * 1000
            except Exception:
                self.db.order_state(client_id, OrderState.UNKNOWN)
        return "cancel diminta; hasil menunggu rekonsiliasi"

    def start_session(self) -> str:
        blockers = self.live_blockers()
        if blockers:
            return "live diblok: " + ", ".join(blockers)
        self.db.set_flag("live_ever", "1")
        self.session_end = time.monotonic() + self.config.session_seconds
        self.session_positions = 0
        self.entries_stopped = False
        self.state = EngineState.LIVE
        self.reason = "sesi live terbatas berjalan"
        return "sesi live dimulai"

    async def probe_once(self) -> str:
        blockers = self.probe_blockers()
        if blockers:
            return "probe diblok: " + ", ".join(blockers)
        self.db.set_flag("probe_used", "1")
        self.db.set_flag("probe_pending", "1")
        self.db.set_flag("live_ever", "1")
        self.probe_active = True
        self.entry_epoch += 1
        self.entries_stopped = True
        self.session_end = time.monotonic() + self.config.max_hold_seconds
        self.state = EngineState.PROBING
        self.reason = "satu probe berjalan; entry strategi tetap berhenti"
        task = asyncio.create_task(
            self.enter("buy", kind="PROBE", scheduled_epoch=self.entry_epoch)
        )
        self.entry_tasks.add(task)
        task.add_done_callback(self.entry_tasks.discard)
        return "satu probe diminta; hasilnya menunggu rekonsiliasi"

    def entry_rate_ready(self) -> bool:
        rate = getattr(self.account, "rate", None)
        return bool(
            rate is not None
            and rate.can_claim("entry", cost=2)
            and rate.can_claim("probe_read", cost=14)
        )

    def probe_read_ready(self) -> bool:
        if not self.probe_active:
            return True
        rate = getattr(self.account, "rate", None)
        if rate is None:
            return False
        pending = bool(
            (self.account_snapshot and self.account_snapshot.position_qty)
            or self.db.unresolved()
            or self.protection_id is not None
            or self.exit_id is not None
            or self.entry_tasks
            or self.dispatching_entries
        )
        return rate.can_claim("probe_read", cost=4 if pending else 8)

    def probe_blockers(self) -> list[str]:
        blockers = self.live_blockers(for_probe=True)
        if not self.entry_rate_ready():
            blockers.append("jatah pengiriman entry belum tersedia")
        if self.reconcile_task is not None and not self.reconcile_task.done():
            blockers.append("rekonsiliasi akun masih berjalan")
        if self.db.flag("probe_used") == "1":
            blockers.append("probe sudah pernah digunakan")
        if not self.entries_stopped or time.monotonic() < self.session_end:
            blockers.append("sesi lain belum selesai")
        if self.entry_tasks or self.dispatching_entries:
            blockers.append("pengiriman entry belum selesai")
        if self.manual_exposure or self.protection_id is not None or self.exit_id is not None:
            blockers.append("posisi, proteksi, atau exit belum bersih")
        if self.db.flag("close_requested") == "1":
            blockers.append("permintaan close belum selesai")
        return blockers

    def live_blockers(self, *, for_probe: bool = False) -> list[str]:
        blockers = []
        if not for_probe and self.config.live_entry_scope != "strategy":
            blockers.append("izin entry hanya untuk satu probe; START_LIVE dilarang")
        if self.config.mode != "live_capable" or not self.config.allow_live_orders:
            blockers.append("konfigurasi observe/allow_live_orders=false")
        if self.probe_active or self.db.flag("probe_pending") == "1":
            blockers.append("uji probe belum selesai")
        if not self.account or not self.account_snapshot:
            blockers.append("akun/kunci belum tersambung")
        if not self.market:
            blockers.append("metadata BTC belum tersedia")
        if self.account_snapshot and self.account_snapshot.active_orders:
            blockers.append("ada order aktif di bursa")
        if self.state == EngineState.IDLE:
            blockers.append("mode idle; entry dinonaktifkan")
        elif self.state != EngineState.READY:
            blockers.append("engine belum READY")
        if time.monotonic() - self.last_reconcile_ok > 15:
            blockers.append("rekonsiliasi akun belum segar")
        if self.account and self.reconcile_failed:
            blockers.append("rekonsiliasi akun terakhir gagal")
        if self.last_decision.basis_bps is None or not self.data_valid():
            blockers.append("data atau warm-up belum valid")
        if self.db.unresolved():
            blockers.append("order sebelumnya belum pasti")
        if self.account_snapshot and self.account_snapshot.position_qty:
            blockers.append("masih ada posisi")
        if self.account_snapshot and time.time() - self.account_snapshot.received_at > 15:
            blockers.append("snapshot akun basi")
        if self.account_snapshot and self.account_snapshot.equity <= self.config.exit_reserve_usd:
            blockers.append("equity tidak cukup untuk order dan cadangan exit")
        if self.account_snapshot and self.market and not self.account_snapshot.position_qty:
            quote = self.books["lighter"].quote()
            if quote:
                try:
                    size_entry(
                        self.config,
                        min(self.account_snapshot.equity, self.account_snapshot.available),
                        Decimal(0),
                        Decimal(0),
                        quote.ask,
                        "buy",
                        dec(self.market["min_base_amount"]),
                        dec(self.market["min_quote_amount"]),
                        int(self.market["size_decimals"]),
                        int(self.market["price_decimals"]),
                        Decimal(35),
                        Decimal(0),
                    )
                except ValueError:
                    blockers.append("ukuran minimum legal melampaui kapasitas dan cadangan")
        if Decimal(self.db.gross_deposits()) > self.config.max_funding_usd:
            blockers.append("total deposit melampaui pagu")
        if (
            self.account_snapshot
            and self.account_snapshot.equity > 0
            and Decimal(self.db.gross_deposits()) <= 0
        ):
            blockers.append("asal dana akun belum tercatat di riwayat")
        if self.db.flag("daily_stop_day") == datetime.now(JAKARTA).date().isoformat():
            blockers.append("batas loss harian sudah dipicu")
        if self.db.flag("total_loss_stop") == "1":
            blockers.append("batas loss total sudah dipicu")
        if self.db.flag("funding_change_pending") == "1":
            blockers.append("arus dana berubah setelah sesi live")
        if self.db.flag("account_incident_pending") == "1":
            blockers.append("trade akun tidak dikenal atau tidak lengkap")
        if (
            self.account_snapshot
            and self.config.expected_account_type_code != self.account_snapshot.account_type_code
        ):
            blockers.append("tier akun belum cocok dengan verifikasi lokal")
        if self.account_snapshot and not self.margin_settings_valid(self.account_snapshot):
            blockers.append("mode akun atau margin BTC belum cocok dengan verifikasi lokal")
        if self.account_snapshot and (
            self.account_snapshot.other_positions or self.account_snapshot.other_assets
        ):
            blockers.append("akun memiliki aset/posisi lain")
        if not self.journal_ok:
            blockers.append("jurnal tidak sehat")
        if (
            self.baseline
            and self.account_snapshot
            and self.baseline.check(self.config, self.account_snapshot.equity, self.current_flow)
        ):
            blockers.append("batas loss tercapai")
        return blockers

    def status(self) -> dict:
        now = time.monotonic_ns()
        quotes = {}
        for venue, book in self.books.items():
            q = book.quote()
            depth_qty = Decimal(25) / q.ask if q else Decimal(0)
            quotes[venue] = {
                "valid": bool(
                    q and now - book.updated_ns < self.config.max_data_age_ms * 1_000_000
                ),
                "bid": str(q.bid) if q else None,
                "ask": str(q.ask) if q else None,
                "age_ms": round((now - book.updated_ns) / 1e6) if book.updated_ns else None,
                "gaps": book.gaps,
                "spread_bps": str((q.ask - q.bid) / q.mid * 10000) if q else None,
                "buy_25_vwap": str(book.price_for("buy", depth_qty)) if q else None,
                "sell_25_vwap": str(book.price_for("sell", depth_qty)) if q else None,
            }
        snap = self.account_snapshot
        pending = self.db.unresolved()
        rate = getattr(self.account, "rate", None)
        if rate is not None:
            rate.can_claim("exit", cost=0)  # prune expired slots without consuming any
        pending_entry = sum(
            (
                dec(x["qty"]) * dec(x["limit_price"])
                for x in pending
                if x["kind"] in {"ENTRY", "PROBE"}
            ),
            Decimal(0),
        )
        lighter_quote = self.books["lighter"].quote()
        exposure = (
            abs(snap.position_qty) * lighter_quote.mid if snap and lighter_quote else Decimal(0)
        )
        fees = sum(
            (dec(row[0]) for row in self.db.conn.execute("SELECT fee FROM fills")), Decimal(0)
        )
        return {
            "mode": self.config.mode,
            "allow_live_orders": self.config.allow_live_orders,
            "live_entry_scope": self.config.live_entry_scope,
            "request_budget": None
            if rate is None
            else {
                "used_last_60s": len(rate.calls),
                "normal_cap": 40,
                "urgent_read_cap": 47,
                "transaction_cap": 55,
                "transaction_slots_remaining": max(0, 55 - len(rate.calls)),
            },
            "state": self.state,
            "reason": self.reason,
            "market_id": self.market["market_id"] if self.market else None,
            "quotes": quotes,
            "fx_usdcusdt": str(self.fx) if self.fx else None,
            "decision": {
                "side": self.last_decision.side,
                "deviation_bps": str(self.last_decision.deviation_bps)
                if self.last_decision.deviation_bps is not None
                else None,
                "basis_bps": str(self.last_decision.basis_bps)
                if self.last_decision.basis_bps is not None
                else None,
                "reasons": self.last_decision.reasons,
                "version": self.last_decision.version,
            },
            "account": None
            if not snap
            else {
                "equity": str(snap.equity),
                "available": str(snap.available),
                "position_qty": str(snap.position_qty),
                "position_price": str(snap.position_price),
                "unrealized_pnl": str(snap.unrealized_pnl),
                "realized_pnl": str(snap.realized_pnl),
                "funding": str(snap.funding),
                "account_type_code": snap.account_type_code,
                "account_trading_mode": snap.account_trading_mode,
                "margin_mode": snap.margin_mode,
                "initial_margin_fraction": str(snap.initial_margin_fraction)
                if snap.initial_margin_fraction is not None
                else None,
                "age_s": round(time.time() - snap.received_at, 1),
                "funding_audit_age_s": round(time.monotonic() - self.last_reconcile_ok, 1)
                if self.last_reconcile_ok
                else None,
                "daily_loss": str(self.baseline.daily_loss(snap.equity, self.current_flow))
                if self.baseline
                else None,
                "total_loss": str(self.baseline.total_loss(snap.equity, self.current_flow))
                if self.baseline
                else None,
                "source_funding_spent_usd": str(self.config.source_funding_spent_usd)
                if self.config.source_funding_spent_usd is not None
                else None,
                "source_funding_budget_remaining": str(
                    max(
                        Decimal(0),
                        self.config.max_funding_usd - self.config.source_funding_spent_usd,
                    )
                )
                if self.config.source_funding_spent_usd is not None
                else None,
                "gross_history_inflow": self.db.gross_deposits(),
                "effective_exposure_usd": str(exposure + pending_entry),
                "recorded_fees": str(fees),
            },
            "entries_stopped": self.entries_stopped,
            "session_remaining_s": max(0, round(self.session_end - time.monotonic()))
            if self.session_end
            else 0,
            "session_positions": self.session_positions,
            "probe_used": self.db.flag("probe_used") == "1",
            "probe_active": self.probe_active,
            "probe_blockers": self.probe_blockers(),
            "max_session_positions": self.config.max_session_positions,
            "pending_orders": pending,
            "protection_id": self.protection_id,
            "protection_confirmed": self.protection_confirmed,
            "manual_exposure": self.manual_exposure,
            "exit_id": self.exit_id,
            "journal_ok": self.journal_ok,
            "queue_backlog": self.queue.qsize(),
            "event_loop_lag_ms": round(self.lag_ms, 1),
            "last_order_ack_ms": round(self.last_order_ack_ms, 1)
            if self.last_order_ack_ms
            else None,
            "last_cancel_ack_ms": round(self.last_cancel_ack_ms, 1)
            if self.last_cancel_ack_ms
            else None,
            "reconnects": self.feeds.reconnects if self.feeds else {},
            "binance_streams": {
                "public": self.feeds.binance_public_ok if self.feeds else False,
                "market": self.feeds.binance_trades_ok if self.feeds else False,
            },
            "live_blockers": self.live_blockers(),
            "engine_pid": os.getpid(),
        }


async def run_engine() -> None:
    config = load_config()
    engine = Engine(config)
    await engine.run()
