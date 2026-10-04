"""Approved reported fee scope and dispatch races with isolated exchanges only."""

import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from test_probe_only import drain
from test_probe_only import engine as engine_fixture

from lldesk.config import Config
from lldesk.engine import Engine
from lldesk.models import EngineState

engine = engine_fixture


def authorize(engine):
    engine.config = engine.config.model_copy(
        update={
            "live_entry_scope": "probe_only",
            "probe_accept_reported_fees": True,
        }
    )
    for cid, side, tid, stamp in [(1, "buy", "1001", 1000), (2, "sell", "1002", 2000)]:
        engine.db.intent(cid, "PROBE" if cid == 1 else "EXIT", side, "0.00012", "83000")
        engine.db.order_state(cid, "FILLED", f"tx-{cid}")
        engine.db.add_fill(tid, cid, "0.00012", "83000", None, stamp)
        engine.db.record_fee_report(
            tid,
            {
                "trade_id": tid,
                "client_id": cid,
                "qty": "0.00012",
                "price": "83000",
                "side": "Open Long" if side == "buy" else "Close Long",
                "market": "BTC",
                "role": "Taker",
                "reported_fee_usdc": "0.0000",
                "precision_decimals": 4,
                "source": "official_account_export",
                "account_index": 77,
                "transaction_hash": f"tx-{cid}",
                "exchange_ms": stamp,
                "evidence_sha256": "a" * 64,
            },
        )
    engine.db.set_flag("last_flat_epoch", str(time.time()))
    bind(engine)


def bind(engine):
    fingerprint = engine.db.accounting_fingerprint()
    engine.db.event(
        "NET_CASH_AUDIT",
        {
            "equity_after": str(engine.account_snapshot.equity),
            "cash_change_usdc": "0",
            "fingerprint": fingerprint,
        },
    )
    engine.db.set_flag("probe_session_id", "isolated-test-session")
    engine.db.set_flag(
        "probe_authorization_snapshot",
        json.dumps(
            {
                "session_id": "isolated-test-session",
                "account_index": 77,
                "fingerprint": fingerprint,
                "equity": str(engine.account_snapshot.equity),
            }
        ),
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("source", "unverified"),
        ("account_index", 88),
        ("side", "Close Long"),
        ("qty", "0.00013"),
        ("price", "83001"),
        ("transaction_hash", "different-tx"),
        ("exchange_ms", 1001),
        ("precision_decimals", 6),
        ("market", "ETH"),
        ("evidence_sha256", "missing"),
        ("trade_id", "9999"),
    ],
)
async def test_mismatched_report_cannot_enter(engine, key, value):
    authorize(engine)
    report = json.loads(engine.db.flag("fee_report:1001"))
    engine.db.set_flag("fee_report:1001", json.dumps({**report, key: value}))
    assert any("ekspor fee" in b for b in engine.probe_blockers())
    assert "diblok" in await engine.probe_once()
    assert not engine.account.sent


@pytest.mark.parametrize("incident", ["missing", "cash", "funding", "fill", "stale", "session"])
async def test_incomplete_cash_or_changed_history_blocks_preflight(engine, incident):
    authorize(engine)
    if incident == "missing":
        engine.db.conn.execute("DELETE FROM flags WHERE key='fee_report:1001'")
    elif incident == "cash":
        engine.account_snapshot = replace(engine.account_snapshot, equity=Decimal("49.430001"))
    elif incident == "funding":
        engine.db.record_flows((("unexpected-deposit", "1"),))
    elif incident == "fill":
        engine.db.add_fill("1001", 1, "0.00012", "83000", "0.000001", 1000)
    elif incident == "stale":
        engine.last_reconcile_ok = time.monotonic() - 16
    else:
        engine.db.set_flag("probe_session_id", "different-session")
    assert engine.probe_blockers()
    assert "diblok" in await engine.probe_once()
    assert not engine.account.sent


async def test_approved_reports_allow_one_probe_and_never_strategy(engine):
    authorize(engine)
    assert not engine.probe_blockers()
    assert engine.db.pending_fees() == 2
    assert "diblok" in engine.start_session()
    assert "menunggu" in await engine.probe_once()
    await drain(engine)
    assert len(engine.account.sent) == 1 and engine.account.sent[0].kind == "PROBE"
    assert engine.db.flag("probe_used") == "1"
    assert "diblok" in await engine.probe_once()
    assert len(engine.account.sent) == 1


async def test_history_change_while_waiting_for_signer_aborts_dispatch(engine):
    authorize(engine)
    engine.account.wait = True
    await engine.probe_once()
    await asyncio.wait_for(engine.account.entered.wait(), 1)
    engine.db.record_flows((("unexpected-deposit", "1"),))
    bind(engine)  # Even a refreshed cash reference must not replace the frozen preflight.
    engine.account.release.set()
    await drain(engine)
    assert not engine.account.sent
    assert engine.db.flag("probe_used") == "1"
    assert "diblok" in await engine.probe_once()


@pytest.mark.parametrize("accepted", [True, False])
async def test_new_unknown_fee_partial_fill_protects_then_closes_without_second_entry(
    engine, accepted
):
    authorize(engine)
    await engine.probe_once()
    await drain(engine)
    engine.account.send_stop = AsyncMock(return_value="stop ACK")
    if not accepted:
        engine.account.send_stop = AsyncMock(side_effect=TimeoutError)
    engine._record_trade(
        {
            "trade_id": 1003,
            "bid_account_id": 77,
            "ask_account_id": 88,
            "bid_client_id": 3,
            "is_maker_ask": True,
            "size": "0.00006",
            "price": "83000",
            "taker_fee": None,
            "timestamp": 3000,
        },
        77,
    )
    engine.account.current = replace(
        engine.account_snapshot, position_qty=Decimal("0.00006"), position_price=Decimal("83000")
    )
    engine.account.statuses = {3: {"status": "filled", "filled_base_amount": "0.00006"}}
    await engine.recover_exposure()
    await engine.manage_position()
    assert engine.db.pending_fees() == 3
    assert engine.account.send_stop.call_args.args[0].quantity == Decimal("0.00006")
    stop = {
        "client_order_index": 4,
        "order_index": 999,
        "market_index": 1,
        "status": "pending",
        "type": "stop-loss",
        "is_ask": True,
        "reduce_only": True,
        "remaining_base_amount": "0.00006",
        "filled_base_amount": "0",
        "trigger_price": "82709.5",
        "price": "82461.3",
    }
    if accepted:
        engine.account.statuses[4] = stop
        await engine.recover_exposure()
        assert engine.protection_confirmed
        assert not engine.stop_covers_position(
            {**stop, "client_order_index": 999}, engine.account_snapshot
        )
    else:
        assert not engine.protection_confirmed
    await engine.manage_position()
    await engine.manage_position()
    assert [x.kind for x in engine.account.sent] == ["PROBE", "EXIT"]
    assert engine.account.sent[-1].quantity == Decimal("0.00006")
    assert engine.db.pending_fees() == 3
    assert "diblok" in await engine.probe_once()


async def test_verified_cash_cleanup_can_finish_while_new_fee_metadata_remains_unknown(engine):
    authorize(engine)
    engine.probe_active = True
    engine.state = EngineState.PAUSED
    engine.db.set_flag("probe_pending", "1")
    engine.db.set_flag("probe_used", "1")
    engine.db.set_flag("close_requested", "1")
    for cid, side in [(3, "buy"), (4, "sell")]:
        engine.db.intent(cid, "PROBE" if side == "buy" else "EXIT", side, "0.00006", "83000")
        engine.db.order_state(cid, "FILLED", f"new-tx-{cid}")
        engine.db.add_fill(str(1000 + cid), cid, "0.00006", "83000", None, cid * 1000)
    await Engine.safe_reconcile(engine)
    assert engine.probe_active  # Cash reference changed; flat alone does not finish audit.
    engine.db.event(
        "NET_CASH_AUDIT",
        {
            "equity_after": "49.43",
            "cash_change_usdc": "0",
            "fingerprint": engine.db.accounting_fingerprint(),
        },
    )
    await Engine.safe_reconcile(engine)
    assert not engine.probe_active and engine.db.flag("probe_pending") == "0"
    assert engine.db.flag("close_requested") == "0"
    assert engine.db.pending_fees() == 4 and not engine.db.unresolved()
    assert "diblok" in await engine.probe_once()


def test_policy_config_cannot_enable_strategy():
    with pytest.raises(ValueError, match="restricted to one probe"):
        Config(live_entry_scope="strategy", probe_accept_reported_fees=True)


async def test_stop_entries_applies_local_order_revocation_without_restarting_feeds(engine):
    path = engine.root / "config.json"
    path.write_text(engine.config.model_copy(update={"allow_live_orders": False}).model_dump_json())
    original_strategy = engine.strategy
    engine.db.command("STOP_ENTRIES")
    await engine.commands()
    engine.decide()
    assert not engine.config.allow_live_orders and engine.entries_stopped
    assert engine.state == EngineState.IDLE
    assert engine.strategy is original_strategy and not engine.account.sent
