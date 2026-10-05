"""Single-use strategy authorization and crash-durable dispatch quota."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .config import Config
from .db import Database

AUTHORIZATION_SECONDS = 1200


def parameter_hash(config: Config) -> str:
    sources = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ("strategy.py", "risk.py")
    }
    return hashlib.sha256(
        json.dumps(
            {"config": config.model_dump(mode="json"), "sources": sources}, sort_keys=True
        ).encode()
    ).hexdigest()


class BoundedSession:
    def __init__(self, db: Database):
        self.db = db

    def read(self) -> dict | None:
        raw = self.db.flag("strategy_session")
        return json.loads(raw) if raw else None

    def write(self, record: dict) -> None:
        self.db.set_flag("strategy_session", json.dumps(record))

    def authorize(self, session_id: str, config: Config, equity: str) -> dict:
        old = self.read()
        if old and old["status"] != "closed":
            raise ValueError("previous session authorization not closed")
        if config.live_entry_scope != "strategy" or not config.session_accept_reported_fees:
            raise ValueError("explicit bounded-session fee policy required")
        if not session_id or self.db.flag(f"session_used:{session_id}"):
            raise ValueError("session authorization already used")
        now = time.time()
        record = {
            "id": session_id,
            "status": "authorized",
            "authorized_at": now,
            "authorization_expires_epoch": now + AUTHORIZATION_SECONDS,
            "revoked_at": None,
            "started_at": None,
            "deadline_epoch": None,
            "ended_at": None,
            "max_seconds": min(1800, config.session_seconds),
            "max_attempts": min(3, config.max_session_positions),
            "attempts": [],
            "completed_positions": [],
            "parameter_hash": parameter_hash(config),
            "account_index": config.account_index,
            "equity_before": equity,
            "initial_fingerprint": self.db.accounting_fingerprint(),
            "audited_fingerprint": self.db.accounting_fingerprint(),
            "audited_equity": equity,
            "stop_reason": None,
            "fee_policy": "official_export_reported_precision_one_session",
        }
        self.write(record)
        self.db.event("STRATEGY_SESSION_AUTHORIZATION", record)
        return record

    def authorization_valid(self, record: dict | None = None) -> bool:
        record = self.read() if record is None else record
        if not record or record.get("status") != "authorized":
            return False
        try:
            expiry = float(record["authorization_expires_epoch"])
            return bool(
                float(record["authorized_at"])
                < expiry
                <= float(record["authorized_at"]) + AUTHORIZATION_SECONDS
                and time.time() < expiry
                and record.get("revoked_at") is None
                and record.get("started_at") is None
                and record.get("deadline_epoch") is None
                and record.get("ended_at") is None
                and not record.get("attempts")
                and not record.get("completed_positions")
                and 0 < record["max_seconds"] <= 1800
                and 0 < record["max_attempts"] <= 3
                and not self.db.flag(f"session_used:{record['id']}")
            )
        except (KeyError, TypeError, ValueError):
            return False

    def revoke(self, reason: str) -> None:
        record = self.read()
        if not record or record["status"] == "closed":
            return
        if record["status"] in {"active", "ending"}:
            self.stop(reason)
            return
        if record["status"] != "authorized":
            raise ValueError("unknown authorization state")
        record.update(
            status="closed", revoked_at=time.time(), ended_at=time.time(), stop_reason=reason
        )
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            self.write(record)
            self.db.set_flag(f"session_used:{record['id']}", "1")
            self.db.event("STRATEGY_SESSION_UNUSED_AUTHORIZATION_CLOSED", record)
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def activate(self, *, expected_id: str | None = None, expected_hash: str | None = None) -> dict:
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            record = self.read()
            if not self.authorization_valid(record):
                raise ValueError("no unused strategy session authorization")
            assert record is not None
            if (expected_id is not None and record["id"] != expected_id) or (
                expected_hash is not None and record["parameter_hash"] != expected_hash
            ):
                raise ValueError("authorization changed before activation")
            now = time.time()
            record.update(
                status="active", started_at=now, deadline_epoch=now + record["max_seconds"]
            )
            self.write(record)
            self.db.set_flag(f"session_used:{record['id']}", "1")
            self.db.event("STRATEGY_SESSION_STARTED", record)
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise
        return record

    def consume(self, client_id: int) -> bool:
        """Commit the quota AND SENT before any signer/network call, once per ID."""
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            record = self.read()
            if (
                not record
                or record["status"] != "active"
                or time.time() >= record["deadline_epoch"]
                or len(record["attempts"]) >= record["max_attempts"]
                or any(a["client_id"] == client_id for a in record["attempts"])
            ):
                self.db.conn.rollback()
                return False
            record["attempts"].append({"client_id": client_id, "consumed_at": time.time()})
            self.write(record)
            self.db.order_state(client_id, "SENT")
            self.db.event(
                "STRATEGY_ENTRY_QUOTA_CONSUMED",
                {
                    "session_id": record["id"],
                    "client_id": client_id,
                    "attempt": len(record["attempts"]),
                    "max_attempts": record["max_attempts"],
                    "before_signing": True,
                },
            )
            self.db.conn.commit()
            return True
        except Exception:
            self.db.conn.rollback()
            raise

    def stop(self, reason: str) -> None:
        record = self.read()
        if not record or record["status"] not in {"active", "ending"}:
            return
        if record["status"] == "active":
            record.update(status="ending", stop_reason=reason, entry_stopped_at=time.time())
            self.write(record)
            self.db.event(
                "STRATEGY_SESSION_END_REQUESTED", {"session_id": record["id"], "reason": reason}
            )

    def close(self) -> None:
        record = self.read()
        if not record or record["status"] != "ending":
            raise ValueError("session not ending")
        record.update(status="closed", ended_at=time.time())
        self.write(record)
        self.db.event("STRATEGY_SESSION_CLOSED", record)
