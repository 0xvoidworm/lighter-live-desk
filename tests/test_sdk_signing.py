"""Exercise the pinned native SDK signer; only nonce lookup/submission are replaced.

Keys are ephemeral and never read from the real account. No network is allowed.
Captured fields are explicitly whitelisted so failures cannot print signatures.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from importlib.metadata import distribution, version
from types import SimpleNamespace

import aiohttp
import lighter
import pytest
import requests
from lighter.signer_client import create_api_key

from lldesk.account import LighterAccount
from lldesk.config import Config
from lldesk.models import OrderIntent, Stamp

PUBLIC_FIELDS = {
    "AccountIndex",
    "ApiKeyIndex",
    "MarketIndex",
    "ClientOrderIndex",
    "BaseAmount",
    "Price",
    "IsAsk",
    "Type",
    "TimeInForce",
    "ReduceOnly",
    "TriggerPrice",
    "OrderExpiry",
    "Nonce",
    "Index",
}


@pytest.fixture
async def native_adapter(monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("offline signing test attempted network I/O")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", forbid_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbid_network)
    private_key, public_key, error = create_api_key()
    if error:
        pytest.fail("SDK ephemeral key generation failed")
    signer = lighter.SignerClient(
        url="https://signing-test.invalid",
        account_index=999998,
        api_private_keys={4: private_key},
        chain_id=304,
        nonce_management_type=lighter.nonce_manager.NonceManagerType.API,
    )
    del private_key, public_key
    submissions = []
    next_nonce = 1

    async def offline_nonce(api_key_index):
        nonlocal next_nonce
        nonce = next_nonce
        next_nonce += 1
        return api_key_index, nonce

    async def offline_transport(*, tx_type, tx_info):
        payload = json.loads(tx_info)
        submissions.append(
            (tx_type, {key: value for key, value in payload.items() if key in PUBLIC_FIELDS})
        )
        return SimpleNamespace(code=200, tx_hash="offline-not-broadcast")

    monkeypatch.setattr(signer.nonce_manager, "async_next_nonce", offline_nonce)
    monkeypatch.setattr(signer, "send_tx", offline_transport)
    adapter = LighterAccount(Config(account_index=999998))
    adapter.signer = signer
    try:
        yield adapter, submissions
    finally:
        await adapter.close()


def test_sdk_distribution_matches_reviewed_pin():
    origin = json.loads(distribution("lighter-sdk").read_text("direct_url.json"))
    assert version("lighter-sdk") == "1.1.5"
    assert origin["vcs_info"]["commit_id"] == "78db23f632de8deca9a232a61bbd3bf6e79fc522"


async def test_original_ioc_expiry_rejected_by_native_signer_before_transport(native_adapter):
    adapter, submissions = native_adapter
    tx, response, error = await adapter.signer.create_order(
        market_index=1,
        client_order_index=1,
        base_amount=12,
        price=845586,
        is_ask=False,
        order_type=0,
        time_in_force=0,
        reduce_only=False,
        order_expiry=-1,
    )
    assert tx is None and response is None
    assert error == "OrderExpiry is invalid"
    assert submissions == []


async def test_adapter_entry_stop_cancel_close_use_valid_native_signing(native_adapter):
    adapter, submissions = native_adapter
    entry = OrderIntent(2, "PROBE", "buy", Decimal("0.00012"), Decimal("84558.6"), Stamp())
    assert await adapter.send_ioc(entry, 1, 5, 1, dispatch_check=lambda: True) == (
        "offline-not-broadcast"
    )
    # Partial fills must sign protection and close for the actual remaining size.
    stop = OrderIntent(3, "STOP", "sell", Decimal("0.00006"), Decimal("84000.0"), Stamp())
    await adapter.send_stop(stop, 1, 5, 1, Decimal("84200.0"))
    await adapter.cancel(1, 281474976710658)
    close = OrderIntent(4, "EXIT", "sell", Decimal("0.00006"), Decimal("83900.0"), Stamp())
    await adapter.send_ioc(close, 1, 5, 1)

    assert len(submissions) == 4
    for _, fields in submissions:
        assert fields["AccountIndex"] == 999998
        assert fields["ApiKeyIndex"] == 4
        assert fields["MarketIndex"] == 1
    entry_type, entry_fields = submissions[0]
    assert entry_type == 14
    assert entry_fields["BaseAmount"] == 12
    assert entry_fields["Price"] == 845586
    assert entry_fields["IsAsk"] == 0
    assert entry_fields["Type"] == 0
    assert entry_fields["TimeInForce"] == 0
    assert entry_fields["ReduceOnly"] == 0
    assert entry_fields["OrderExpiry"] == 0
    stop_type, stop_fields = submissions[1]
    assert stop_type == 14
    assert stop_fields["BaseAmount"] == 6
    assert stop_fields["Price"] == 840000
    assert stop_fields["IsAsk"] == 1
    assert stop_fields["Type"] == 2
    assert stop_fields["ReduceOnly"] == 1
    assert stop_fields["TriggerPrice"] == 842000
    assert stop_fields["OrderExpiry"] > int(time.time() * 1000)
    cancel_type, cancel_fields = submissions[2]
    assert cancel_type == 15
    assert cancel_fields["Index"] == 281474976710658
    close_type, close_fields = submissions[3]
    assert close_type == 14
    assert close_fields["BaseAmount"] == 6
    assert close_fields["Price"] == 839000
    assert close_fields["IsAsk"] == 1
    assert close_fields["ReduceOnly"] == 1
    assert close_fields["OrderExpiry"] == 0
