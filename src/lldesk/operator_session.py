"""Human UI admission only. Engine owns every activation, audit and dispatch.

Preview is SQLite read-only. A CSRF-protected manual POST is the only caller of
begin(); this module never signs, sends orders or starts/restarts services.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .activation import ActivationRequests
from .config import Config, load_config, repo_root
from .db import Database
from .session import FIVE_HOUR_PROPOSAL, BoundedSession, parameter_hash

LEGACY_PROPOSAL = "bounded-ui-2026-10-08-v1"
PROPOSAL = FIVE_HOUR_PROPOSAL
LIMITS = {
    "session_seconds": "18000",
    "max_session_positions": "3",
    "max_entry_notional_usd": "25",
    "max_trade_risk_usd": "0.50",
    "daily_loss_limit_usd": "5",
    "max_effective_leverage": "1",
    "exit_reserve_usd": "2",
    "max_hold_seconds": "180",
    "max_funding_usd": "50",
    "max_total_loss_usd": "50",
    "source_funding_spent_usd": "50",
    "live_entry_scope": "strategy",
    "mode": "live_capable",
}
OPERATIONS = (
    "account_identity",
    "margin_settings",
    "account_fresh",
    "flat_clean",
    "lifecycle_idle",
    "reconciliation_idle",
    "reconciliation_fresh",
    "journal",
    "incident_flags",
    "funding_known",
    "cash_audit",
    "loss_limits",
    "historical_fee_evidence",
    "entry_request_reserve",
    "minimum_sizing_depth",
)


class AdmissionError(ValueError):
    pass


def require(passed: object, message: str) -> None:
    if not passed:
        raise AdmissionError(message)


@contextmanager
def read_db(root: Path):
    db = sqlite3.connect(f"file:{root}/state/desk.db?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def status_readonly(root: Path) -> dict:
    try:
        with read_db(root) as db:
            row = db.execute("SELECT body,updated_at FROM status WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return {
            "connected": False,
            "state": "DISCONNECTED",
            "reason": "Snapshot engine belum tersedia",
        }
    if not row:
        return {
            "connected": False,
            "state": "DISCONNECTED",
            "reason": "Engine belum tersedia",
        }
    body = json.loads(row[0])
    body["updated_at"] = row[1]
    body["connected"] = 0 <= time.time() - row[1] < 3
    if not body["connected"]:
        body.update(
            state="DISCONNECTED",
            reason="Heartbeat engine terputus; snapshot terakhir bukan live",
        )
    return body


def atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(value, f, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def flat(status: dict) -> bool:
    a = status.get("account") or {}
    return bool(
        a.get("components_complete")
        and max(a.get("component_ages_s", {"unknown": 99}).values()) < 15
        and str(a.get("position_qty")) in {"0", "0.00000"}
        and a.get("exchange_active_order_count") == 0
        and a.get("other_exchange_position_count") == 0
        and a.get("exchange_pending_count") == 0
        and not status.get("pending_orders")
    )


def validate(config: Config, status: dict, db: sqlite3.Connection) -> None:
    require(
        all(str(getattr(config, k)) == v for k, v in LIMITS.items() if k != "session_seconds")
        and config.session_seconds in {1800, 18000},
        "Batas sesi berubah; perlu review baru",
    )
    require(
        not config.allow_live_orders
        and not config.session_accept_reported_fees
        and not config.probe_accept_reported_fees,
        "Izin order harus OFF sebelum sesi baru",
    )
    require(
        status.get("connected") and 0 <= time.time() - status.get("updated_at", 0) < 1,
        "Engine belum terhubung/fresh",
    )
    require(
        not status.get("allow_live_orders") and status.get("entries_stopped"),
        "Entry belum berhenti",
    )
    observation = status.get("diagnostic_observation") or {}
    require(
        0 <= time.time() - observation.get("observed_at", 0) < 1,
        "Assessment operasi belum fresh",
    )
    failed = [k for k in OPERATIONS if observation.get("checks", {}).get(k) is not True]
    require(not failed, "Pemeriksaan akun/operasi belum lolos: " + ", ".join(failed))
    require(flat(status), "Posisi/order/pending belum bersih seluruh market")
    flags = dict(db.execute("SELECT key,value FROM flags"))
    old = json.loads(flags.get("strategy_session", "null"))
    require(
        old is not None and old.get("status") == "closed",
        "Sesi lama belum selesai/ditutup",
    )
    require(not status.get("loaded_session_permission_id"), "Izin masih dimuat")
    require(flags.get("close_requested") != "1", "Pemulihan/close masih diperlukan")
    require(
        db.execute(
            "SELECT count(*) FROM intents WHERE state NOT IN ('FILLED','CANCELED','REJECTED')"
        ).fetchone()[0]
        == 0,
        "Intent belum final",
    )
    require(
        db.execute("SELECT count(*) FROM commands WHERE state='PENDING'").fetchone()[0] == 0,
        "Perintah masih pending",
    )
    require(
        db.execute(
            "SELECT count(*) FROM activation_requests WHERE status IN ('QUEUED','WAITING')"
        ).fetchone()[0]
        == 0,
        "Aktivasi masih menunggu",
    )
    require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Jurnal tidak sehat")


async def official_read(config: Config) -> Any:
    from .account import LighterAccount
    from .market import market_metadata

    m = await market_metadata()
    adapter = LighterAccount(config)
    try:
        await adapter.open()
        snap = await adapter.snapshot(int(m["market_id"]))
        require(
            snap.position_qty == 0
            and not snap.active_orders
            and not snap.other_positions
            and not snap.other_assets
            and snap.exchange_order_count == 0
            and snap.exchange_pending_count == 0,
            "GET resmi: posisi/order/pending belum bersih",
        )
        require(
            snap.account_type_code == config.expected_account_type_code
            and snap.account_trading_mode == config.expected_account_trading_mode
            and snap.margin_mode == config.expected_margin_mode
            and snap.initial_margin_fraction == config.expected_initial_margin_fraction,
            "GET resmi: identitas/margin berubah",
        )
        require(time.time() - snap.received_at < 15, "GET akun sudah stale")
        return snap
    finally:
        await adapter.close()


class OperatorSession:
    def __init__(self, root: Path, account_reader=official_read):
        self.root = root
        self.account_reader = account_reader
        self.coord = (
            Path("~/Workspace/projects/lldesk-service-coordination")
            if root.resolve() == repo_root().resolve()
            else root / "coordination"
        )
        self.admission = threading.Lock()
        self.holders: list[tuple[Any, str, threading.Thread]] = []
        self.shutdown_event = threading.Event()
        self.source_binding = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parent.glob("*.py")
        }

    def source_valid(self) -> bool:
        return all(
            hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() == digest
            for name, digest in self.source_binding.items()
        )

    def stored_request(self, db, request_id: str) -> dict | None:
        for base in (PROPOSAL, LEGACY_PROPOSAL):
            row = db.execute(
                "SELECT value FROM flags WHERE key=?",
                ("ui_request:" + base + ":" + request_id,),
            ).fetchone()
            if row:
                return json.loads(row[0])
            old = db.execute(
                "SELECT value FROM flags WHERE key=?", ("ui_proposal:" + base,)
            ).fetchone()
            if old:
                result = json.loads(old[0])
                if result.get("request_id") == request_id:
                    return {"proposal": result.get("proposal", base), "result": result}
        return None

    def proposal_scope(self, db, status: dict) -> tuple[str, dict | None, bool]:
        raw = db.execute(
            "SELECT value FROM flags WHERE key=?", ("ui_proposal:" + PROPOSAL,)
        ).fetchone()
        if not raw:
            raw = db.execute(
                "SELECT value FROM flags WHERE key=?", ("ui_proposal:" + LEGACY_PROPOSAL,)
            ).fetchone()
        if not raw:
            return PROPOSAL, None, False
        old = json.loads(raw[0])
        record_row = db.execute("SELECT value FROM flags WHERE key='strategy_session'").fetchone()
        activation_row = db.execute(
            "SELECT status,body FROM activation_requests WHERE request_id=?",
            (old.get("request_id"),),
        ).fetchone()
        record = json.loads(record_row[0]) if record_row else None
        activation = json.loads(activation_row[1]) if activation_row else None
        runtime = status.get("strategy_session") or {}
        terminal = bool(
            record
            and activation
            and activation_row
            and isinstance(old.get("session_id"), str)
            and isinstance(old.get("request_id"), str)
            and isinstance(record.get("parameter_hash"), str)
            and len(record["parameter_hash"]) == 64
            and isinstance(record.get("account_index"), int)
            and isinstance(record.get("authorization_expires_epoch"), (float, int))
            and record.get("status") == runtime.get("status") == "closed"
            and old.get("session_id")
            == record.get("id")
            == runtime.get("id")
            == activation.get("session_id")
            and old.get("request_id") == activation.get("request_id")
            and activation_row[0] == activation.get("status")
            and activation.get("status") in {"CANCELLED", "REJECTED", "ACTIVATED"}
            and isinstance(activation.get("terminal_at"), (float, int))
            and activation["terminal_at"] > 0
            and activation.get("parameter_hash") == record.get("parameter_hash")
            and activation.get("account_index") == record.get("account_index")
            and activation.get("authorization_expires_epoch")
            == record.get("authorization_expires_epoch")
            and not status.get("loaded_session_permission_id")
        )
        if terminal:
            # Scope changes only after the exact prior request/session is terminal.
            try:
                session_id = str(uuid.UUID(old["session_id"]))
            except (ValueError, TypeError, AttributeError):
                return PROPOSAL, old, False
            return PROPOSAL + ":after:" + session_id, old, True
        return PROPOSAL, old, False

    def admit_proposal(self, db, status: dict, proposal: str, request_id: str) -> dict | None:
        scope, old, renewable = self.proposal_scope(db, status)
        require(
            proposal == scope,
            "Status sesi berubah; periksa kembali sebelum menyetujui sesi baru",
        )
        require(
            not old or renewable,
            "Sesi sebelumnya belum terbukti selesai; jangan membuat pengganti",
        )
        require(
            not old or old.get("request_id") != request_id,
            "UUID permintaan lama tidak boleh menjadi sesi baru",
        )
        return old

    def save_request(self, db, proposal: str, result: dict) -> None:
        key = "ui_request:" + PROPOSAL + ":" + result["request_id"]
        db.set_flag(key, json.dumps({"proposal": proposal, "result": result}))
        db.set_flag("ui_proposal:" + PROPOSAL, json.dumps(result))

    def preview(self) -> dict:
        status = status_readonly(self.root)
        if not (self.root / "state/desk.db").exists():
            return {
                "proposal": PROPOSAL,
                "limits": LIMITS,
                "can_request": False,
                "blockers": ["Snapshot engine belum tersedia"],
                "technical_ready": False,
                "preparation_seconds": 1200,
                "request": None,
                "waiting_blockers": [],
            }
        with read_db(self.root) as db:
            scope, request, renewable = self.proposal_scope(db, status)
            blockers = []
            try:
                require(
                    self.source_valid(),
                    "Source aplikasi berubah; tunggu review/reload pasif",
                )
                validate(load_config(self.root / "config.json"), status, db)
            except (ValueError, OSError) as exc:
                blockers = [str(exc)]
        return {
            "proposal": scope,
            "limits": LIMITS,
            "can_request": not blockers and (not request or renewable),
            "blockers": blockers,
            "technical_ready": bool(
                (status.get("diagnostic_observation") or {}).get("technical_ready")
            ),
            "preparation_seconds": 1200,
            "request": request,
            "waiting_blockers": (status.get("diagnostic_observation") or {}).get("blockers", []),
        }

    def begin(self, request_id: str, proposal: str, consent: bool) -> dict:
        require(consent, "Persetujuan eksplisit batas sesi diperlukan")
        request_id = str(uuid.UUID(request_id))
        with self.admission:
            with read_db(self.root) as ro:
                prior = self.stored_request(ro, request_id)
                if prior:
                    require(
                        proposal == prior["proposal"],
                        "UUID permintaan sudah terikat proposal lain",
                    )
                    return prior["result"]
                self.admit_proposal(ro, status_readonly(self.root), proposal, request_id)
            self.coord.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock: Any = (self.coord / "engine.lock").open("a+")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise AdmissionError("Service sedang dimiliki pekerjaan lain; tunggu") from None
            run = str(uuid.uuid4())
            session_id = str(uuid.uuid4())
            staged = False
            db = None
            self.owner(run, "held")
            try:
                require(
                    self.source_valid(),
                    "Source aplikasi berubah; review baru diperlukan",
                )
                config = load_config(self.root / "config.json")
                status = status_readonly(self.root)
                with read_db(self.root) as ro:
                    self.admit_proposal(ro, status, proposal, request_id)
                    validate(config, status, ro)
                snap = asyncio.run(self.account_reader(config))
                # Fresh status read after GET; no network under the DB write lock.
                status = status_readonly(self.root)
                db = Database(self.root / "state/desk.db")
                db.conn.execute("BEGIN IMMEDIATE")
                require(self.source_valid(), "Source berubah selama pemeriksaan akun")
                previous = self.admit_proposal(db.conn, status, proposal, request_id)
                validate(config, status, db.conn)
                require(
                    load_config(self.root / "config.json") == config,
                    "Konfigurasi berubah saat pemeriksaan",
                )
                require(
                    str(snap.equity) == str(status["account"]["equity"]),
                    "Equity berubah saat pemeriksaan",
                )
                require(
                    sorted(snap.funding_flows)
                    == sorted(db.conn.execute("SELECT flow_id,amount FROM funding_flows")),
                    "Riwayat dana berubah",
                )
                require(
                    db.net_cash_status(str(snap.equity), "0")["status"] == "matched",
                    "Audit kas berubah",
                )
                candidate = Config.model_validate(
                    config.model_dump()
                    | {
                        "session_seconds": 18000,
                        "allow_live_orders": True,
                        "session_accept_reported_fees": True,
                        "probe_accept_reported_fees": False,
                    }
                )
                if previous:
                    archive_key = "ui_request:" + PROPOSAL + ":" + previous["request_id"]
                    archive = json.dumps(
                        {
                            "proposal": previous.get("proposal", PROPOSAL),
                            "result": previous,
                        }
                    )
                    existing = db.conn.execute(
                        "SELECT value FROM flags WHERE key=?", (archive_key,)
                    ).fetchone()
                    require(
                        not existing or json.loads(existing[0]) == json.loads(archive),
                        "Arsip permintaan lama berbeda; perlu pemeriksaan",
                    )
                    if not existing:
                        db.set_flag(archive_key, archive)
                record = BoundedSession(db).authorize(session_id, candidate, str(snap.equity))
                staged = True
                result = {
                    "request_id": request_id,
                    "session_id": session_id,
                    "proposal": proposal,
                    "state": "PREPARING",
                    "message": "Persetujuan tercatat; belum aktif",
                    "authorization_expires_epoch": record["authorization_expires_epoch"],
                }
                self.save_request(db, proposal, result)
                db.conn.commit()
                # Config rename and DB commit are separate units. Never claim atomic across both.
                require(
                    load_config(self.root / "config.json") == config,
                    "Konfigurasi berubah sebelum pemuatan izin",
                )
                atomic_json(self.root / "config.json", candidate.model_dump(mode="json"))
                db.command("LOAD_SESSION_PERMISSION")
                activation = ActivationRequests(db).submit(
                    {
                        "request_id": request_id,
                        "session_id": session_id,
                        "account_index": record["account_index"],
                        "parameter_hash": parameter_hash(candidate),
                        "authorization_expires_epoch": record["authorization_expires_epoch"],
                    }
                )
                result.update(
                    state=activation["status"],
                    message="Menunggu kesiapan dan audit aktual; belum aktif",
                )
                self.save_request(db, proposal, result)
                holder_lock, lock = lock, None
                self.hold(holder_lock, run, session_id)
                return result
            except BaseException:
                if db and db.conn.in_transaction:
                    db.conn.rollback()
                if staged and db:
                    # Human-request failure: one engine-owned revocation, no direct active rollback.
                    owned = BoundedSession(db).read()
                    if owned and owned.get("id") == session_id:
                        try:
                            db.command("STOP_ENTRIES")
                            failed = {
                                "request_id": request_id,
                                "session_id": session_id,
                                "proposal": proposal,
                                "state": "FAILED",
                                "message": "Persiapan gagal; hentikan entry dan pantau sampai bersih.",  # noqa: E501
                            }
                            self.save_request(db, proposal, failed)
                        finally:
                            if lock:
                                holder_lock, lock = lock, None
                                self.hold(holder_lock, run, session_id)
                raise
            finally:
                if db:
                    db.close()
                if lock:
                    self.owner(run, "closed", cleanup="no_permission_created")
                    lock.close()

    def owner(self, run: str, state: str, **extra) -> None:
        path = self.coord / "owner.json"
        if state == "closed":
            old = json.loads(path.read_text())
            require(
                old.get("run") == run and old.get("status") == "held",
                "Ownership UUID/status berubah",
            )
            old.update(status=state, closed_epoch=time.time(), **extra)
            atomic_json(path, old)
        else:
            atomic_json(
                path,
                {
                    "owner": "human/dashboard",
                    "run": run,
                    "status": state,
                    "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "holder_pid": os.getpid(),
                    "holder_start_ticks": int(
                        Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
                    ),
                    "started_epoch": time.time(),
                    "deadline_epoch": time.time() + 1200 + int(LIMITS["session_seconds"]) + 120,
                    "scope": "one manual UI request; engine owns all trading",
                },
            )

    def clean_closed(self, session_id: str) -> bool:
        status = status_readonly(self.root)
        record = status.get("strategy_session") or {}
        config = load_config(self.root / "config.json")
        with read_db(self.root) as db:
            intents = db.execute(
                "SELECT count(*) FROM intents WHERE state NOT IN ('FILLED','CANCELED','REJECTED')"
            ).fetchone()[0]
        return bool(
            status.get("connected")
            and time.time() - status["updated_at"] < 1
            and flat(status)
            and record.get("id") == session_id
            and record.get("status") == "closed"
            and not intents
            and not status.get("allow_live_orders")
            and not config.allow_live_orders
            and not config.session_accept_reported_fees
        )

    def hold(self, lock, run: str, session_id: str) -> None:
        def monitor():
            try:
                while not self.shutdown_event.wait(1):
                    try:
                        if self.clean_closed(session_id):
                            self.owner(run, "closed", cleanup="confirmed")
                            return
                    except Exception:
                        continue
                self.owner(
                    run,
                    "closed",
                    cleanup="unconfirmed_api_shutdown; inspect actual session before service operations",  # noqa: E501
                )
            finally:
                lock.close()

        thread = threading.Thread(target=monitor, name="ui-session-ownership", daemon=True)
        self.holders.append((lock, run, thread))
        try:
            thread.start()
        except BaseException:
            # Registry retains the real FD; never unwind ownership after staging.
            path = self.coord / "owner.json"
            try:
                old = json.loads(path.read_text())
                require(old.get("run") == run, "Ownership UUID berubah")
                old["monitor_failure"] = "unconfirmed; manual inspection required"
                atomic_json(path, old)
            except Exception:
                pass  # The held kernel lock remains authoritative while this process lives.
            raise

    def shutdown(self) -> None:
        self.shutdown_event.set()
        for lock, run, thread in self.holders:
            if lock.closed:
                continue
            if thread.ident is None:
                try:
                    self.owner(
                        run,
                        "closed",
                        cleanup="unconfirmed_api_shutdown; monitor never started",
                    )
                finally:
                    lock.close()
            else:
                thread.join(timeout=2)
