"""Authenticate export economics against official trade identities, offline."""

import json
from datetime import UTC, datetime

import pytest
from test_probe_only import engine as engine_fixture
from test_reported_fee_probe import authorize

from lldesk.fee_audit import match_export

engine = engine_fixture


def evidence(engine):
    authorize(engine)
    trades, rows = [], []
    for tid, cid, side, stamp in [("1001", 1, "Open Long", 1000), ("1002", 2, "Close Long", 2000)]:
        ask = side == "Close Long"
        trade = {
            "trade_id": int(tid),
            "market_id": 1,
            "size": "0.00012",
            "price": "83000",
            "ask_account_id": 77 if ask else 88,
            "bid_account_id": 88 if ask else 77,
            "ask_client_id": cid if ask else 100,
            "bid_client_id": 100 if ask else cid,
            "ask_id": 700 + cid,
            "bid_id": 600 + cid,
            "timestamp": stamp,
            "is_maker_ask": not ask,
            "tx_hash": f"tx-{cid}",
        }
        trades.append(trade)
        rows.append(
            {
                "Market": "BTC",
                "Side": side,
                "Date": datetime.fromtimestamp(stamp / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "Trade Value": "9.96000",
                "Size": "0.00012",
                "Price": "83000",
                "Closed PnL": "-",
                "Fee": "0.0000",
                "Role": "Taker",
                "Type": "trade",
                "Trade ID": tid,
            }
        )
    return trades, rows


def encode(rows):
    import csv
    import io

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def test_matching_csv_preserves_exact_null_and_separate_capture_time(engine):
    trades, rows = evidence(engine)
    reports = match_export(engine.db, 77, encode(rows), trades)
    for report in reports:
        assert report["reported_fee_usdc"] == "0.0000" and report["precision_decimals"] == 4
        assert datetime.fromisoformat(report["captured_at_utc"]).tzinfo is not None
        engine.db.record_fee_report(report["trade_id"], report)
    assert engine.db.pending_fees() == 2
    assert json.loads(engine.db.flag("fee_report:1001"))["source"] == "official_account_export"


@pytest.mark.parametrize(
    "field,value",
    [
        ("Trade ID", "9999"),
        ("Price", "83001"),
        ("Size", "0.00013"),
        ("Trade Value", "10"),
        ("Date", "2026-10-04 00:00:00"),
        ("Side", "Close Long"),
        ("Market", "ETH"),
        ("Role", "Maker"),
        ("Fee", "NaN"),
    ],
)
def test_invalid_csv_never_blesses_exact_fee(engine, field, value):
    trades, rows = evidence(engine)
    rows[0][field] = value
    with pytest.raises((ValueError, KeyError)):
        match_export(engine.db, 77, encode(rows), trades)
    assert engine.db.pending_fees() == 2


def test_missing_row_blocks_export_audit(engine):
    trades, rows = evidence(engine)
    with pytest.raises(ValueError, match="incomplete"):
        match_export(engine.db, 77, encode(rows[:1]), trades)
