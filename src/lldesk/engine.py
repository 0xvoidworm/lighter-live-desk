from __future__ import annotations

import asyncio
import fcntl
import io
import json
import logging
import os
import time
import uuid
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .account import AccountSnapshot, DispatchBlocked, LighterAccount, LocalOrderRejected
from .audit_scheduler import AuditScheduler
from .config import Config, load_config, repo_root
from .db import Database
from .diagnostics import observe as diagnostic_observation
from .fee_audit import collect_export
from .fx import FRESH_SECONDS, FxMonitor, next_start
from .market import Book, Feeds, GapError, Recorder, dec, market_metadata
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
from .preflight import (
    HISTORY_IDLE_SECONDS,
    PREFLIGHT_TTL_SECONDS,
    Evidence,
    component_ages,
    digest,
    state_complete,
    state_digest,
)
from .risk import EquityBaseline, limit_price, size_entry
from .session import BoundedSession, parameter_hash
from .source_time import BinanceSourceClock
from .strategy import Decision, DeviationStrategy

LOG = logging.getLogger("lldesk.engine")
JAKARTA = ZoneInfo("Asia/Jakarta")
# Legacy retry/forced-refresh marker; routine audits use freshness deadlines.
ACCOUNT_RECONCILE_INTERVAL_SECONDS = 12
POSITION_READ_TIMEOUT_SECONDS = 2
STOP_INSTALL_SECONDS = 3
STOP_CONFIRM_SECONDS = 8
RISK_TRANSACTION_SECONDS = 3


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
        self.source_clock = BinanceSourceClock(config.max_data_age_ms)
        self.exit_basis_bps: Decimal | None = None
        self.account: LighterAccount | None = None
        self.account_snapshot: AccountSnapshot | None = None
        self.baseline: EquityBaseline | None = None
        self.current_flow = Decimal(0)
        self.fx: Decimal | None = None
        self.fx_at = 0.0
        self.fx_monitor = FxMonitor()
        self.last_decision = Decision(None, None, None, None, ("BELUM_ADA_DATA",))
        self.last_account_poll = 0.0
        self.last_reconcile_ok = 0.0
        self.reconcile_failed = True
        self.last_reconcile_diagnostic: dict = {}
        self.last_decision_inputs: dict = {}
        self.market_processing: dict[str, dict] = {}
        self.session_end = 0.0
        self.session_positions = 0
        self.entries_stopped = True
        self.probe_active = self.db.flag("probe_pending") == "1"
        self.last_position_qty = Decimal(0)
        self.position_opened_at = 0.0
        self.protection_id: int | None = None
        self.protection_confirmed = False
        self.protect_sent_at = 0.0
        self.unprotected_since = 0.0
        self.last_position_read_ok = 0.0
        self.last_risk_poll = 0.0
        self.risk_task: asyncio.Task | None = None
        self.risk_poll_task: asyncio.Task | None = None
        self.risk_action_task: asyncio.Task | None = None
        self.exit_id: int | None = None
        self.manual_exposure = False
        self.exchange_owned_qty = Decimal(0)
        self.lag_ms = 0.0
        self.last_order_ack_ms: float | None = None
        self.last_cancel_ack_ms: float | None = None
        self.journal_ok = True
        self.tasks: list[asyncio.Task] = []
        self.reconcile_task: asyncio.Task | None = None
        self.reconcile_lock = asyncio.Lock()
        self.audit_scheduler = AuditScheduler()
        self.state_scheduler = AuditScheduler()
        self.state_scheduler.request_cost = 2
        self.state_refresh_task: asyncio.Task | None = None
        self.state_refresh_failed = True
        self.last_state_diagnostic: dict = {}
        self.history_required = True
        self.history_reason = "startup: complete REST audit required"
        self.audit_binding: str | None = None
        self.preflight_generation = 0
        self.preflight_boot_id = uuid.uuid4().hex
        self.preflight_evidence: Evidence | None = None
        self.preflight_attempt: str | None = None
        self.preflight_in_progress = False
        self.last_preflight_diagnostic: dict = {}
        self._reconcile_owner: asyncio.Task | None = None
        self.entry_epoch = 0
        self.entry_tasks: set[asyncio.Task] = set()
        self.dispatching_entries: set[int] = set()
        self.probe_preflight_fingerprint: dict | None = None
        self.probe_preflight_equity: Decimal | None = None
        self.session = BoundedSession(self.db)
        # Volatile admission: restart never implicitly loads entry permission.
        self.loaded_session_permission_id: str | None = None
        self.session_cleanup_task: asyncio.Task | None = None
        self.fee_audit_task: asyncio.Task | None = None
        self.last_session_sample = 0.0
        self.last_complete_snapshot: AccountSnapshot | None = None
        record = self.session.read()
        if record and record["status"] in {"active", "ending"}:
            self.session_end = time.monotonic() + max(0, record["deadline_epoch"] - time.time())
            self.session.stop("restart: entry tidak dilanjutkan otomatis")
            self.db.set_flag("close_requested", "1")
        self._restore_order_ids()

    def _restore_order_ids(self) -> None:
        for item in self.db.unresolved():
            if item["kind"] == "STOP":
                self.protection_id = item["client_id"]
            elif item["kind"] == "EXIT":
                self.exit_id = item["client_id"]
        opened = self.db.flag("position_opened_epoch")
        if opened:
            # Downtime / wall-clock jumps must never grant a fresh holding period.
            self.position_opened_at = time.monotonic() - self.config.max_hold_seconds
        if self.protection_id is not None:
            self.unprotected_since = time.monotonic() - STOP_CONFIRM_SECONDS
            self.protect_sent_at = self.unprotected_since
        if self.probe_active:
            self.db.set_flag("close_requested", "1")

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
                asyncio.create_task(
                    self.source_clock.run(self.config.binance_dns_mode, self.record_source_clock)
                ),
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
            if self.state_refresh_task:
                self.tasks.append(self.state_refresh_task)
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
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(3):
                await self.fx_monitor.request(client, metadata=True)
                if self.fx_monitor.pair_verified:
                    break
                await asyncio.sleep(60)
            if not self.fx_monitor.pair_verified:
                return
            due = time.monotonic()
            while True:
                await asyncio.sleep(max(0, due - time.monotonic()))
                observation = await self.fx_monitor.request(client)
                if observation:
                    self.fx = observation.mid
                    # Start is conservative; late body/parsing cannot make old data fresh.
                    self.fx_at = observation.started.monotonic_ns / 1e9
                    try:
                        self.recorder.write(
                            MarketEvent(
                                "binance",
                                "fx",
                                {
                                    "symbol": "USDCUSDT",
                                    "rate": str(self.fx),
                                    "bid": str(observation.bid),
                                    "ask": str(observation.ask),
                                    "bid_qty": str(observation.bid_qty),
                                    "ask_qty": str(observation.ask_qty),
                                    "request_started_ns": observation.started.receipt_ns,
                                    "verified_monotonic_ns": observation.verified_ns,
                                    "price_changed_ns": observation.price_changed_ns,
                                    "source": "Binance Spot REST Memory",
                                },
                                observation.received,
                            )
                        )
                    except Exception:
                        self.fx = None
                        self.journal_ok = False
                        self.fx_monitor.counts["record_failures"] += 1
                        self.invalidate_preflight("FX recording failed", history=False)
                else:
                    self.fx = None
                    self.invalidate_preflight("FX snapshot failed/unverified", history=False)
                # Fixed starts, skip missed intervals; no catch-up burst or blind retry.
                due = next_start(due, time.monotonic())

    async def loop(self) -> None:
        self.risk_task = asyncio.create_task(self.risk_loop())
        try:
            await self._loop()
        finally:
            workers = [
                t
                for t in (
                    self.reconcile_task,
                    self.risk_task,
                    self.risk_poll_task,
                    self.risk_action_task,
                    self.session_cleanup_task,
                    self.fee_audit_task,
                )
                if t is not None
            ]
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

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
                self.invalidate_preflight("market sequence gap", history=True)
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
                and not self.preflight_in_progress
                and not (self.probe_active and self.entry_tasks)
                and not self.entry_tasks
                and (self.fee_audit_task is None or self.fee_audit_task.done())
                and time.monotonic() >= self.audit_scheduler.next_due
                and not self.risk_needs_read()
                and self.account.rate.can_claim("reconcile", self.audit_scheduler.request_cost)
                and (self.reconcile_task is None or self.reconcile_task.done())
                and (self.state_refresh_task is None or self.state_refresh_task.done())
            ):
                self.reconcile_task = asyncio.create_task(self.safe_reconcile())
            elif (
                self.account
                and not self.preflight_in_progress
                and self.last_reconcile_ok
                and not self.risk_needs_read()
                and not self.entry_tasks
                and (self.reconcile_task is None or self.reconcile_task.done())
                and (self.state_refresh_task is None or self.state_refresh_task.done())
                and time.monotonic() >= self.state_scheduler.next_due
                and self.account.rate.can_claim("reconcile", 2)
            ):
                self.state_refresh_task = asyncio.create_task(self.refresh_account_state())
            self.decide()
            self.session_tick()
            if self.probe_active and self.session_end and time.monotonic() >= self.session_end:
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
        self.invalidate_preflight("feed recovery", history=True)
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
        started = time.monotonic_ns()
        book = self.books.get(event.venue)
        previous_stamp = book.stamp.monotonic_ns if book else None
        outcome = "error"
        try:
            self._apply_market(event)
            outcome = (
                "applied"
                if book
                and book.stamp.monotonic_ns == event.stamp.monotonic_ns
                and book.stamp.monotonic_ns != previous_stamp
                else "duplicate_or_old_sequence"
                if event.kind in {"depthUpdate", "order_book"}
                else "disconnect"
                if event.kind == "disconnect"
                else "diagnostic_or_trade"
            )
        finally:
            finished = time.monotonic_ns()
            key = event.venue + ":" + event.kind
            record = self.market_processing.setdefault(key, {"processed": 0})
            previous_receipt = record.get("receipt_monotonic_ns")
            gap = (
                (event.stamp.monotonic_ns - previous_receipt) / 1e9
                if previous_receipt is not None
                else 0
            )
            record["processed"] += 1
            record[outcome] = record.get(outcome, 0) + 1
            record["max_receipt_gap_s"] = max(record.get("max_receipt_gap_s", 0), gap)
            receipt_delay = (started - event.stamp.monotonic_ns) / 1e6
            record["max_receipt_to_processing_ms"] = max(
                record.get("max_receipt_to_processing_ms", 0), receipt_delay
            )
            processing = (finished - started) / 1e6
            record["max_processing_ms"] = max(record.get("max_processing_ms", 0), processing)
            record["processing_ms"] = processing
            if event.queued_monotonic_ns is not None:
                parsing = (event.queued_monotonic_ns - event.stamp.monotonic_ns) / 1e6
                waiting = (started - event.queued_monotonic_ns) / 1e6
                record["max_receive_to_enqueue_ms"] = max(
                    record.get("max_receive_to_enqueue_ms", 0), parsing
                )
                record["max_queue_wait_ms"] = max(record.get("max_queue_wait_ms", 0), waiting)
                record["queued_monotonic_ns"] = event.queued_monotonic_ns
            record.update(
                receipt_ns=event.stamp.receipt_ns,
                receipt_monotonic_ns=event.stamp.monotonic_ns,
                source_exchange_ms=event.stamp.exchange_ms,
                processing_started_monotonic_ns=started,
                processing_finished_monotonic_ns=finished,
                quote_monotonic_ns=book.stamp.monotonic_ns if book else None,
                outcome=outcome,
            )

    def _apply_market(self, event: MarketEvent) -> None:
        self.recorder.write(event)
        book = self.books.get(event.venue)
        if not book:
            return
        if event.kind == "disconnect":
            self.invalidate_preflight("market reconnect: " + event.venue, history=True)
            channel = str(event.payload.get("channel", "unknown"))
            self.strategy.interrupted(event.venue, channel, str(event.payload.get("reason", "")))
            # Loss of the trade channel does not invalidate an independent public book.
            # data_valid still requires BOTH Binance channels to recover before sampling.
            if not (event.venue == "binance" and channel == "market"):
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
            self.invalidate_preflight("account reconnect", history=True)
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
            self.invalidate_preflight("account trade update", history=True)
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
        inserted = self.db.add_fill(
            str(trade["trade_id"]),
            int(client_id),
            str(trade["size"]),
            str(trade["price"]),
            str(dec(fee_micro) / Decimal(1_000_000)) if fee_micro is not None else None,
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
                    "fee_pending": fee_micro is None,
                },
            )
            self.entries_stopped = True
            if self.probe_active:
                self._start_position_deadline()

    def market_data_valid(self) -> bool:
        """Existing receipt/transport guards, also used for reducing exposure."""
        now = time.monotonic_ns()
        max_age = self.config.max_data_age_ms * 1_000_000
        return (
            all(book.valid and now - book.updated_ns < max_age for book in self.books.values())
            and self.books["binance"].bridged
            and self.feeds is not None
            and self.feeds.binance_public_ok
            and self.feeds.binance_trades_ok
            and self.fx is not None
            and time.monotonic() - self.fx_at < FRESH_SECONDS
            and self.queue.qsize() < 1500
        )

    def source_freshness(self) -> dict:
        return self.source_clock.assess(self.books["binance"].stamp)

    def data_valid(self) -> bool:
        """Entry and strategy sampling: source age is an additional guard."""
        return self.market_data_valid() and self.source_freshness()["passed"]

    def record_source_clock(self, proof: dict) -> None:
        try:
            self.recorder.write(MarketEvent("binance", "source_clock", proof, Stamp()))
        except Exception:
            self.journal_ok = False
            self.source_clock.evidence = None
            self.invalidate_preflight("bukti clock tidak dapat disimpan")
            raise

    def idle_valid(self) -> bool:
        """Flat monitoring; closed/disarmed sessions display feed readiness separately."""
        snap = self.account_snapshot
        now = time.monotonic()
        record = self.session.read()
        closed_session_flat = bool(
            record and record["status"] == "closed" and self.session_flat_clean()
        )
        return bool(
            not self.config.allow_live_orders
            and self.entries_stopped
            and not self.probe_active
            and now >= self.session_end
            and self.account
            and snap
            and not self.reconcile_failed
            and self.history_valid()
            and self.account_state_valid()
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
            and (
                closed_session_flat
                or (self.data_valid() and self.last_decision.basis_bps is not None)
            )
        )

    def decide(self) -> None:
        lighter, binance = self.books["lighter"].quote(), self.books["binance"].quote()
        valid = self.data_valid()
        self.last_decision_inputs = {
            "evaluated_at": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            "data_valid": valid,
            "source_freshness": self.source_freshness(),
            "fx": str(self.fx) if self.fx is not None else None,
            "fx_age_s": time.monotonic() - self.fx_at if self.fx is not None else None,
            "fx_observation": self.fx_monitor.status()["observation"],
            "queue_backlog": self.queue.qsize(),
            "quotes": {
                name: {
                    "bid": str(q.bid),
                    "ask": str(q.ask),
                    "receipt_ns": q.stamp.receipt_ns,
                    "monotonic_ns": q.stamp.monotonic_ns,
                    "exchange_ms": q.stamp.exchange_ms,
                    "exchange_event_ms": q.stamp.exchange_event_ms,
                }
                if q
                else None
                for name, q in (("lighter", lighter), ("binance", binance))
            },
        }
        self.last_decision = self.strategy.evaluate(lighter, binance, valid, self.fx)
        if valid and self.last_decision.basis_bps is not None:
            self.exit_basis_bps = self.last_decision.basis_bps
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
                record = self.session.read()
                self.reason = (
                    "standby; sesi selesai, akun/order bersih, izin order mati"
                    if record and record["status"] == "closed"
                    else "standby; akun flat, data segar, entry dinonaktifkan"
                )
            elif self.state == EngineState.IDLE:
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                self.reason = "idle ditahan; akun, pasar, atau jurnal belum valid"
        if (
            self.state == EngineState.LIVE
            and not self.entries_stopped
            and self.session_entry_ready()
            and self.last_decision.side
        ):
            if (
                not self.entry_tasks
                and not self.dispatching_entries
                and not self.db.unresolved()
                and (not self.account_snapshot or not self.account_snapshot.position_qty)
            ):
                task = asyncio.create_task(
                    self.enter(self.last_decision.side, scheduled_epoch=self.entry_epoch)
                )
                self.entry_tasks.add(task)
                task.add_done_callback(self.entry_tasks.discard)
                self.entries_stopped = True  # prevent duplicate signal while send is in flight

    async def safe_reconcile(self, *, reserved: bool = False) -> None:
        task = asyncio.current_task()
        if (
            self._reconcile_owner
            and self._reconcile_owner is not task
            and not self._reconcile_owner.done()
        ):
            await asyncio.shield(self._reconcile_owner)
            return  # Shared result, never a second queued full audit.
        self._reconcile_owner = task
        self.audit_scheduler.refresh_requested = False
        started = time.monotonic()
        generation = self.preflight_generation
        acquired: float | None = None
        trace = getattr(self.account, "transport_trace", None)
        trace_start = trace.sequence if trace else 0
        task_id = id(asyncio.current_task())
        outcome, error = "cancelled", None
        rate = getattr(self.account, "rate", None)
        deadline = 8.0
        try:
            async with self.reconcile_lock:
                acquired = time.monotonic()
                self.last_account_poll = time.monotonic()
                deadline = 8.0
                with (
                    (
                        rate.reserve("reconcile", self.audit_scheduler.request_cost)
                        if rate and hasattr(rate, "reserve") and not reserved
                        else nullcontext()
                    ),
                    (
                        trace.operation("account_reconciliation", "reconcile", 1)
                        if trace
                        else nullcontext()
                    ),
                ):
                    async with asyncio.timeout(deadline):
                        await self.reconcile()
                self.reconcile_failed = False
                outcome = "ok"
        except Exception as exc:
            self.invalidate_preflight("complete audit failed", history=True)
            outcome, error = "error", type(exc).__name__
            self.reconcile_failed = True
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"rekonsiliasi gagal: {type(exc).__name__}"
            LOG.warning("reconciliation blocked: %s", type(exc).__name__)
            self.end_session("rekonsiliasi gagal: " + type(exc).__name__)
        finally:
            rate = getattr(self.account, "rate", None)
            requests = (
                [r for r in trace.completed if r["id"] > trace_start and r["task"] == task_id]
                if trace
                else []
            )
            self.last_reconcile_diagnostic = {
                "finished_at": time.time(),
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "lock_wait_ms": ((acquired or time.monotonic()) - started) * 1000,
                "request_deadline_s": deadline,
                "outcome": outcome,
                "error": error,
                "requests": requests,
                "request_used_60s": len(rate.calls) if rate else None,
            }
            if outcome == "ok":
                self.audit_scheduler.request_cost = max(
                    self.audit_scheduler.request_cost, len(requests)
                )
                if self.last_reconcile_ok >= started and generation == self.preflight_generation:
                    self.history_required = False
                    self.history_reason = "complete audit verified; idle periodic deadline 120s"
                    self.audit_binding = self.preflight_binding()
                    self.audit_scheduler.next_due = time.monotonic() + HISTORY_IDLE_SECONDS
                    self.audit_scheduler.reason = self.history_reason
                    self.state_refresh_failed = False
                    self.state_scheduler.completed(
                        time.monotonic(), self.state_age(), time.monotonic() - started, 2
                    )
                else:
                    self.history_required = True
                    self.audit_scheduler.request_now(
                        "exposure-only or superseded audit; recovery continues"
                    )
            else:
                self.history_required = True
                self.reconcile_failed = True
                self.preflight_evidence = None
                self.audit_scheduler.next_due = (
                    time.monotonic() + ACCOUNT_RECONCILE_INTERVAL_SECONDS
                )
                self.audit_scheduler.reason = (
                    "failed/cancelled audit; bounded retry, old data stays old"
                )
            self._reconcile_owner = None
            self.db.event("ACCOUNT_RECONCILE_DIAGNOSTIC", self.last_reconcile_diagnostic)

    def invalidate_preflight(self, reason: str, *, history: bool = False) -> None:
        self.preflight_generation += 1
        self.preflight_evidence = None
        if history:
            self.history_required = True
            self.history_reason = reason
            self.audit_scheduler.request_now(reason)

    def account_state_valid(self) -> bool:
        return not self.state_refresh_failed and state_complete(self.account_snapshot)

    def state_age(self) -> float:
        return max(
            (age if age is not None else 15.0)
            for age in component_ages(self.account_snapshot).values()
        )

    def history_valid(self) -> bool:
        return bool(
            self.last_reconcile_ok and not self.reconcile_failed and not self.history_required
        )

    def preflight_binding(self) -> str:
        return digest(
            {
                "boot_id": self.preflight_boot_id,
                "account_index": self.config.account_index,
                "risk": parameter_hash(self.config),
                "history": self.db.accounting_fingerprint(),
                "checkpoint": self.db.flag("last_trade_id"),
                "fee_reports": self.db.conn.execute(
                    "SELECT key,value FROM flags WHERE key LIKE 'fee_report:%' ORDER BY key"
                ).fetchall(),
                "cash_proof": self.db.conn.execute(
                    "SELECT id,body FROM events WHERE kind='NET_CASH_AUDIT' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone(),
                "state": state_digest(self.account_snapshot) if self.account_snapshot else None,
            }
        )

    async def refresh_account_state(self) -> None:
        if not self.account or not self.market:
            return
        started = time.monotonic()
        trace = getattr(self.account, "transport_trace", None)
        seq = trace.sequence if trace else 0
        task_id = id(asyncio.current_task())
        outcome, error = "error", None
        try:
            with (
                self.account.rate.reserve("reconcile", 2),
                (
                    trace.operation("account_state_refresh", "reconcile", 1)
                    if trace
                    else nullcontext()
                ),
            ):
                async with asyncio.timeout(self.state_scheduler.timeout(self.state_age())):
                    snap = await self.account.state_snapshot(
                        int(self.market["market_id"]), self.account_snapshot
                    )
            if not state_complete(snap):
                raise RuntimeError("account components incomplete/stale/inconsistent")
            if self.account_snapshot and snap.received_at < self.account_snapshot.received_at:
                return
            if self.account_snapshot and state_digest(snap) != state_digest(self.account_snapshot):
                self.invalidate_preflight("account state/funds changed", history=True)
            self.account_snapshot = snap
            self.last_position_read_ok = time.monotonic()
            self.state_refresh_failed = False
            outcome = "ok"
            self.state_scheduler.completed(
                time.monotonic(), self.state_age(), time.monotonic() - started, 2
            )
        except Exception as exc:
            error = type(exc).__name__
            self.state_refresh_failed = True
            self.invalidate_preflight("account state refresh failed", history=True)
            self.entries_stopped = True
            self.state_scheduler.next_due = time.monotonic() + 2
        finally:
            self.last_state_diagnostic = {
                "finished_at": time.time(),
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "outcome": outcome,
                "error": error,
                "requests": [r for r in trace.completed if r["id"] > seq and r["task"] == task_id]
                if trace
                else [],
            }
            self.db.event("ACCOUNT_STATE_DIAGNOSTIC", self.last_state_diagnostic)

    def audit_account_age(self) -> float | None:
        snap = self.account_snapshot
        if snap is None:
            return None
        if snap.received_monotonic:
            return max(0.0, time.monotonic() - snap.received_monotonic)
        return max(0.0, time.time() - snap.received_at)  # Legacy/isolated fixtures only.

    def risk_needs_read(self) -> bool:
        return bool(
            self.db.unresolved()
            or self.entry_tasks
            or self.protection_id is not None
            or self.exit_id is not None
            or self.exchange_owned_qty
            or (self.account_snapshot and self.account_snapshot.position_qty)
        )

    def _start_position_deadline(self) -> None:
        if not self.position_opened_at:
            last_flat = float(self.db.flag("last_flat_epoch") or "0")
            row = self.db.conn.execute(
                "SELECT MIN(created_at) FROM intents WHERE kind IN ('ENTRY','PROBE') "
                "AND created_at>?",
                (last_flat,),
            ).fetchone()
            epoch = float(row[0]) if row and row[0] is not None else time.time()
            age = time.time() - epoch
            if age < 0:  # Wall clock moved backward: reduce risk immediately.
                age = self.config.max_hold_seconds
            self.position_opened_at = time.monotonic() - age
            self.db.set_flag("position_opened_epoch", str(epoch))
        if not self.unprotected_since:
            self.unprotected_since = time.monotonic()

    def _own_quantity(self) -> Decimal:
        last_flat = float(self.db.flag("last_flat_epoch") or "0")
        return sum(
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

    async def recover_exposure(self, *, priority: str = "probe_read") -> None:
        """Priority position/order evidence; never awaits fee/funding history or its lock."""
        if not self.account or not self.market:
            return
        try:
            async with asyncio.timeout(POSITION_READ_TIMEOUT_SECONDS):
                snap = await self.account.position_snapshot(
                    int(self.market["market_id"]), self.account_snapshot, priority=priority
                )
            if self.account_snapshot and snap.received_at < self.account_snapshot.received_at:
                return
            if self.account_snapshot and state_digest(snap) != state_digest(self.account_snapshot):
                self.invalidate_preflight(
                    "exposure/lifecycle changed; full audit after cleanup", history=True
                )
            self.account_snapshot = snap
            self.last_position_read_ok = time.monotonic()
            if snap.position_qty:
                self._start_position_deadline()
                # A validated position is already exchange evidence of exposure;
                # an entry ACK/history wait must release the signer for protection.
                for task in tuple(self.entry_tasks):
                    task.cancel()
            self.manual_exposure = bool(
                snap.position_qty and self._own_quantity() != snap.position_qty
            )
            last_flat = float(self.db.flag("last_flat_epoch") or "0")
            cycle = self.db.conn.execute(
                "SELECT client_id,side FROM intents WHERE created_at>? "
                "AND kind IN ('ENTRY','PROBE','STOP','EXIT')",
                (last_flat,),
            ).fetchall()
            ids = sorted({cid for cid, _ in cycle} | {x["client_id"] for x in self.db.unresolved()})
            async with asyncio.timeout(POSITION_READ_TIMEOUT_SECONDS):
                statuses = await self.account.order_statuses(ids, priority=priority)
            for item in self.db.unresolved():
                order = statuses.get(item["client_id"])
                if order:
                    self.db.order_state(
                        item["client_id"],
                        normalize_order_state(
                            str(order.get("status", "")), dec(order.get("filled_base_amount", "0"))
                        ),
                    )
            # Cumulative exchange fills prove size even before fee-bearing trades arrive.
            cycle_complete = bool(cycle) and all(
                cid in statuses and statuses[cid].get("filled_base_amount") is not None
                for cid, _ in cycle
            )
            if cycle_complete:
                owned = sum(
                    (
                        dec(statuses[cid].get("filled_base_amount", "0"))
                        * (1 if side == "buy" else -1)
                        for cid, side in cycle
                    ),
                    Decimal(0),
                )
                self.exchange_owned_qty = owned
                self.manual_exposure = bool(snap.position_qty and owned != snap.position_qty)
            flat_verified = not snap.position_qty and (
                (cycle_complete and not self.exchange_owned_qty)
                or (not cycle and not self._own_quantity() and not self.db.unresolved())
            )
            old_confirmed = self.protection_confirmed
            if self.protection_id is not None:
                stop = statuses.get(self.protection_id)
                self.protection_confirmed = self.stop_covers_position(stop, snap)
                if self.protection_confirmed and not old_confirmed:
                    self.db.event(
                        "PROTECTION_CONFIRMED",
                        self.protection_evidence(stop, snap),
                    )
                if old_confirmed and not self.protection_confirmed and snap.position_qty:
                    self.db.set_flag("close_requested", "1")
                    self.end_session("proteksi exchange hilang/tidak cocok")
                if flat_verified and stop:
                    state = normalize_order_state(str(stop.get("status", "")))
                    if state in TERMINAL_ORDER_STATES:
                        self.protection_id = None
                    elif stop.get("status") in {"pending", "open"}:
                        await self._cancel_flat_stop(stop)
            if self.exit_id is not None:
                order = statuses.get(self.exit_id)
                if (
                    order
                    and normalize_order_state(str(order.get("status", ""))) in TERMINAL_ORDER_STATES
                ):
                    if flat_verified:
                        self.exit_id = None
                    elif not self.manual_exposure:
                        # Post-terminal account read is required before a partial-close retry.
                        seen = float(self.db.flag("exit_terminal_seen_epoch") or "0")
                        if seen and snap.received_at >= seen:
                            self.exit_id = None
                            self.db.set_flag("exit_terminal_seen_epoch", "")
                        else:
                            self.db.set_flag("exit_terminal_seen_epoch", str(time.time()))
        except Exception as exc:
            self.entries_stopped = True
            self.reason = f"pemulihan exposure tertahan: {type(exc).__name__}"
            self.db.event("EXPOSURE_RECOVERY_FAILED", {"error_type": type(exc).__name__})
            self.end_session("pemulihan exposure gagal: " + type(exc).__name__)

    async def _cancel_flat_stop(self, stop: dict) -> None:
        if self.protection_id is None or not self.account or not self.market:
            return
        cid = self.protection_id
        row = self.db.conn.execute("SELECT state FROM intents WHERE client_id=?", (cid,)).fetchone()
        if row and row[0] == OrderState.CANCEL_REQUESTED:
            return
        if self.db.flag(f"cancel_submitted:{cid}") == "1":
            return
        index = int(stop.get("order_index") or 0)
        if index <= 0:
            return
        self.db.order_state(cid, OrderState.CANCEL_REQUESTED)
        self.db.set_flag(f"cancel_submitted:{cid}", "1")
        try:
            tx = await self.account.cancel(int(self.market["market_id"]), index)
            self.db.event("CANCEL_ACK", {"client_id": cid, "order_index": index, "tx_hash": tx})
        except (Exception, asyncio.CancelledError) as exc:
            self.db.order_state(cid, OrderState.UNKNOWN)
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def risk_loop(self) -> None:
        while True:
            self.session_tick(risk_only=True)
            now = time.monotonic()
            snap = self.account_snapshot
            urgent = bool(
                self.entry_tasks
                or any(x["kind"] in {"ENTRY", "PROBE"} for x in self.db.unresolved())
                or self.exit_id is not None
                or self.db.flag("close_requested") == "1"
                or (
                    snap
                    and snap.position_qty
                    and (
                        not self.protection_confirmed
                        or now - self.position_opened_at >= self.config.max_hold_seconds - 10
                    )
                )
            )
            priority = "probe_read" if urgent else "reconcile"
            needs_read = bool(
                self.account
                and (
                    self.db.unresolved()
                    or self.entry_tasks
                    or self.protection_id is not None
                    or self.exit_id is not None
                    or (self.account_snapshot and self.account_snapshot.position_qty)
                    or self.exchange_owned_qty
                    or self._own_quantity()
                )
            )
            if (
                needs_read
                and now - self.last_risk_poll >= 2
                and (self.risk_poll_task is None or self.risk_poll_task.done())
                and self.account
                and self.account.rate.can_claim(priority, cost=2)
            ):
                self.last_risk_poll = now
                self.risk_poll_task = asyncio.create_task(self.recover_exposure(priority=priority))
            if snap and snap.position_qty:
                self._start_position_deadline()
                unprotected = not self.protection_confirmed
                overdue = bool(
                    now - self.position_opened_at
                    >= self.config.max_hold_seconds - RISK_TRANSACTION_SECONDS
                    or (unprotected and now - self.unprotected_since >= STOP_CONFIRM_SECONDS)
                    or (
                        unprotected
                        and self.protection_id is None
                        and now - self.unprotected_since >= STOP_INSTALL_SECONDS
                    )
                )
                if overdue:
                    if unprotected:
                        self.end_session("deadline proteksi terlewati")
                    self.entries_stopped = True
                    self.db.set_flag("close_requested", "1")
                    # Release queued/hung entry/stop operations; never replay their intent.
                    if self.exit_id is None:
                        for task in self.entry_tasks:
                            task.cancel()
                        if self.risk_action_task and not self.risk_action_task.done():
                            self.risk_action_task.cancel()
                if self.risk_action_task is None or self.risk_action_task.done():
                    self.risk_action_task = asyncio.create_task(self.manage_position())
            await asyncio.sleep(0.05)

    async def reconcile(self) -> None:
        if not self.account or not self.market:
            return
        market_id = int(self.market["market_id"])
        exposure_only = bool(
            not self.preflight_in_progress
            and (self.probe_active or self.session_open())
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
        if self.probe_active or self.session_open():
            flows = (
                self.account_snapshot.funding_flows
                if exposure_only and self.account_snapshot
                else None
            )
            snapshot = await self.account.probe_snapshot(market_id, flows, priority="reconcile")
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
        fee_lookup_ids = self.db.fee_lookup_candidates()
        pending_fee_id = min(map(int, fee_lookup_ids)) if fee_lookup_ids else None
        if pending_fee_id is not None:
            after_id = min(after_id, int(pending_fee_id) - 1)
        if self.probe_active:
            statuses = await self.account.order_statuses(client_ids, priority="reconcile")
            trades = await self.account.recent_trades(market_id, after_id, priority="reconcile")
        else:
            statuses = await self.account.order_statuses(client_ids)
            trades = await self.account.recent_trades(market_id, after_id)
        for trade in reversed(trades):
            self._record_trade(trade, self.account.account_index)
        self.db.fee_lookup_completed(fee_lookup_ids)
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
        if self.account_snapshot and self.account_snapshot.received_at > snapshot.received_at:
            self.last_account_poll = time.monotonic() - ACCOUNT_RECONCILE_INTERVAL_SECONDS
            self.audit_scheduler.request_now("bookkeeping response older than risk exposure")
            return  # Slow bookkeeping must not overwrite newer risk exposure.
        self.account_snapshot = snapshot
        self.last_position_read_ok = time.monotonic()
        self.manual_exposure = bool(snapshot.position_qty and own_qty != snapshot.position_qty)
        if self.probe_active and own_qty != snapshot.position_qty:
            # A fill can arrive after the account GET. Do not declare flat or
            # retry a filled close using that older snapshot; fetch again promptly.
            self.last_account_poll = time.monotonic() - ACCOUNT_RECONCILE_INTERVAL_SECONDS
            self.audit_scheduler.request_now(
                "position and ledger differ; priority recovery continues"
            )
        if self.manual_exposure:
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "posisi tidak cocok dengan fill lokal; periksa intervensi manual"
            self.end_session("posisi tidak cocok dengan ledger")
        if self.protection_id is not None:
            protected = statuses.get(self.protection_id)
            previously_confirmed = self.protection_confirmed
            self.protection_confirmed = self.stop_covers_position(protected, snapshot)
            if self.protection_confirmed and not previously_confirmed:
                self.db.event(
                    "PROTECTION_CONFIRMED",
                    self.protection_evidence(protected, snapshot),
                )
            if previously_confirmed and not self.protection_confirmed and not self.manual_exposure:
                self.entries_stopped = True
                self.db.set_flag("close_requested", "1")
                if snapshot.position_qty:
                    self.end_session("proteksi exchange hilang/tidak cocok")
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
                    self._start_position_deadline()
                    if not self.db.flag("exit_attempts"):
                        self.db.set_flag("exit_attempts", "0")
            if self.last_position_qty and not snapshot.position_qty and not own_qty:
                self.db.set_flag("close_requested", "0")
                if self.db.flag("probe_pending") != "1":
                    self.session_positions += 1
                self.position_opened_at = 0.0
                self.unprotected_since = 0.0
                self.db.set_flag("position_opened_epoch", "")
                self.db.set_flag("exit_attempts", "0")
            self.last_position_qty = snapshot.position_qty
        if (
            snapshot.position_qty
            and not self.manual_exposure
            and self.protection_id is None
            and self.exit_id is None
        ):
            self._start_position_deadline()
            # Risk worker owns transaction dispatch outside the accounting timeout.
        elif (
            snapshot.position_qty
            and self.protection_id is not None
            and not self.protection_confirmed
            and self.exit_id is None
            and not self.manual_exposure
            and time.monotonic() - self.protect_sent_at > 8
        ):
            self.reason = "stop tidak terkonfirmasi; meminta tutup posisi"
            self.db.set_flag("close_requested", "1")
        elif not snapshot.position_qty and not own_qty and self.exit_id is not None:
            self.reason = "posisi flat menurut bursa; entry tetap berhenti"
            self.exit_id = None
            self.db.set_flag("close_requested", "0")
        if not snapshot.position_qty and not own_qty and self.protection_id is not None:
            stop_status = statuses.get(self.protection_id)
            if (
                stop_status
                and normalize_order_state(str(stop_status.get("status", "")))
                in TERMINAL_ORDER_STATES
            ):
                self.protection_id = None
                self.protection_confirmed = False
            elif stop_status and stop_status.get("status") in {"pending", "open"}:
                await self._cancel_flat_stop(stop_status)
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
            and (
                not self.db.pending_fees()
                or (
                    self.config.probe_accept_reported_fees
                    and self.db.net_cash_status(str(snapshot.equity), "0")["status"] == "matched"
                )
            )
        ):
            self.probe_active = False
            self.db.set_flag("probe_pending", "0")
            self.db.set_flag("close_requested", "0")
            self.session_end = 0.0
            self.entries_stopped = True
            self.db.event(
                "PROBE_OPERATIONAL_COMPLETE",
                {
                    "session_id": self.db.flag("probe_session_id"),
                    "position_qty": "0",
                    "all_active_orders": 0,
                    "fee_exact_unknown_count": self.db.pending_fees(),
                    "cash_audit": self.db.net_cash_status(str(snapshot.equity), "0"),
                },
            )
            if self.state != EngineState.HALTED:
                self.state = EngineState.PAUSED
                self.reason = "probe selesai menurut rekonsiliasi; entry strategi tetap berhenti"
        if self.state == EngineState.RECONCILING:
            self.state = EngineState.WARMING_UP
            self.reason = "rekonsiliasi selesai; menunggu data valid"
        if not exposure_only:
            self.last_reconcile_ok = time.monotonic()
            self.last_complete_snapshot = snapshot
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
                self.protection_id is not None
                and int(order["client_order_index"]) == self.protection_id
                and int(order["order_index"]) > 0
                and order.get("status") in {"pending", "open"}
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

    def protection_evidence(self, order: dict | None, snap: AccountSnapshot) -> dict:
        return {
            "client_id": self.protection_id,
            "position_qty": str(snap.position_qty),
            "position_price": str(snap.position_price),
            "snapshot_received_at": snap.received_at,
            "order_index": order.get("order_index") if order else None,
            "unprotected_observed_s": round(time.monotonic() - self.unprotected_since, 6)
            if self.unprotected_since
            else None,
            "order": {
                key: order.get(key)
                for key in (
                    "client_order_index",
                    "order_index",
                    "market_index",
                    "status",
                    "type",
                    "is_ask",
                    "reduce_only",
                    "remaining_base_amount",
                    "filled_base_amount",
                    "trigger_price",
                    "price",
                )
            }
            if order
            else None,
        }

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
        if time.time() - snap.received_at > 5 or snap.position_price <= 0:
            self.last_risk_poll = 0.0
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
        except (Exception, asyncio.CancelledError) as exc:
            self.db.order_state(client_id, OrderState.UNKNOWN)
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = "proteksi posisi belum pasti; perlu tutup posisi"
            self.db.set_flag("close_requested", "1")
            self.end_session("proteksi gagal atau ambigu")
            if isinstance(exc, asyncio.CancelledError):
                raise

    def preflight_valid(self, attempt: str, *, sending: bool = False) -> bool:
        proof = self.preflight_evidence
        binding = self.preflight_binding()
        return bool(
            proof
            and proof.valid(attempt, self.preflight_generation, binding, sending=sending)
            and self.audit_binding == binding
            and self.history_valid()
            and self.account_state_valid()
            and self.data_valid()
            and self.journal_ok
            and self.last_decision.basis_bps is not None
        )

    async def prepare_preflight(self, attempt: str, *, sendable: bool) -> bool:
        """Caller owns one mixed audit+send lease. No intent/signing in this method."""
        self.preflight_evidence = None
        started = time.monotonic()
        generation = self.preflight_generation
        before = self.preflight_binding()
        planned = self.audit_scheduler.request_cost
        await self.safe_reconcile(reserved=True)
        actual = sum(t >= started for t in self.account.rate.calls) if self.account else 0
        snap = self.account_snapshot
        blockers = []
        if (
            not self.history_valid()
            or not self.account_state_valid()
            or self.last_reconcile_ok < started
        ):
            blockers.append("complete history/account state failed or incomplete")
        if before != self.preflight_binding() or generation != self.preflight_generation:
            blockers.append("state/history changed during preflight")
        if actual > planned:
            blockers.append("pagination/retry grew beyond admission plan")
        if not snap or snap.position_qty or snap.active_orders or self.db.unresolved():
            blockers.append("account not flat/clean")
        if (
            not snap
            or self.db.net_cash_status(str(snap.equity), str(snap.position_qty))["status"]
            != "matched"
        ):
            blockers.append("cash audit not matched")
        checks = diagnostic_observation(self)["checks"]
        if self.probe_active:
            checks["incident_flags"] = not any(
                self.db.flag(flag) == "1"
                for flag in (
                    "funding_change_pending",
                    "account_incident_pending",
                    "total_loss_stop",
                )
            )
        blockers.extend(
            name
            for name, passed in checks.items()
            if not passed
            and name not in {"entry_request_reserve", "lifecycle_idle", "reconciliation_idle"}
        )
        if not self.data_valid() or self.last_decision.basis_bps is None:
            blockers.append("market/coverage not ready")
        if not self.dispatch_rate_ready():
            blockers.append("send/protection headroom unavailable")
        if not blockers:
            now = time.monotonic()
            ttl = min(PREFLIGHT_TTL_SECONDS, 15 - self.state_age() - 0.5)
            self.preflight_evidence = Evidence(
                attempt, generation, self.preflight_binding(), now, now + max(0.0, ttl), sendable
            )
        self.last_preflight_diagnostic = {
            "finished_at": time.time(),
            "elapsed_ms": (time.monotonic() - started) * 1000,
            "attempt": attempt,
            "sendable": sendable,
            "planned_read_cost": planned,
            "actual_read_cost": actual,
            "reserved_send_cost": 2,
            "broadcast": False,
            "ttl_seconds": PREFLIGHT_TTL_SECONDS,
            "blockers": blockers,
            "outcome": "complete_no_send"
            if not blockers and not sendable
            else "ready"
            if not blockers
            else "blocked",
            "binding": self.preflight_binding(),
            "generation": generation,
        }
        self.db.event("ENTRY_PREFLIGHT_DIAGNOSTIC", self.last_preflight_diagnostic)
        return not blockers

    async def diagnose_preflight(self) -> str:
        """Read-only rehearsal; evidence cannot authorize signing even if orders enabled."""
        if (
            self.preflight_in_progress
            or self.entry_tasks
            or self.risk_needs_read()
            or not self.account
            or not self.history_valid()
        ):
            return "preflight diagnostik diblok: lifecycle/history belum bersih"
        if self.reconcile_task and not self.reconcile_task.done():
            await asyncio.shield(self.reconcile_task)
        if self.state_refresh_task and not self.state_refresh_task.done():
            await asyncio.shield(self.state_refresh_task)
        if self.preflight_in_progress or not self.entry_rate_ready():
            return "preflight diagnostik diblok: kapasitas penuh belum tersedia"
        self.preflight_in_progress = True
        try:
            with self.account.rate.reserve("preflight", self.audit_scheduler.request_cost + 2):
                ok = await self.prepare_preflight("diagnostic:" + uuid.uuid4().hex, sendable=False)
        finally:
            self.preflight_in_progress = False
        return "preflight lengkap tanpa signing/broadcast" if ok else "preflight diagnostik gagal"

    async def enter(
        self, side: str, kind: str = "ENTRY", *, scheduled_epoch: int | None = None
    ) -> None:
        if self.preflight_in_progress or not self.config.allow_live_orders or not self.account:
            return
        if self.reconcile_task and not self.reconcile_task.done():
            await asyncio.shield(self.reconcile_task)
        if self.state_refresh_task and not self.state_refresh_task.done():
            await asyncio.shield(self.state_refresh_task)
        if self.preflight_in_progress or not self.entry_rate_ready():
            return
        attempt = uuid.uuid4().hex
        self.preflight_in_progress = True
        try:
            with self.account.rate.reserve("preflight", self.audit_scheduler.request_cost + 2):
                if not await self.prepare_preflight(attempt, sendable=True):
                    self.entries_stopped = True
                    return
                self.preflight_attempt = attempt
                await self._enter_dispatch(side, kind, scheduled_epoch=scheduled_epoch)
        finally:
            self.preflight_attempt = None
            self.preflight_evidence = None
            self.preflight_in_progress = False

    async def _enter_dispatch(
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
            or (kind != "PROBE" and not self.session_entry_ready())
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
        fingerprint_before = self.db.accounting_fingerprint()
        equity_before = snap.equity
        if (
            not self.account_state_valid()
            or snap.account_type_code != self.config.expected_account_type_code
            or not self.margin_settings_valid(snap)
            or snap.active_orders
            or self.entry_fee_blockers(kind, frozen=kind == "PROBE")
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
                dec(self.market.get("taker_fee", "0")),
                minimum_only=True,
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
                    or not self.dispatch_rate_ready()
                    or not self.preflight_valid(self.preflight_attempt or "", sending=True)
                    or (
                        kind == "ENTRY"
                        and (
                            not self.session_entry_ready()
                            or self.last_decision.side != side
                            or fingerprint_before != self.db.accounting_fingerprint()
                            or current is None
                            or current.equity != equity_before
                        )
                    )
                    or self.config.mode != "live_capable"
                    or self.entry_fee_blockers(kind, frozen=kind == "PROBE")
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
                    or any(x["client_id"] != client_id for x in self.db.unresolved())
                    or not self.journal_ok
                    or (
                        kind in {"PROBE", "ENTRY"}
                        and (
                            self.reconcile_failed
                            or not self.history_valid()
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
                if kind == "ENTRY":
                    if not self.session.consume(client_id):
                        return False
                else:
                    self.db.order_state(client_id, OrderState.SENT)
                self.dispatching_entries.add(client_id)
                proof = self.preflight_evidence
                assert proof is not None  # Validated above, without an intervening await.
                self.db.event(
                    "ENTRY_PREFLIGHT_CONSUMED",
                    {
                        "client_id": client_id,
                        "attempt": self.preflight_attempt,
                        "binding": proof.binding,
                        "expires_monotonic": proof.expires,
                    },
                )
                self.preflight_evidence = None  # One signing attempt only.
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
                        "signal": self.decision_evidence() if kind == "ENTRY" else None,
                        "session_id": (self.session.read() or {}).get("id")
                        if kind == "ENTRY"
                        else None,
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
                if state == OrderState.UNKNOWN:
                    self.end_session("pengiriman entry terinterupsi; rekonsiliasi prioritas")
            raise
        except Exception as exc:
            if "client_id" in locals():
                self.db.order_state(client_id, OrderState.UNKNOWN, reason=type(exc).__name__)
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"entry tidak pasti: {type(exc).__name__}"
            self.end_session(self.reason)
        finally:
            if "client_id" in locals():
                self.dispatching_entries.discard(client_id)

    async def manage_position(self) -> None:
        if not self.account_snapshot or not self.account_snapshot.position_qty:
            return
        if self.manual_exposure and self.db.flag("close_requested") != "1":
            return
        timed_out = self.position_opened_at and (
            time.monotonic() - self.position_opened_at
            >= self.config.max_hold_seconds - RISK_TRANSACTION_SECONDS
        )
        session_over = self.session_end and time.monotonic() >= self.session_end
        probing = self.probe_active or self.db.flag("probe_pending") == "1"
        probe_fill = probing and self.protection_confirmed
        protection_timeout = bool(
            not self.protection_confirmed
            and (
                (
                    self.protection_id is not None
                    and time.monotonic() - self.protect_sent_at >= STOP_CONFIRM_SECONDS
                )
                or (
                    self.unprotected_since
                    and time.monotonic() - self.unprotected_since >= STOP_CONFIRM_SECONDS
                )
                or (
                    self.protection_id is None
                    and self.unprotected_since
                    and time.monotonic() - self.unprotected_since >= STOP_INSTALL_SECONDS
                )
            )
        )
        recovered = False
        if not probing and self.market_data_valid():
            deviation = self.last_decision.deviation_bps
            basis = self.last_decision.basis_bps
            if basis is None and self.exit_basis_bps is not None:
                # Exit uses the last trusted basis without adding an entry/basis sample.
                # The new source guard must never hold protection or exposure reduction.
                local, remote = self.books["lighter"].quote(), self.books["binance"].quote()
                if local and remote and self.fx:
                    deviation = (local.mid / (remote.mid / self.fx) - 1) * 10000
                    basis = self.exit_basis_bps
            if deviation is not None and basis is not None:
                recovered = abs(deviation - basis) <= 2
                if self.session_open() and not self.protection_confirmed:
                    recovered = False  # Normal reversion exit follows verified protection.
        close_requested = self.db.flag("close_requested") == "1"
        if (
            timed_out
            or session_over
            or recovered
            or probe_fill
            or close_requested
            or protection_timeout
        ) and self.exit_id is None:
            self.db.event(
                "EXIT_REASON",
                {
                    "timed_out": bool(timed_out),
                    "session_over": bool(session_over),
                    "recovered": recovered,
                    "probe": bool(probe_fill),
                    "close_requested": close_requested,
                    "protection_timeout": protection_timeout,
                    "signal": self.decision_evidence(),
                },
            )
            self.db.set_flag("close_requested", "1")
            await self.close_position()
        elif self.protection_id is None and not self.manual_exposure and self.exit_id is None:
            await self.protect_position(self.account_snapshot)

    async def close_position(self) -> str:
        if not self.account or not self.market or not self.account_snapshot:
            return "akun tidak tersedia"
        snap = self.account_snapshot
        if time.time() - snap.received_at > 5:
            self.last_risk_poll = 0.0
            return "snapshot position basi; pemulihan prioritas diperlukan sebelum close"
        if not snap.position_qty:
            self.db.set_flag("close_requested", "0")
            return "tidak ada posisi menurut snapshot terakhir"
        if self.exit_id is not None:
            return "permintaan close sudah ada; menunggu rekonsiliasi"
        side = "sell" if snap.position_qty > 0 else "buy"
        quote = self.books["lighter"].quote()
        if not quote or time.monotonic_ns() - self.books["lighter"].updated_ns > (
            self.config.max_data_age_ms * 1_000_000
        ):
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
        except (Exception, asyncio.CancelledError) as exc:
            self.db.order_state(client_id, OrderState.UNKNOWN)
            self.entries_stopped = True
            self.state = EngineState.PAUSED
            self.reason = f"close belum pasti: {type(exc).__name__}"
            if isinstance(exc, asyncio.CancelledError):
                raise
            return self.reason

    async def commands(self) -> None:
        for command_id, kind in self.db.pending_commands():
            result = ""
            if kind == "STOP_ENTRIES":
                self.revoke_session_permission("entry dihentikan melalui perintah")
                self.entry_epoch += 1
                self.entries_stopped = True
                self.state = EngineState.PAUSED
                result = "entry dihentikan"
            elif kind == "LOAD_SESSION_PERMISSION":
                result = self.load_session_permission()
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
            elif kind == "DIAGNOSE_PREFLIGHT":
                self.tasks.append(asyncio.create_task(self.diagnose_preflight()))
                result = "preflight diagnostik dijadwalkan; tanpa intent/signing/broadcast"
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

    def decision_evidence(self) -> dict:
        d = self.last_decision
        return {
            "side": d.side,
            "deviation_bps": str(d.deviation_bps) if d.deviation_bps is not None else None,
            "basis_bps": str(d.basis_bps) if d.basis_bps is not None else None,
            "scale_bps": str(d.scale_bps) if d.scale_bps is not None else None,
            "threshold_bps": str(max(Decimal(8), d.scale_bps * 4))
            if d.scale_bps is not None
            else None,
            "reasons": d.reasons,
            "version": d.version,
        }

    def session_open(self) -> bool:
        record = self.session.read()
        return bool(record and record["status"] in {"active", "ending"})

    def session_entry_ready(self) -> bool:
        record = self.session.read()
        return bool(
            record
            and record["status"] == "active"
            and self.loaded_session_permission_id == record["id"]
            and len(record["attempts"]) < record["max_attempts"]
            and self.session_end
            and time.monotonic() < self.session_end
            and time.time() < record["deadline_epoch"]
            and record["parameter_hash"] == parameter_hash(self.config)
        )

    def session_flat_clean(self) -> bool:
        snap, full = self.account_snapshot, self.last_complete_snapshot
        record = self.session.read()
        last_attempt = (
            max((a["consumed_at"] for a in record["attempts"]), default=0) if record else 0
        )
        return bool(
            snap
            and full
            and not snap.position_qty
            and not snap.active_orders
            and not full.position_qty
            and not full.active_orders
            and not self.db.unresolved()
            and not self._own_quantity()
            and not self.entry_tasks
            and not self.dispatching_entries
            and self.exit_id is None
            and self.protection_id is None
            and not snap.other_positions
            and not snap.other_assets
            and full.received_at >= last_attempt
            and self.account_state_valid()
            and not self.reconcile_failed
            and self.history_valid()
        )

    def end_session(self, reason: str) -> None:
        if not self.session_open():
            return
        record = self.session.read()
        if record and record["status"] == "active":
            self.entry_epoch += 1
            self.session.stop(reason)
        self.entries_stopped = True
        self.db.set_flag("close_requested", "1")
        self.state = EngineState.PAUSED
        self.reason = "sesi berakhir; cleanup/proteksi/exit tetap berjalan"

    def session_tick(self, *, risk_only: bool = False) -> None:
        record = self.session.read()
        if (
            record
            and record["status"] == "authorized"
            and not self.session.authorization_valid(record)
        ):
            self.revoke_session_permission("activation authorization expired/invalid")
            return
        if not record or record["status"] not in {"active", "ending"}:
            return
        if record["status"] == "active":
            if time.monotonic() >= self.session_end or time.time() >= record["deadline_epoch"]:
                self.end_session("deadline 30 menit tercapai")
            elif not self.journal_ok:
                self.end_session("jurnal gagal; entry sesi dihentikan")
            elif record["parameter_hash"] != parameter_hash(self.config):
                self.end_session("parameter beku berubah")
            elif any(
                self.db.flag(flag) == "1"
                for flag in (
                    "account_incident_pending",
                    "funding_change_pending",
                    "total_loss_stop",
                )
            ):
                self.end_session("insiden akun/dana atau batas loss")
            elif self.db.flag("daily_stop_day") == datetime.now(JAKARTA).date().isoformat():
                self.end_session("batas loss harian")
            elif (
                self.account_snapshot
                and self.session_flat_clean()
                and self.db.net_cash_status(str(self.account_snapshot.equity), "0")["status"]
                == "unexplained_difference"
            ):
                self.end_session("selisih kas yang tidak dijelaskan")
        record = self.session.read()
        assert record is not None
        if record["status"] == "ending":
            cancelable = any(
                x["kind"] == "ENTRY"
                and x["state"] not in {OrderState.UNKNOWN, OrderState.CANCEL_REQUESTED}
                for x in self.db.unresolved()
            )
            if cancelable and (
                self.session_cleanup_task is None or self.session_cleanup_task.done()
            ):
                self.session_cleanup_task = asyncio.create_task(self.cancel_entries())
            if self.account_snapshot and self.account_snapshot.position_qty:
                self.db.set_flag("close_requested", "1")
        if risk_only:
            return
        now = time.monotonic()
        if now - self.last_session_sample >= 1:
            self.last_session_sample = now
            self.db.event(
                "STRATEGY_OBSERVATION",
                {
                    "session_id": record["id"],
                    "signal": self.decision_evidence(),
                    "state": self.state,
                    "entries_stopped": self.entries_stopped,
                    "entry_blockers": self.entry_fee_blockers("ENTRY")
                    if self.session_flat_clean()
                    else ["posisi/order atau rekonsiliasi belum bersih"],
                },
            )
        if not self.session_flat_clean() or self.account_snapshot is None:
            return
        if record["status"] == "active" and len(record["attempts"]) >= record["max_attempts"]:
            self.end_session("kuota tiga percobaan habis; posisi terakhir selesai")
            record = self.session.read()
            assert record is not None
        cash = self.db.net_cash_status(str(self.account_snapshot.equity), "0")
        needs_audit = bool(
            cash["status"] != "matched"
            or not self.db.fee_report_status()["all_unknown_fills_covered"]
        )
        fingerprint = self.db.accounting_fingerprint()
        if needs_audit and (self.fee_audit_task is None or self.fee_audit_task.done()):
            audit_key = "session_export:" + record["id"] + ":" + str(len(fingerprint["fills"]))
            attempts = int(self.db.flag(audit_key) or "0")
            last = float(self.db.flag(audit_key + ":at") or "0")
            if (
                attempts < 3
                and time.time() - last >= 20
                and self.account
                and (self.reconcile_task is None or self.reconcile_task.done())
                and self.account.rate.can_claim("reconcile", cost=4)
            ):
                self.db.set_flag(audit_key, str(attempts + 1))
                self.db.set_flag(audit_key + ":at", str(time.time()))
                self.fee_audit_task = asyncio.create_task(self.audit_session_cash())
        busy = self.fee_audit_task is not None and not self.fee_audit_task.done()
        if record["status"] == "ending":
            # Revoking entry authority must not wait indefinitely for fee metadata.
            # Audit pending remains visible; operational cleanup has independent proof.
            if not busy and (not needs_audit or time.time() - record["entry_stopped_at"] >= 60):
                self.finish_session(cash)
            return
        if needs_audit or busy or self.entry_fee_blockers("ENTRY"):
            self.entries_stopped = True
            return
        completed = [
            a["client_id"]
            for a in record["attempts"]
            if self.db.conn.execute(
                "SELECT 1 FROM fills WHERE client_id=?", (a["client_id"],)
            ).fetchone()
        ]
        if completed != record["completed_positions"]:
            record["completed_positions"] = completed
            self.session.write(record)
        self.session_positions = len(completed)
        self.db.set_flag("close_requested", "0")
        if (
            self.data_valid()
            and self.last_decision.basis_bps is not None
            and self.strategy.coverage()["ready"]
            and self.entry_rate_ready()
        ):
            self.entries_stopped = False
            self.state = EngineState.LIVE
            self.reason = "sesi terbatas; menunggu sinyal strategi dan guard"

    async def audit_session_cash(self) -> None:
        record = self.session.read()
        if not record or not self.account or not self.account_snapshot:
            return
        fingerprint = self.db.accounting_fingerprint()
        try:
            # Only authorized fills may renew this session's entry audit binding.
            historical = {row[0] for row in record["initial_fingerprint"]["fills"]}
            new = self.db.conn.execute(
                "SELECT f.fill_id,f.qty,f.price,f.fee,i.side,i.client_id,i.kind,i.created_at "
                "FROM fills f JOIN intents i USING(client_id)"
            ).fetchall()
            new = [row for row in new if row[0] not in historical]
            allowed = {a["client_id"] for a in record["attempts"]}
            if fingerprint["flows"] != record["initial_fingerprint"]["flows"] or any(
                row[7] < record["started_at"]
                or (row[6] == "ENTRY" and row[5] not in allowed)
                or row[6] not in {"ENTRY", "STOP", "EXIT"}
                for row in new
            ):
                self.end_session("fill/dana tak terduga saat audit sesi")
                return
            async with asyncio.timeout(12):
                reports = await collect_export(
                    self.account, self.db, self.root / "state" / "audits" / record["id"]
                )
                snap = await self.account.probe_snapshot(
                    1, self.account_snapshot.funding_flows, priority="reconcile"
                )
            if (
                snap.position_qty
                or snap.active_orders
                or fingerprint != self.db.accounting_fingerprint()
                or not self.history_valid()
            ):
                return
            if sum((dec(row[1]) * (1 if row[4] == "buy" else -1) for row in new), Decimal(0)):
                return
            by_id = {r["trade_id"]: r for r in reports}
            gross = sum(
                (dec(row[1]) * dec(row[2]) * (1 if row[4] == "sell" else -1) for row in new),
                Decimal(0),
            )
            fees = sum(
                (
                    dec(row[3]) if row[3] is not None else dec(by_id[row[0]]["reported_fee_usdc"])
                    for row in new
                ),
                Decimal(0),
            )
            delta = snap.equity - dec(record["equity_before"])
            residual = delta - gross + fees
            proof = {
                "session_id": record["id"],
                "equity_before": record["equity_before"],
                "equity_after": str(snap.equity),
                "cash_change_usdc": str(delta),
                "gross_fill_pnl_usdc": str(gross),
                "reported_or_exact_fee_sum_usdc": str(fees),
                "reported_precision_decimals": sorted({r["precision_decimals"] for r in reports}),
                "net_residual_usdc": str(residual),
                "fingerprint": fingerprint,
                "fee_exact_complete": not bool(self.db.pending_fees()),
                "source": "matched official CSV, fresh flat/global orders, full funding reconcile",
                "captured_at_utc": datetime.now(JAKARTA).isoformat(),
            }
            self.db.event("STRATEGY_CASH_AUDIT_RESULT", proof)
            if residual:
                self.end_session("selisih kas/ledger setelah transaksi")
                return
            self.db.event("NET_CASH_AUDIT", proof)
            current = self.session.read()
            assert current is not None
            current.update(audited_fingerprint=fingerprint, audited_equity=str(snap.equity))
            self.session.write(current)
        except Exception as exc:
            self.db.event(
                "STRATEGY_FEE_AUDIT_PENDING",
                {"session_id": record["id"], "error_type": type(exc).__name__},
            )
            self.entries_stopped = True

    def finish_session(self, cash: dict) -> None:
        if not self.session_flat_clean():
            return
        self.entries_stopped = True
        self.loaded_session_permission_id = None
        self.entry_epoch += 1
        self.config = self.config.model_copy(
            update={"allow_live_orders": False, "session_accept_reported_fees": False}
        )
        path = self.root / "config.json"
        if path.exists():
            temp = path.with_suffix(".json.pending")
            temp.write_text(self.config.model_dump_json(indent=2) + "\n")
            temp.chmod(0o600)
            temp.replace(path)
        self.db.set_flag("close_requested", "0")
        record = self.session.read()
        assert record is not None
        record["completed_positions"] = [
            a["client_id"]
            for a in record["attempts"]
            if self.db.conn.execute(
                "SELECT 1 FROM fills WHERE client_id=?", (a["client_id"],)
            ).fetchone()
        ]
        record["final_cash_audit"] = cash
        self.session.write(record)
        self.session.close()
        self.session_end = 0.0
        self.db.event(
            "STRATEGY_OPERATIONAL_COMPLETE",
            {
                "position_qty": "0",
                "all_active_orders": 0,
                "cash_audit": cash,
                "fee_exact_unknown_count": self.db.pending_fees(),
                "order_permission": False,
            },
        )
        self.state = EngineState.PAUSED
        self.reason = "sesi selesai, akun flat/order bersih, izin order mati"

    def revoke_session_permission(self, reason: str) -> None:
        """Engine owns runtime permission; cleanup workers retain their authority."""
        self.end_session(reason)
        self.session.revoke(reason)
        self.entries_stopped = True
        self.loaded_session_permission_id = None
        self.entry_epoch += 1
        self.invalidate_preflight("session permission revoked", history=False)
        self.config = self.config.model_copy(
            update={
                "allow_live_orders": False,
                "session_accept_reported_fees": False,
                "probe_accept_reported_fees": False,
            }
        )
        path = self.root / "config.json"
        if path.exists():
            temp = path.with_suffix(".json.pending")
            temp.write_text(self.config.model_dump_json(indent=2) + "\n")
            temp.chmod(0o600)
            temp.replace(path)

    def load_session_permission(self) -> str:
        """Explicit prepare only: no activation, network calls, or buffer reset."""
        try:
            candidate = load_config(self.root / "config.json")
        except Exception as exc:
            return f"permission diblok: config invalid ({type(exc).__name__})"
        if not candidate.allow_live_orders:
            self.revoke_session_permission("explicit permission reload: orders OFF")
            return "permission dicabut; order OFF, data collection tetap berjalan"
        record = self.session.read()
        if not self.session.authorization_valid(record):
            return "permission diblok: authorization expired/revoked/used/closed"
        assert record is not None
        permission_fields = {
            "allow_live_orders",
            "session_accept_reported_fees",
            "probe_accept_reported_fees",
            "live_entry_scope",
        }
        if (
            candidate.model_dump(exclude=permission_fields)
            != self.config.model_dump(exclude=permission_fields)
            or candidate.live_entry_scope != "strategy"
            or not candidate.session_accept_reported_fees
            or candidate.probe_accept_reported_fees
            or record["account_index"] != candidate.account_index
            or record["parameter_hash"] != parameter_hash(candidate)
        ):
            return "permission diblok: identity/risk/config binding mismatch"
        if (
            not self.session_flat_clean()
            or not self.account_state_valid()
            or not self.history_valid()
            or self.preflight_in_progress
            or (self.state_refresh_task and not self.state_refresh_task.done())
        ):
            return "permission diblok: account/lifecycle/history belum bersih dan segar"
        previous = self.config
        self.config = candidate
        blockers = self.entry_fee_blockers("ENTRY")
        if blockers:
            self.config = previous
            return "permission diblok: " + ", ".join(blockers)
        if self.loaded_session_permission_id == record["id"]:
            return "permission sudah dimuat; deadline/kuota/buffer tidak berubah"
        self.loaded_session_permission_id = record["id"]
        self.entries_stopped = True
        self.invalidate_preflight("explicit session permission loaded", history=False)
        self.state = (
            EngineState.READY
            if self.data_valid()
            and self.last_decision.basis_bps is not None
            and self.strategy.coverage()["ready"]
            else EngineState.WARMING_UP
        )
        self.db.event(
            "SESSION_PERMISSION_LOADED",
            {
                "session_id": record["id"],
                "parameter_hash": record["parameter_hash"],
                "authorization_expires_epoch": record["authorization_expires_epoch"],
                "activated": False,
                "entries_stopped": True,
                "coverage_samples_preserved": len(self.strategy.samples),
            },
        )
        return "permission dimuat; belum aktif, START_LIVE dan preflight tetap wajib"

    def start_session(self) -> str:
        blockers = self.live_blockers()
        if blockers:
            return "live diblok: " + ", ".join(blockers)
        try:
            record = self.session.activate(
                expected_id=self.loaded_session_permission_id,
                expected_hash=parameter_hash(self.config),
            )
        except ValueError:
            return "live diblok: authorization changed/expired before activation"
        self.db.set_flag("live_ever", "1")
        self.session_end = time.monotonic() + record["max_seconds"]
        self.session_positions = 0
        self.entries_stopped = False
        self.state = EngineState.LIVE
        self.reason = "sesi live terbatas berjalan"
        return "sesi live dimulai"

    async def probe_once(self) -> str:
        blockers = self.probe_blockers()
        if blockers:
            return "probe diblok: " + ", ".join(blockers)
        if self.account_snapshot is None:
            return "probe diblok: snapshot akun belum tersedia"
        self.probe_preflight_fingerprint = self.db.accounting_fingerprint()
        self.probe_preflight_equity = self.account_snapshot.equity
        self.db.event(
            "PROBE_PREFLIGHT",
            {
                "session_id": self.db.flag("probe_session_id"),
                "fingerprint": self.probe_preflight_fingerprint,
                "equity_before": str(self.probe_preflight_equity),
                "fee_policy": "reported_export_probe_only"
                if self.config.probe_accept_reported_fees
                else "exact",
                "preview": self.probe_preview(),
            },
        )
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

    def probe_preview(self) -> dict | None:
        snap, market = self.account_snapshot, self.market
        quote = self.books["lighter"].quote()
        if not snap or not market or not quote:
            return None
        try:
            sizing = size_entry(
                self.config,
                min(snap.equity, snap.available),
                Decimal(0),
                Decimal(0),
                quote.ask,
                "buy",
                dec(market["min_base_amount"]),
                dec(market["min_quote_amount"]),
                int(market["size_decimals"]),
                int(market["price_decimals"]),
                Decimal(35),
                Decimal(0),
                minimum_only=True,
            )
        except ValueError as exc:
            return {"blocked": str(exc)}
        tick = Decimal(1).scaleb(-int(market["price_decimals"]))
        trigger = limit_price(sizing.price * Decimal("0.9965"), "sell", tick, Decimal(0))
        return {
            "side": "buy",
            "side_reason": "fixed BUY probe; lifecycle test, no strategy signal",
            "quantity": str(sizing.quantity),
            "quote_reference": str(quote.ask),
            "entry_limit": str(sizing.price),
            "notional_max": str(sizing.quantity * sizing.price),
            "planned_risk": str(sizing.planned_risk),
            "slippage_bps": "2",
            "stop_trigger_preview": str(trigger),
            "stop_limit_preview": str(
                limit_price(trigger * Decimal("0.997"), "sell", tick, Decimal(0))
            ),
            "stop_reference": "actual position entry price after fill; preview uses entry limit",
            "max_hold_seconds": self.config.max_hold_seconds,
            "stop_install_seconds": STOP_INSTALL_SECONDS,
            "stop_confirm_seconds": STOP_CONFIRM_SECONDS,
            "spread_bps": str((quote.ask - quote.bid) / quote.mid * 10000),
            "depth_price": str(self.books["lighter"].price_for("buy", sizing.quantity)),
        }

    def entry_rate_ready(self) -> bool:
        """Include the COMPLETE audit still to run, not only two send calls."""
        rate = getattr(self.account, "rate", None)
        reads = self.audit_scheduler.request_cost
        return bool(
            rate
            and rate.can_claim("preflight", reads + 2)
            and rate.can_claim("probe_read", reads + 14)
        )

    def dispatch_rate_ready(self) -> bool:
        rate = getattr(self.account, "rate", None)
        return bool(
            rate is not None
            and rate.can_claim("entry", cost=2)
            and rate.can_claim("probe_read", cost=14)
        )

    def probe_read_ready(self) -> bool:
        if not self.probe_active and not self.session_open():
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
        return rate.can_claim("reconcile", cost=4 if pending else 8)

    def probe_blockers(self) -> list[str]:
        blockers = self.live_blockers(for_probe=True)
        record = self.session.read()
        if record and record["status"] != "closed":
            blockers.append("otorisasi sesi strategi belum selesai; probe dilarang")
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

    def entry_fee_blockers(self, kind: str, *, frozen: bool = False) -> list[str]:
        session_policy = kind == "ENTRY" and self.config.session_accept_reported_fees
        if not session_policy and (kind != "PROBE" or not self.config.probe_accept_reported_fees):
            return (
                ["fee trade belum diketahui; audit pembukuan tertunda"]
                if self.db.pending_fees()
                else []
            )
        snap = self.account_snapshot
        if not snap:
            return ["bukti fee probe membutuhkan snapshot akun"]
        blockers = []
        for (fill_id,) in self.db.conn.execute("SELECT fill_id FROM fills WHERE fee IS NULL"):
            raw = self.db.flag(f"fee_report:{fill_id}")
            try:
                report = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                report = {}
            if not self.db.fee_report_matches(fill_id, report, self.config.account_index):
                blockers.append(f"bukti ekspor fee hilang/tidak cocok: trade {fill_id}")
            elif session_policy:
                try:
                    captured = datetime.fromisoformat(report["captured_at_utc"])
                    if captured.tzinfo is None:
                        raise ValueError("missing timezone")
                except (KeyError, ValueError, TypeError):
                    blockers.append(
                        f"waktu pengambilan bukti fee hilang/tidak cocok: trade {fill_id}"
                    )
        if self.db.net_cash_status(str(snap.equity), str(snap.position_qty))["status"] != "matched":
            blockers.append("audit net kas/dana probe belum cocok")
        if not self.history_valid():
            blockers.append("audit kas/dana probe belum segar")
        if session_policy:
            record = self.session.read()
            if not record or record["status"] not in {"authorized", "active"}:
                blockers.append("otorisasi sesi strategi tidak tersedia atau sudah selesai")
            elif (
                record["account_index"] != self.config.account_index
                or record["parameter_hash"] != parameter_hash(self.config)
                or record["audited_fingerprint"] != self.db.accounting_fingerprint()
                or record["audited_equity"] != str(snap.equity)
            ):
                blockers.append("parameter/fill/dana berubah dari audit sesi strategi")
            return blockers
        raw_binding = self.db.flag("probe_authorization_snapshot")
        try:
            binding = json.loads(raw_binding) if raw_binding else {}
        except (ValueError, TypeError):
            binding = {}
        fingerprint = self.db.accounting_fingerprint()
        if (
            not self.db.flag("probe_session_id")
            or binding.get("session_id") != self.db.flag("probe_session_id")
            or binding.get("account_index") != self.config.account_index
            or binding.get("fingerprint") != fingerprint
            or binding.get("equity") != str(snap.equity)
        ):
            blockers.append("fill/dana berubah atau sesi izin probe belum terikat")
        if frozen and (
            fingerprint != self.probe_preflight_fingerprint
            or snap.equity != self.probe_preflight_equity
        ):
            blockers.append("fill/dana berubah setelah preflight probe")
        return blockers

    def live_blockers(self, *, for_probe: bool = False) -> list[str]:
        blockers = []
        if not for_probe:
            record = self.session.read()
            if not record or record["status"] != "authorized":
                blockers.append("tidak ada otorisasi sesi strategi sekali pakai")
            elif not self.session.authorization_valid(record):
                blockers.append("otorisasi sesi expired/revoked/used")
            elif self.loaded_session_permission_id != record["id"]:
                blockers.append("izin sesi belum dimuat eksplisit oleh engine")
            if not self.entry_rate_ready():
                blockers.append("cadangan request entry/proteksi belum cukup")
            if (
                self.entry_tasks
                or self.dispatching_entries
                or self.protection_id is not None
                or self.exit_id is not None
            ):
                blockers.append("siklus posisi sebelumnya belum bersih")
            if self.reconcile_task and not self.reconcile_task.done():
                blockers.append("rekonsiliasi akun masih berjalan")
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
        if not self.history_valid():
            blockers.append("audit riwayat lengkap wajib belum selesai")
        if not self.account_state_valid():
            blockers.append("komponen keadaan akun belum lengkap/segar/konsisten")
        if self.account and self.reconcile_failed:
            blockers.append("rekonsiliasi akun terakhir gagal")
        if self.last_decision.basis_bps is None or not self.data_valid():
            blockers.append("data atau warm-up belum valid")
        source = self.source_freshness()
        if not source["passed"]:
            blockers.append(f"kesegaran sumber Binance: {source['reason']}")
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
                        minimum_only=True,
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
        blockers.extend(self.entry_fee_blockers("PROBE" if for_probe else "ENTRY"))
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
                "receipt_ns": q.stamp.receipt_ns if q else None,
                "exchange_ms": q.stamp.exchange_ms if q else None,
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
            (
                dec(row[0])
                for row in self.db.conn.execute("SELECT fee FROM fills WHERE fee IS NOT NULL")
            ),
            Decimal(0),
        )
        return {
            "mode": self.config.mode,
            "allow_live_orders": self.config.allow_live_orders,
            "live_entry_scope": self.config.live_entry_scope,
            "probe_accept_reported_fees": self.config.probe_accept_reported_fees,
            "session_accept_reported_fees": self.config.session_accept_reported_fees,
            "strategy_session": self.session.read(),
            "loaded_session_permission_id": self.loaded_session_permission_id,
            "probe_session_id": self.db.flag("probe_session_id"),
            "probe_preview": self.probe_preview(),
            "request_budget": None
            if rate is None
            else {
                "used_last_60s": len(rate.calls),
                "reserved_unconsumed": rate._held() if hasattr(rate, "_held") else 0,
                "normal_cap": 40,
                "urgent_read_cap": 47,
                "transaction_cap": 55,
                "transaction_slots_remaining": max(
                    0, 55 - len(rate.calls) - (rate._held() if hasattr(rate, "_held") else 0)
                ),
            },
            "audit_scheduler": self.audit_scheduler.status(time.monotonic()),
            "account_state_scheduler": self.state_scheduler.status(time.monotonic()),
            "account_state_diagnostic": self.last_state_diagnostic,
            "history_audit": {
                "complete": self.history_valid(),
                "required": self.history_required,
                "reason": self.history_reason,
                "idle_interval_seconds": HISTORY_IDLE_SECONDS,
                "age_seconds": time.monotonic() - self.last_reconcile_ok
                if self.last_reconcile_ok
                else None,
            },
            "preflight": {
                "diagnostic": self.last_preflight_diagnostic,
                "ttl_seconds": PREFLIGHT_TTL_SECONDS,
                "complete_reads_required": self.audit_scheduler.request_cost,
                "send_cost": 2,
                "protection_headroom_envelope": 14,
                "ready_to_send_technically": bool(
                    self.preflight_evidence
                    and self.preflight_valid(self.preflight_evidence.attempt)
                ),
                "send_authorization": False
                if not self.preflight_evidence
                else bool(
                    self.preflight_evidence.sendable
                    and self.config.allow_live_orders
                    and self.preflight_valid(self.preflight_evidence.attempt)
                    and (
                        self.session_entry_ready()
                        or (
                            self.probe_active
                            and self.config.live_entry_scope == "probe_only"
                            and time.monotonic() < self.session_end
                        )
                    )
                ),
            },
            "state": self.state,
            "reason": self.reason,
            "market_id": self.market["market_id"] if self.market else None,
            "market_rules": {
                key: self.market[key]
                for key in (
                    "min_base_amount",
                    "min_quote_amount",
                    "size_decimals",
                    "price_decimals",
                    "taker_fee",
                )
                if key in self.market
            }
            if self.market
            else None,
            "quotes": quotes,
            "decision_inputs": self.last_decision_inputs,
            "source_clock": self.source_clock.status(),
            "source_freshness": self.source_freshness(),
            "market_processing": {
                key: dict(value) for key, value in self.market_processing.items()
            },
            "diagnostic_observation": diagnostic_observation(self),
            "reconcile_diagnostic": self.last_reconcile_diagnostic,
            "transport_trace": self.account.transport_trace.status()
            if self.account and hasattr(self.account, "transport_trace")
            else None,
            "coverage": self.strategy.coverage(),
            "feed_channels": getattr(self.feeds, "channel_status", lambda: {})(),
            "binance_transport_trace": self.feeds.transport_trace.status()
            if self.feeds and hasattr(self.feeds, "transport_trace")
            else None,
            "fee_lookup": self.db.fee_lookup_status(),
            "fee_reports": self.db.fee_report_status(),
            "net_cash_audit": self.db.net_cash_status(str(snap.equity), str(snap.position_qty))
            if snap
            else None,
            "fx_usdcusdt": str(self.fx) if self.fx else None,
            "fx_diagnostic": self.fx_monitor.status(),
            "decision": {
                "side": self.last_decision.side,
                "deviation_bps": str(self.last_decision.deviation_bps)
                if self.last_decision.deviation_bps is not None
                else None,
                "basis_bps": str(self.last_decision.basis_bps)
                if self.last_decision.basis_bps is not None
                else None,
                "scale_bps": str(self.last_decision.scale_bps)
                if self.last_decision.scale_bps is not None
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
                "exchange_active_order_count": len(snap.active_orders),
                "other_exchange_position_count": len(snap.other_positions),
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
                "component_ages_s": component_ages(snap),
                "components_complete": self.account_state_valid(),
                "exchange_order_count": snap.exchange_order_count,
                "exchange_pending_count": snap.exchange_pending_count,
                "collateral": str(snap.collateral) if snap.collateral is not None else None,
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
                "recorded_fees": str(fees) if not self.db.pending_fees() else None,
                "known_fees": str(fees),
                "pending_fee_count": self.db.pending_fees(),
                "fee_audit_complete": not bool(self.db.pending_fees()),
                "position_read_age_s": round(time.monotonic() - self.last_position_read_ok, 1)
                if self.last_position_read_ok
                else None,
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
