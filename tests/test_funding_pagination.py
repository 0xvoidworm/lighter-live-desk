from types import SimpleNamespace

import pytest

from lldesk.account import LighterAccount
from lldesk.config import Config

KINDS = ("deposit", "withdraw", "transfer")
FIELDS = {"deposit": "deposits", "withdraw": "withdraws", "transfer": "transfers"}


def page(kind, items=(), cursor="", code=200):
    response = SimpleNamespace(code=code, cursor=cursor, deposits=[], withdraws=[], transfers=[])
    setattr(response, FIELDS[kind], list(items))
    return response


def flow(kind, identifier="fixture"):
    return SimpleNamespace(
        id=identifier,
        amount="12",
        asset_id=3,
        status="completed",
        type="L2TransferInflow",
        to_account_index=77,
        to_route="perps",
    )


def adapter_with_pages(kind, pages):
    adapter = LighterAccount(Config(mode="live_capable", account_index=77))
    calls = []

    def reader(selected_kind):
        async def fetch(**kwargs):
            if selected_kind != kind:
                return page(selected_kind)
            cursor = kwargs["cursor"]
            calls.append(cursor)
            return pages[cursor]

        return fetch

    adapter.transaction_api = SimpleNamespace(
        deposit_history=reader("deposit"),
        withdraw_history=reader("withdraw"),
        transfer_history=reader("transfer"),
    )
    adapter.auth = lambda: "fixture"  # type: ignore[method-assign]
    return adapter, calls


def expected_flow(kind):
    identifier = "transfer:fixture:L2TransferInflow" if kind == "transfer" else f"{kind}:fixture"
    return ((identifier, "-12" if kind == "withdraw" else "12"),)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("has_prior_flow", (False, True))
async def test_empty_terminal_page_echoes_requested_cursor(kind, has_prior_flow):
    # Mainnet withdrawal history echoes its cursor when the terminal page is empty.
    adapter, calls = adapter_with_pages(
        kind,
        {
            None: page(kind, [flow(kind)] if has_prior_flow else [], cursor="a"),
            "a": page(kind, cursor="a"),
        },
    )
    result = await adapter.funding_history("wallet")
    assert result == (expected_flow(kind) if has_prior_flow else ())
    assert calls == [None, "a"]


@pytest.mark.parametrize("kind", KINDS)
async def test_empty_advancing_pages_do_not_hide_later_funding(kind):
    adapter, calls = adapter_with_pages(
        kind,
        {
            None: page(kind, cursor="a"),
            "a": page(kind, cursor="b"),
            "b": page(kind, [flow(kind)]),
        },
    )
    assert await adapter.funding_history("wallet") == expected_flow(kind)
    assert calls == [None, "a", "b"]


@pytest.mark.parametrize("kind", KINDS)
async def test_nonempty_repeated_cursor_still_blocks_history(kind):
    adapter, _ = adapter_with_pages(
        kind,
        {
            None: page(kind, [flow(kind, "first")], cursor="a"),
            "a": page(kind, [flow(kind, "second")], cursor="a"),
        },
    )
    with pytest.raises(RuntimeError, match="pagination loop"):
        await adapter.funding_history("wallet")


@pytest.mark.parametrize("kind", KINDS)
async def test_empty_cursor_cycle_still_blocks_history(kind):
    adapter, _ = adapter_with_pages(
        kind,
        {
            None: page(kind, cursor="a"),
            "a": page(kind, cursor="b"),
            "b": page(kind, cursor="a"),
        },
    )
    with pytest.raises(RuntimeError, match="pagination loop"):
        await adapter.funding_history("wallet")


@pytest.mark.parametrize("kind", KINDS)
async def test_advancing_empty_history_retains_page_limit(kind):
    pages = {None: page(kind, cursor="0")}
    pages.update({str(i): page(kind, cursor=str(i + 1)) for i in range(4)})
    adapter, calls = adapter_with_pages(kind, pages)
    with pytest.raises(RuntimeError, match="exceeds verified page limit"):
        await adapter.funding_history("wallet")
    assert len(calls) == 5


@pytest.mark.parametrize("kind", KINDS)
async def test_failed_empty_terminal_response_still_blocks_history(kind):
    adapter, _ = adapter_with_pages(
        kind, {None: page(kind, cursor="a"), "a": page(kind, cursor="a", code=503)}
    )
    with pytest.raises(RuntimeError, match="history unavailable"):
        await adapter.funding_history("wallet")
