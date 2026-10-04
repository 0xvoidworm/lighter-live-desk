"""Official export evidence collected separately from the position risk worker."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from .account import LighterAccount
from .db import Database


def match_export(
    db: Database, account_index: int, content: bytes, trades: list[dict]
) -> list[dict]:
    raw = {str(t["trade_id"]): t for t in trades}
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    ids = {str(r[0]) for r in db.conn.execute("SELECT fill_id FROM fills")}
    if len(rows) != len(ids) or {r["Trade ID"] for r in rows} != ids:
        raise ValueError("official export incomplete or unexpected fills")
    digest = hashlib.sha256(content).hexdigest()
    reports = []
    for row in rows:
        tid = row["Trade ID"]
        trade = raw[tid]
        ask = trade["ask_account_id"] == account_index
        if ask == (trade["bid_account_id"] == account_index) or trade["market_id"] != 1:
            raise ValueError("export account/market mismatch")
        cid = trade["ask_client_id"] if ask else trade["bid_client_id"]
        if (
            Decimal(row["Size"]) != Decimal(trade["size"])
            or Decimal(row["Price"]) != Decimal(trade["price"])
            or Decimal(row["Trade Value"]) != Decimal(trade["size"]) * Decimal(trade["price"])
            or row["Date"]
            != datetime.fromtimestamp(trade["timestamp"] / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
            or row["Role"] != ("Maker" if bool(trade["is_maker_ask"]) == ask else "Taker")
        ):
            raise ValueError("official export economics/identity mismatch")
        intent = db.conn.execute(
            "SELECT state,tx_hash FROM intents WHERE client_id=?", (cid,)
        ).fetchone()
        if not intent:
            raise ValueError("export trade has no local intent")
        if intent[1] is None:
            db.order_state(cid, intent[0], trade["tx_hash"])
        fee = Decimal(row["Fee"])
        if not fee.is_finite():
            raise ValueError("invalid reported fee")
        report = {
            "trade_id": tid,
            "client_id": cid,
            "qty": row["Size"],
            "price": row["Price"],
            "side": row["Side"],
            "market": row["Market"],
            "role": row["Role"],
            "reported_fee_usdc": row["Fee"],
            "precision_decimals": -int(fee.as_tuple().exponent),
            "source": "official_account_export",
            "account_index": account_index,
            "transaction_hash": trade["tx_hash"],
            "exchange_ms": trade["timestamp"],
            "evidence_sha256": digest,
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "order_id": str(trade["ask_id"] if ask else trade["bid_id"]),
        }
        if not db.fee_report_matches(tid, report, account_index):
            raise ValueError("export does not match local fill")
        reports.append(report)
    return reports


async def collect_export(account: LighterAccount, db: Database, directory: Path) -> list[dict]:
    stamps = [row[0] for row in db.conn.execute("SELECT exchange_ms FROM fills")]
    if not stamps or any(stamp is None for stamp in stamps):
        raise ValueError("fill timestamps unavailable")
    lowest = min(int(row[0]) for row in db.conn.execute("SELECT fill_id FROM fills"))
    trades = await account.recent_trades(1, lowest - 1)
    account.rate.claim("reconcile")
    export = await account.order_api.export(
        authorization=account.auth(),
        account_index=account.account_index,
        market_id=1,
        type="trade",
        start_timestamp=min(stamps) - 60000,
        end_timestamp=max(stamps) + 60000,
        aggregate=False,
    )
    url = export.data_url
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".amazonaws.com")
    ):
        raise ValueError("unexpected official export host")

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def download() -> bytes:
        # urllib emits no INFO request log containing the presigned URL.
        with build_opener(NoRedirect).open(url, timeout=5) as response:
            content = response.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("export size limit")
        return content

    body = await asyncio.to_thread(download)
    # The signed URL, headers and auth token are never persisted or logged.
    reports = match_export(db, account.account_index, bytes(body), trades)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"trades-{hashlib.sha256(body).hexdigest()}.csv"
    path.write_bytes(body)
    path.chmod(0o600)
    for report in reports:
        db.record_fee_report(report["trade_id"], report)
    return reports
