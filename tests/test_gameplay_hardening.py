"""Regression coverage for player-facing economy hardening."""

from __future__ import annotations

import pytest

from tendies import money
from tendies.discordutil import parse_amount
from tendies.errors import BadInput, GameError, NotAllowed
from tendies.models import FundingRound
from tendies.services import acquisitions, companies, employment, investment

from cog_harness import Harness


async def _found(world, owner: int, ticker: str):
    await world.make_rich(owner, 2_000_000)
    result = await companies.found_company(
        world.session, world.state, owner, ticker, f"{ticker} Co", "tech"
    )
    await world.session.flush()
    return result.company


def test_amount_parser_rejects_values_outside_sqlite_integer_range():
    assert parse_amount(str(money.MAX_INT64)) == money.MAX_INT64
    with pytest.raises(BadInput, match="too large"):
        parse_amount(str(money.MAX_INT64 + 1))
    with pytest.raises(BadInput):
        parse_amount("1e999")


async def test_print_rejects_total_supply_overflow(world):
    with pytest.raises(BadInput, match="money supply"):
        await money.print_money(
            world.session,
            world.state,
            money.MAX_INT64,
            world.state.game_day,
        )


async def test_nonfinite_percentages_and_oversized_job_values_are_rejected(world):
    owner = 4
    await _found(world, owner, "SAFE")
    for percentage in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(BadInput):
            await investment.open_round(
                world.session, world.state, owner, "SAFE", 100_000, percentage
            )
    with pytest.raises(BadInput):
        await companies.promote(
            world.session, world.state, owner, 999, float("nan")
        )
    with pytest.raises(BadInput, match="too large"):
        await companies.post_job(
            world.session,
            world.state,
            owner,
            "SAFE",
            "Impossible wage",
            "",
            money.MAX_INT64 + 1,
            0,
            0,
        )


async def test_deposit_and_closeround_commands_are_wired():
    harness = await Harness.create()
    try:
        owner = 5
        await harness.fund_wallet(owner, 1_000_000)
        await harness.invoke(
            harness.ctx(owner), "found", args='WIRE "Wired Co" tech'
        )

        ctx = await harness.invoke(harness.ctx(owner), "deposit", "WIRE", "100K")
        assert "Deposited into WIRE" in ctx.last_text()
        assert "100,000" in ctx.last_text()

        await harness.invoke(harness.ctx(owner), "raise", "WIRE", "500K", 10.0)
        denied = await harness.invoke(harness.ctx(6), "closeround", "WIRE")
        assert "owner" in denied.last_text()
        closed = await harness.invoke(
            harness.ctx(7, manager=True), "closeround", "WIRE"
        )
        assert "funding round closed" in closed.last_text()
    finally:
        await harness.close()


async def test_owner_deposit_transfers_existing_money_and_conserves_supply(world):
    owner, stranger = 10, 11
    company = await _found(world, owner, "BOOT")
    supply_before = await world.money_supply()
    wallet_before = await world.wallet(owner)

    result = await investment.deposit(
        world.session, world.state, owner, "BOOT", 250_000
    )
    await world.session.flush()

    assert result.treasury_after == 250_000
    assert company.treasury == 250_000
    assert await world.wallet(owner) == wallet_before - 250_000
    await world.assert_supply(supply_before)

    await world.make_rich(stranger, 300_000)
    with pytest.raises(NotAllowed):
        await investment.deposit(
            world.session, world.state, stranger, "BOOT", 100_000
        )


async def test_round_can_be_closed_by_owner_or_explicit_manager(world):
    owner = 20
    await _found(world, owner, "RND")
    await investment.open_round(world.session, world.state, owner, "RND", 500_000, 10)

    with pytest.raises(NotAllowed):
        await investment.close_round(
            world.session, world.state, 999, "RND", manager=False
        )
    result = await investment.close_round(
        world.session, world.state, 999, "RND", manager=True
    )
    assert result.amount_raised == 0

    await investment.open_round(world.session, world.state, owner, "RND", 300_000, 5)
    result = await investment.close_round(world.session, world.state, owner, "RND")
    assert result.target_amount == 300_000


async def test_partial_investments_use_cumulative_rounding(world):
    owner, first, second = 30, 31, 32
    company = await _found(world, owner, "FILL")
    await investment.open_round(world.session, world.state, owner, "FILL", 6, 10)
    for investor_id in (first, second):
        money.record_tx(
            world.session,
            guild_id=world.guild_id,
            game_day=world.state.game_day,
            type="wage",
            amount=40_000,
            user_id=investor_id,
        )
        await world.make_rich(investor_id, 10)
    await world.session.flush()

    one = await investment.invest(world.session, world.state, first, "FILL", 1)
    two = await investment.invest(world.session, world.state, second, "FILL", 1)

    assert one.shares == 18_518
    assert two.shares == 18_519
    round_ = await world.session.get(FundingRound, 1)
    assert round_.shares_minted == 37_037
    assert company.total_shares == 1_037_037


async def test_weekend_blocks_invest_dividend_and_acquisition_close(world):
    owner, investor, buyer_owner, target_owner = 40, 41, 42, 43
    dividend_co = await _found(world, owner, "WKND")
    await investment.open_round(world.session, world.state, owner, "WKND", 100_000, 10)
    await world.make_rich(investor, 200_000)
    money.record_tx(
        world.session,
        guild_id=world.guild_id,
        game_day=world.state.game_day,
        type="wage",
        amount=40_000,
        user_id=investor,
    )
    dividend_co.treasury = 100_000
    world.state.pool_balance -= 100_000

    buyer = await _found(world, buyer_owner, "BUY")
    await _found(world, target_owner, "TGT")
    buyer.treasury = 500_000
    world.state.pool_balance -= 500_000
    await acquisitions.offer(
        world.session, world.state, buyer_owner, "BUY", "TGT", 100_000
    )
    await world.set_weekday("saturday")

    for operation in (
        investment.invest(world.session, world.state, investor, "WKND", 10_000),
        investment.pay_dividend(world.session, world.state, owner, "WKND", 10_000),
        acquisitions.accept(world.session, world.state, target_owner, "BUY"),
    ):
        with pytest.raises(NotAllowed, match="Monday"):
            await operation


async def test_acquisition_target_can_be_disambiguated(world):
    buyer_owner, target_owner = 50, 51
    buyer = await _found(world, buyer_owner, "ACQ")
    await _found(world, target_owner, "ONE")
    await _found(world, target_owner, "TWO")
    buyer.treasury = 500_000
    world.state.pool_balance -= 500_000
    await acquisitions.offer(
        world.session, world.state, buyer_owner, "ACQ", "ONE", 100_000
    )
    await acquisitions.offer(
        world.session, world.state, buyer_owner, "ACQ", "TWO", 100_000
    )

    with pytest.raises(GameError, match="Choose one"):
        await acquisitions.accept(world.session, world.state, target_owner, "ACQ")
    result = await acquisitions.accept(
        world.session, world.state, target_owner, "ACQ", "TWO"
    )
    assert result.target_ticker == "TWO"


async def test_hire_cannot_reuse_application_and_clocked_shift_is_protected(world):
    owner, worker = 60, 61
    await _found(world, owner, "WORK")
    job = await companies.post_job(
        world.session, world.state, owner, "WORK", "Worker", "", 1_000, 0, 0
    )
    await employment.apply_to_job(world.session, world.state, worker, job.id)
    apps = await companies.list_applicants(world.session, world.state, owner, "WORK")
    application_id = apps[0].application_id
    await companies.hire(
        world.session, world.state, owner, "WORK", application_id
    )

    with pytest.raises(BadInput, match="no longer pending"):
        await companies.hire(
            world.session, world.state, owner, "WORK", application_id
        )

    await employment.clock_in(world.session, world.state, worker)
    with pytest.raises(NotAllowed, match="clocked in"):
        await companies.fire(world.session, world.state, owner, "WORK", worker)
    with pytest.raises(GameError, match="Finish today's tick"):
        await employment.quit_job(world.session, world.state, worker, "WORK")
