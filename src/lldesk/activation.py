"""Durable control admission, never an entry/order authorization or transport."""

from __future__ import annotations

import json
import time

from .db import Database

PENDING = {"QUEUED", "WAITING"}
AUDIT_RETRY_SECONDS = 30.0  # At most 16 activation-audit GET/minute, still budget-admitted.
TEMPORARY_GUARDS = {
    "request_capacity",
    "reconcile_busy",
    "account_connection",
    "metadata",
    "engine_ready",
    "history",
    "account_state",
    "reconcile_ok",
    "market_basis",
    "source",
    "account_age",
    "coverage",
}


def terminal_failures(assessment: dict) -> set[str]:
    temporary = set(TEMPORARY_GUARDS)
    if assessment["checks"]["fee_cash_binding"].get("reasons") == [
        "audit kas/dana probe belum segar"
    ]:
        temporary.add("fee_cash_binding")
    return {
        name for name, check in assessment["checks"].items() if check["result"] == "failed"
    } - temporary


class ActivationRequests:
    def __init__(self, db: Database):
        self.db = db

    def read(self, request_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT body FROM activation_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self) -> dict | None:
        row = self.db.conn.execute(
            "SELECT body FROM activation_requests ORDER BY command_id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0]) if row else None

    def write(self, record: dict) -> None:
        self.db.conn.execute(
            "UPDATE activation_requests SET status=?,body=? WHERE request_id=?",
            (record["status"], json.dumps(record), record["request_id"]),
        )

    def submit(self, binding: dict) -> dict:
        """Idempotent admission + command journal, in one transaction."""
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            old = self.read(binding["request_id"])
            if old:
                if any(old[k] != v for k, v in binding.items()):
                    raise ValueError("request ID already bound to different authorization")
                self.db.conn.commit()
                return old
            if self.db.conn.execute(
                "SELECT 1 FROM activation_requests WHERE status IN ('QUEUED','WAITING') "
                "OR session_id=? LIMIT 1",
                (binding["session_id"],),
            ).fetchone():
                raise ValueError("one pending request and one request per authorization only")
            command_id = self.db.command("START_WHEN_READY")
            wall, mono = time.time(), time.monotonic()
            record = dict(
                binding,
                command_id=command_id,
                requested_at=wall,
                requested_monotonic=mono,
                expires_monotonic=mono + max(0, binding["authorization_expires_epoch"] - wall),
                status="QUEUED",
                reason="queued; entries remain stopped",
                assessment=None,
                terminal_at=None,
            )
            self.db.conn.execute(
                "INSERT INTO activation_requests VALUES(?,?,?,?,?)",
                (
                    record["request_id"],
                    record["session_id"],
                    command_id,
                    record["status"],
                    json.dumps(record),
                ),
            )
            self.db.event("ACTIVATION_REQUESTED", record)
            self.db.conn.commit()
            return record
        except Exception:
            self.db.conn.rollback()
            raise

    def waiting(self, record: dict) -> None:
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            self.write(record)
            self.db.conn.execute(
                "UPDATE commands SET state='WAITING',result=? WHERE id=?",
                (record["reason"], record["command_id"]),
            )
            self.db.event("ACTIVATION_WAITING", record)
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def finish(self, record: dict, status: str, reason: str, assessment: dict | None) -> None:
        record = dict(
            record,
            status=status,
            reason=reason,
            assessment=assessment,
            terminal_at=time.time(),
            control_transition={
                "command_id": record["command_id"],
                "request_id": record["request_id"],
                "session_id": record["session_id"],
                "wall_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "result": status,
                "reason": reason,
                "assessment_role": "last_readiness_only"
                if status == "CANCELLED"
                else "terminal_guard_decision",
                "readiness_skipped": status == "CANCELLED",
            },
        )
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            self.write(record)
            self.db.finish_command(record["command_id"], status + ": " + reason)
            self.db.event("ACTIVATION_CONTROL_DECISION", record)
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def cancel_pending(self, reason: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT body FROM activation_requests WHERE status IN ('QUEUED','WAITING')"
        ).fetchall()
        records = [json.loads(row[0]) for row in rows]
        for record in records:
            self.finish(record, "CANCELLED", reason, record.get("assessment"))
        return records
