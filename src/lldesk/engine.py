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

from .account import AccountSnapshot, LighterAccount
from .config import Config, load_config, repo_root
from .db import Database
from .market import FX_BOOK, Book, Feeds, GapError, Recorder, dec, market_metadata
from .models import AccountEvent, EngineState, MarketEvent, OrderIntent, OrderState, Stamp
from .risk import EquityBaseline, limit_price, size_entry
from .strategy import Decision, DeviationStrategy

LOG = logging.getLogger("lldesk.engine")
JAKARTA = ZoneInfo("Asia/Jakarta")


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
        self.session_end = 0.0
        self.session_positions = 0
        self.entries_stopped = True
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
            self.feeds = Feeds(int(self.market["market_id"]), self.queue)
            if self.config.mode == "live_capable":
                try:
                    self.account = LighterAccount(self.config)
                    await self.account.open()
                    await self.reconcile()
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
        while True:
            start = time.monotonic()
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=0.5)
                self.on_market(event)
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
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = f"feed akun perlu rekonsiliasi: {type(exc).__name__}"
            await self.commands()
            if self.account and time.monotonic() - self.last_account_poll >= 10:
                await self.safe_reconcile()
            self.decide()
            await self.manage_position()
            if self.state == EngineState.LIVE and time.monotonic() >= self.session_end:
                self.entries_stopped = True
                await self.cancel_entries()
                self.state = EngineState.PAUSED
                self.reason = "batas waktu sesi; posisi tetap dikelola"
            self.lag_ms = max(0.0, (time.monotonic() - start - 0.5) * 1000)
            try:
                self.db.save_status(self.status())
                self.journal_ok = True
            except Exception:
                self.journal_ok = False
                self.entries_stopped = True
                self.state = EngineState.HALTED
                self.reason = "jurnal/database gagal ditulis"
                LOG.exception("journal failure")

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
            client_id = (
                trade.get("ask_client_id")
                if trade.get("ask_account_id") == event.account_index
                else trade.get("bid_client_id")
            )
            if client_id is None:
                continue
            known = self.db.conn.execute(
                "SELECT 1 FROM intents WHERE client_id=?", (int(client_id),)
            ).fetchone()
            if not known:
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = "fill tanpa intent lokal; periksa transaksi manual"
            fee_micro = trade.get("taker_fee", trade.get("maker_fee", 0))
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
            and self.fx is not None
            and time.monotonic() - self.fx_at < 15
            and self.queue.qsize() < 1500
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
        if (
            self.state == EngineState.LIVE
            and not self.entries_stopped
            and self.session_positions < self.config.max_session_positions
            and self.last_decision.side
        ):
            if not self.db.unresolved() and (
                not self.account_snapshot or not self.account_snapshot.position_qty
            ):
                asyncio.create_task(self.enter(self.last_decision.side))
                self.entries_stopped = True  # prevent duplicate signal while send is in flight

    async def safe_reconcile(self) -> None:
        try:
            await self.reconcile()
        except Exception as exc:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"rekonsiliasi gagal: {type(exc).__name__}"
            LOG.warning("reconciliation blocked: %s", type(exc).__name__)

    async def reconcile(self) -> None:
        if not self.account or not self.market:
            return
        market_id = int(self.market["market_id"])
        snapshot = await self.account.snapshot(market_id)
        self.last_account_poll = time.monotonic()
        self.account_snapshot = snapshot
        if snapshot.equity > self.config.max_funding_usd:
            raise RuntimeError("dedicated account equity exceeds $50 cap")
        if snapshot.other_positions or snapshot.other_assets:
            raise RuntimeError("dedicated account has unrelated positions or assets")
        if (
            snapshot.position_qty
            and abs(snapshot.position_qty * snapshot.position_price)
            > self.config.max_entry_notional_usd
        ):
            raise RuntimeError("position exceeds $25 cap")
        last_flat = float(self.db.flag("last_flat_epoch") or "0")
        own_entry = self.db.conn.execute(
            "SELECT 1 FROM intents WHERE kind IN ('ENTRY','PROBE') "
            "AND created_at>? ORDER BY created_at DESC LIMIT 1",
            (last_flat,),
        ).fetchone()
        self.manual_exposure = bool(snapshot.position_qty and not own_entry)
        if self.manual_exposure:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "posisi tanpa entry lokal; periksa intervensi manual"
        unresolved = self.db.unresolved()
        known = {x["client_id"] for x in unresolved}
        for order in snapshot.active_orders:
            if int(order["client_order_index"]) not in known:
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = "order manual/tidak dikenal; perlu rekonsiliasi"
        statuses = await self.account.order_statuses([x["client_id"] for x in unresolved])
        for intent in unresolved:
            matched_order = statuses.get(intent["client_id"])
            if not matched_order:
                continue  # uncertain intent stays frozen
            status = str(matched_order.get("status", ""))
            filled = dec(matched_order.get("filled_base_amount", "0"))
            if status == "filled":
                new_state = OrderState.FILLED
            elif status.startswith("canceled"):
                new_state = OrderState.CANCELED
            elif status == "rejected":
                new_state = OrderState.REJECTED
            elif filled > 0:
                new_state = OrderState.PARTIAL
            elif status in {"open", "pending", "in-progress"}:
                new_state = OrderState.OPEN
            else:
                new_state = OrderState.UNKNOWN
            self.db.order_state(intent["client_id"], new_state)
        if self.protection_id is not None:
            protected = statuses.get(self.protection_id)
            self.protection_confirmed = bool(
                protected
                and protected.get("status") in {"pending", "open"}
                and bool(protected.get("reduce_only"))
            )
        self._update_baseline(snapshot)
        if self.db.flag("close_requested") == "1":
            await self.cancel_entries()
        if self.exit_id is not None and snapshot.position_qty:
            exit_status = statuses.get(self.exit_id)
            if exit_status and exit_status.get("status") in {"filled", "canceled", "rejected"}:
                self.exit_id = None
        if snapshot.position_qty != self.last_position_qty:
            if not self.last_position_qty and snapshot.position_qty:
                if not self.position_opened_at:
                    self.position_opened_at = time.monotonic()
                    self.db.set_flag("position_opened_epoch", str(time.time()))
                    self.db.set_flag("exit_attempts", "0")
            if self.last_position_qty and not snapshot.position_qty:
                self.db.set_flag("close_requested", "0")
                if self.db.flag("probe_pending") == "1":
                    self.db.set_flag("probe_pending", "0")
                else:
                    self.session_positions += 1
                self.position_opened_at = 0.0
                self.db.set_flag("position_opened_epoch", "")
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
            if stop_status and stop_status.get("status") in {"filled", "canceled"}:
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
                        await self.account.cancel(market_id, order_index)
                        self.last_cancel_ack_ms = (time.monotonic() - sent_at) * 1000
                    except Exception:
                        self.db.order_state(stop_id, OrderState.UNKNOWN)
                        self.reason = "posisi flat; cancel stop belum pasti"
        if not snapshot.position_qty and not any(
            x["kind"] in {"ENTRY", "PROBE"} for x in self.db.unresolved()
        ):
            self.db.set_flag("last_flat_epoch", str(time.time()))
        if self.state == EngineState.RECONCILING:
            self.state = EngineState.WARMING_UP
            self.reason = "rekonsiliasi selesai; menunggu data valid"
        self.last_reconcile_ok = time.monotonic()
        if (
            self.state == EngineState.PAUSED
            and not snapshot.position_qty
            and not snapshot.active_orders
            and not self.db.unresolved()
            and self.db.flag("daily_stop_day") != datetime.now(JAKARTA).date().isoformat()
            and self.db.flag("total_loss_stop") != "1"
            and self.data_valid()
            and self.last_decision.basis_bps is not None
        ):
            self.state = EngineState.READY
            self.reason = "akun dan pasar tersinkron; entry menunggu perintah"

    def _update_baseline(self, snap: AccountSnapshot) -> None:
        day = datetime.now(JAKARTA).date().isoformat()
        flow = Decimal(self.db.record_flows(snap.funding_flows))
        self.current_flow = flow
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

    async def enter(self, side: str, kind: str = "ENTRY") -> None:
        if (
            not self.account
            or not self.market
            or not self.account_snapshot
            or not self.data_valid()
        ):
            return
        snap = self.account_snapshot
        if snap.position_qty or self.db.unresolved():
            return
        quote = self.books["lighter"].quote()
        if not quote:
            return
        best = quote.ask if side == "buy" else quote.bid
        try:
            sizing = size_entry(
                self.config,
                snap.equity,
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
            self.db.order_state(client_id, OrderState.SENT)
            sent_at = time.monotonic()
            tx = await self.account.send_ioc(
                OrderIntent(client_id, kind, side, sizing.quantity, sizing.price, Stamp()),
                int(self.market["market_id"]),
                int(self.market["size_decimals"]),
                int(self.market["price_decimals"]),
            )
            self.db.order_state(client_id, OrderState.SENT, tx)
            self.last_order_ack_ms = (time.monotonic() - sent_at) * 1000
            await self.safe_reconcile()
        except Exception as exc:
            if "client_id" in locals():
                self.db.order_state(client_id, OrderState.UNKNOWN, reason=type(exc).__name__)
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"entry tidak pasti: {type(exc).__name__}"

    async def manage_position(self) -> None:
        if not self.account_snapshot or not self.account_snapshot.position_qty:
            return
        if self.manual_exposure and self.db.flag("close_requested") != "1":
            return
        timed_out = self.position_opened_at and (
            time.monotonic() - self.position_opened_at >= self.config.max_hold_seconds
        )
        session_over = self.session_end and time.monotonic() >= self.session_end
        probe_fill = self.db.flag("probe_pending") == "1"
        recovered = False
        if self.data_valid() and self.last_decision.basis_bps is not None:
            deviation = self.last_decision.deviation_bps
            if deviation is not None:
                recovered = abs(deviation - self.last_decision.basis_bps) <= 2
        close_requested = self.db.flag("close_requested") == "1"
        if (
            timed_out or session_over or recovered or probe_fill or close_requested
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
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                result = "entry dihentikan"
            elif kind == "CANCEL_ENTRIES":
                self.entries_stopped = True
                result = await self.cancel_entries()
            elif kind == "CLOSE_POSITION":
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
            try:
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
        self.session_end = time.monotonic() + self.config.session_seconds
        self.session_positions = 0
        self.entries_stopped = False
        self.state = EngineState.LIVE
        self.reason = "sesi live terbatas berjalan"
        return "sesi live dimulai"

    async def probe_once(self) -> str:
        if self.state != EngineState.LIVE or self.entries_stopped:
            return "probe diblok: sesi live belum aktif"
        if self.db.flag("probe_used") == "1":
            return "probe sudah pernah digunakan"
        if self.db.unresolved() or not self.account_snapshot or self.account_snapshot.position_qty:
            return "probe diblok: posisi/order belum flat"
        if not self.data_valid():
            return "probe diblok: data pasar belum valid"
        self.db.set_flag("probe_used", "1")
        self.db.set_flag("probe_pending", "1")
        self.entries_stopped = True
        await self.enter("buy", kind="PROBE")
        return "satu probe diminta; hasilnya menunggu rekonsiliasi"

    def live_blockers(self) -> list[str]:
        blockers = []
        if self.config.mode != "live_capable" or not self.config.allow_live_orders:
            blockers.append("konfigurasi observe/allow_live_orders=false")
        if not self.account or not self.account_snapshot:
            blockers.append("akun/kunci belum tersambung")
        if self.account_snapshot and self.account_snapshot.active_orders:
            blockers.append("ada order aktif di bursa")
        if self.state != EngineState.READY:
            blockers.append("engine belum READY")
        if time.monotonic() - self.last_reconcile_ok > 15:
            blockers.append("rekonsiliasi akun belum segar")
        if self.last_decision.basis_bps is None or not self.data_valid():
            blockers.append("data atau warm-up belum valid")
        if self.db.unresolved():
            blockers.append("order sebelumnya belum pasti")
        if self.account_snapshot and self.account_snapshot.position_qty:
            blockers.append("masih ada posisi")
        if self.account_snapshot and time.time() - self.account_snapshot.received_at > 15:
            blockers.append("snapshot akun basi")
        if self.account_snapshot and self.account_snapshot.equity > 50:
            blockers.append("equity dedicated melebihi pagu")
        if self.account_snapshot and self.account_snapshot.equity <= self.config.exit_reserve_usd:
            blockers.append("equity tidak cukup untuk order dan cadangan exit")
        if Decimal(self.db.gross_deposits()) > self.config.max_funding_usd:
            blockers.append("total deposit melampaui pagu")
        if self.db.flag("daily_stop_day") == datetime.now(JAKARTA).date().isoformat():
            blockers.append("batas loss harian sudah dipicu")
        if self.db.flag("total_loss_stop") == "1":
            blockers.append("batas loss total sudah dipicu")
        if (
            self.account_snapshot
            and self.config.expected_account_type_code != self.account_snapshot.account_type_code
        ):
            blockers.append("tier akun belum cocok dengan verifikasi lokal")
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
                "valid": book.valid,
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
                "age_s": round(time.time() - snap.received_at, 1),
                "daily_loss": str(self.baseline.daily_loss(snap.equity, self.current_flow))
                if self.baseline
                else None,
                "total_loss": str(self.baseline.total_loss(snap.equity, self.current_flow))
                if self.baseline
                else None,
                "funding_budget_remaining": str(
                    max(Decimal(0), self.config.max_funding_usd - Decimal(self.db.gross_deposits()))
                ),
                "effective_exposure_usd": str(exposure + pending_entry),
                "recorded_fees": str(fees),
            },
            "entries_stopped": self.entries_stopped,
            "session_remaining_s": max(0, round(self.session_end - time.monotonic()))
            if self.session_end
            else 0,
            "session_positions": self.session_positions,
            "probe_used": self.db.flag("probe_used") == "1",
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
            "live_blockers": self.live_blockers(),
            "engine_pid": os.getpid(),
        }


async def run_engine() -> None:
    config = load_config()
    engine = Engine(config)
    await engine.run()
