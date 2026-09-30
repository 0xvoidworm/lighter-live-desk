from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn

from .account import LighterAccount
from .config import load_config, repo_root
from .db import Database
from .engine import run_engine
from .market import market_metadata


def main() -> None:
    parser = argparse.ArgumentParser(prog="lldesk")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("engine")
    sub.add_parser("api")
    sub.add_parser("check")
    sub.add_parser("market-check")
    sub.add_parser("account-check")
    backup = sub.add_parser("backup")
    backup.add_argument("path", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "engine":
        try:
            asyncio.run(run_engine())
        except KeyboardInterrupt:
            pass
    elif args.command == "api":
        uvicorn.run("lldesk.api:app", host="127.0.0.1", port=8765, log_level="warning")
    elif args.command == "check":
        config = load_config()
        db = Database(repo_root() / "state" / "desk.db")
        try:
            check = db.conn.execute("PRAGMA quick_check").fetchone()[0]
            print(f"config OK: {config.mode}; SQLite: {check}")
        finally:
            db.close()
    elif args.command == "market-check":
        m = asyncio.run(market_metadata())
        for key in (
            "symbol",
            "market_id",
            "market_type",
            "min_base_amount",
            "min_quote_amount",
            "size_decimals",
            "price_decimals",
            "taker_fee",
        ):
            print(f"{key}: {m[key]}")
    elif args.command == "account-check":
        asyncio.run(account_check())
    elif args.command == "backup":
        db = Database(repo_root() / "state" / "desk.db")
        try:
            db.backup(args.path)
            print(f"backup: {args.path}")
        finally:
            db.close()


async def account_check() -> None:
    config = load_config()
    market = await market_metadata()
    adapter = LighterAccount(config)
    try:
        await adapter.open()
        snapshot = await adapter.snapshot(int(market["market_id"]))
        print(f"account_index: {adapter.account_index}")
        print(f"account_type_code: {snapshot.account_type_code}")
        print(f"equity: {snapshot.equity}")
        print(f"available: {snapshot.available}")
        print(f"BTC_position_qty: {snapshot.position_qty}")
        print(f"active_orders: {len(snapshot.active_orders)}")
        print(f"other_positions: {len(snapshot.other_positions)}")
        print(f"other_assets: {len(snapshot.other_assets)}")
    finally:
        await adapter.close()
