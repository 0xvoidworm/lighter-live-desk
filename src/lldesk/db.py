from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS flags(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS status(id INTEGER PRIMARY KEY CHECK(id=1),
  body TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'PENDING', requested_at REAL NOT NULL, result TEXT);
CREATE TABLE IF NOT EXISTS intents(client_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, side TEXT NOT NULL,
  qty TEXT NOT NULL, limit_price TEXT NOT NULL, state TEXT NOT NULL, tx_hash TEXT,
  reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY, client_id INTEGER NOT NULL,
  qty TEXT NOT NULL, price TEXT NOT NULL, fee TEXT,
  exchange_ms INTEGER, received_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS equity_marks(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
  equity TEXT NOT NULL, cumulative_flow TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS funding_flows(flow_id TEXT PRIMARY KEY, amount TEXT NOT NULL,
  recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
  kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=5, isolation_level=None)
        os.chmod(path, 0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO migrations VALUES(1, ?)", (time.time(),))
        self.conn.execute("INSERT OR IGNORE INTO migrations VALUES(2, ?)", (time.time(),))
        self._migrate_pending_fees()

    def _migrate_pending_fees(self) -> None:
        if self.conn.execute("SELECT 1 FROM migrations WHERE version=3").fetchone():
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "CREATE TABLE fills_new(fill_id TEXT PRIMARY KEY, client_id INTEGER NOT NULL, "
                "qty TEXT NOT NULL, price TEXT NOT NULL, fee TEXT, exchange_ms INTEGER, "
                "received_at REAL NOT NULL)"
            )
            self.conn.execute("INSERT INTO fills_new SELECT * FROM fills")
            self.conn.execute("DROP TABLE fills")
            self.conn.execute("ALTER TABLE fills_new RENAME TO fills")
            # Historical cash equality did not establish individual trade fees.
            corrected = []
            for (body,) in self.conn.execute("SELECT body FROM events WHERE kind='AUDITED_FILL'"):
                item = json.loads(body)
                if item.get("fee_source") == "aggregate cash audit; raw API fee absent":
                    self.conn.execute(
                        "UPDATE fills SET fee=NULL WHERE fill_id=?", (str(item["trade_id"]),)
                    )
                    corrected.append(str(item["trade_id"]))
            if corrected:
                self.event(
                    "FEE_PROVENANCE_CORRECTION",
                    {
                        "trade_ids": corrected,
                        "fee": None,
                        "reason": "cash delta does not prove individual trade fees",
                        "cash_audit_preserved": True,
                    },
                )
            self.conn.execute("INSERT INTO migrations VALUES(3, ?)", (time.time(),))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def pending_fees(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM fills WHERE fee IS NULL").fetchone()[0])

    def close(self) -> None:
        self.conn.close()

    def flag(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM flags WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def record_flows(self, flows: tuple[tuple[str, str], ...]) -> str:
        """Persist unique external flows and return the cumulative signed amount."""
        from decimal import Decimal

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for flow_id, amount in flows:
                self.conn.execute(
                    "INSERT OR IGNORE INTO funding_flows VALUES(?, ?, ?)",
                    (flow_id, amount, time.time()),
                )
                stored = self.conn.execute(
                    "SELECT amount FROM funding_flows WHERE flow_id=?", (flow_id,)
                ).fetchone()
                if stored is None or Decimal(stored[0]) != Decimal(amount):
                    raise ValueError("funding flow identity changed")
            total = sum(
                (Decimal(row[0]) for row in self.conn.execute("SELECT amount FROM funding_flows")),
                Decimal(0),
            )
            self.conn.commit()
            return str(total)
        except Exception:
            self.conn.rollback()
            raise

    def gross_deposits(self) -> str:
        from decimal import Decimal

        total = sum(
            (
                Decimal(row[0])
                for row in self.conn.execute(
                    "SELECT amount FROM funding_flows WHERE amount NOT LIKE '-%'"
                )
            ),
            Decimal(0),
        )
        return str(total)

    def set_flag(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO flags(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def save_status(self, body: dict[str, Any]) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO status VALUES(1, ?, ?) ON CONFLICT(id) "
                "DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
                (json.dumps(body, default=str), time.time()),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def read_status(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT body, updated_at FROM status WHERE id=1").fetchone()
        if not row:
            return {"state": "DISCONNECTED", "reason": "engine belum berjalan"}
        out: dict[str, Any] = json.loads(row[0])
        out["updated_at"] = row[1]
        return out

    def command(self, kind: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO commands(kind, requested_at) VALUES(?, ?)", (kind, time.time())
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def pending_commands(self) -> list[tuple[int, str]]:
        return self.conn.execute(
            "SELECT id, kind FROM commands WHERE state='PENDING' ORDER BY id LIMIT 20"
        ).fetchall()

    def finish_command(self, command_id: int, result: str) -> None:
        self.conn.execute(
            "UPDATE commands SET state='DONE', result=? WHERE id=?", (result, command_id)
        )

    def next_client_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(client_id), 0)+1 FROM intents").fetchone()
        return int(row[0])

    def intent(self, client_id: int, kind: str, side: str, qty: str, price: str) -> None:
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO intents VALUES(?, ?, ?, ?, ?, 'INTENT', NULL, NULL, ?, ?)",
                (client_id, kind, side, qty, price, now, now),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def order_state(
        self, client_id: int, state: str, tx_hash: str | None = None, reason: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE intents SET state=?, tx_hash=COALESCE(?,tx_hash), "
            "reason=?, updated_at=? WHERE client_id=?",
            (state, tx_hash, reason, time.time(), client_id),
        )

    def unresolved(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT client_id, kind, side, qty, limit_price, state FROM intents "
            "WHERE state NOT IN ('FILLED','CANCELED','REJECTED') ORDER BY client_id"
        ).fetchall()
        keys = ("client_id", "kind", "side", "qty", "limit_price", "state")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def add_fill(
        self,
        fill_id: str,
        client_id: int,
        qty: str,
        price: str,
        fee: str | None,
        exchange_ms: int | None,
    ) -> bool:
        from decimal import Decimal

        stored = self.conn.execute(
            "SELECT client_id,qty,price,fee FROM fills WHERE fill_id=?", (fill_id,)
        ).fetchone()
        if stored:
            if (
                stored[0] != client_id
                or Decimal(stored[1]) != Decimal(qty)
                or Decimal(stored[2]) != Decimal(price)
            ):
                raise ValueError("previously audited trade economics changed")
            if fee is not None:
                if stored[3] is not None and Decimal(stored[3]) != Decimal(fee):
                    raise ValueError("trade fee changed")
                self.conn.execute("UPDATE fills SET fee=? WHERE fill_id=?", (fee, fill_id))
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fills VALUES(?, ?, ?, ?, ?, ?, ?)",
            (fill_id, client_id, qty, price, fee, exchange_ms, time.time()),
        )
        return cur.rowcount == 1

    def event(self, kind: str, body: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO events(at, kind, body) VALUES(?,?,?)",
            (time.time(), kind, json.dumps(body, default=str)),
        )

    def recent_events(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT at, kind, body FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"at": x[0], "kind": x[1], "body": json.loads(x[2])} for x in rows]

    def backup(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target) as dest:
            self.conn.backup(dest)
        os.chmod(target, 0o600)
