"""A normal-start multiplayer path through the first 30 business days.

Unlike most focused service tests, this scenario does not inject money into a
wallet or treasury. Players begin broke, earn state wages, and build a private
company entirely through public economy rules.
"""

from __future__ import annotations

from sqlalchemy import func, select

from tendies import gameday
from tendies.config import SHARES_AT_FOUNDING, STARTING_POOL
from tendies.models import Holding, Transaction
from tendies.services import companies, employment, investment

from conftest import first_state_job


FOUNDER = 81_001
WORKER = 81_002


async def _advance_to_business_day(world) -> None:
    while not gameday.is_business_day(world.state.game_day):
        report = await world.tick()
        assert report.closed is True


async def test_broke_players_build_a_company_in_thirty_business_days(world):
    state = world.state
    _, state_job_id = await first_state_job(world, "Fry Cook")

    # Both players take a normal, always-open state job with zero starting cash.
    assert await world.wallet(FOUNDER) == 0
    assert await world.wallet(WORKER) == 0
    await employment.apply_to_job(world.session, state, FOUNDER, state_job_id)
    await employment.apply_to_job(world.session, state, WORKER, state_job_id)

    business_days = 0
    for _ in range(12):
        await _advance_to_business_day(world)
        await employment.clock_in(world.session, state, FOUNDER)
        await employment.clock_in(world.session, state, WORKER)
        report = await world.tick()
        assert report.is_business_day is True
        business_days += 1
        await world.assert_supply(STARTING_POOL)

    # Twelve fry-cook shifts plus the ordinary 10-day streak reward make the
    # first 50K founding fee reachable without test-only funding.
    assert await world.wallet(FOUNDER) >= 50_000
    company = (
        await companies.found_company(
            world.session,
            state,
            FOUNDER,
            "PLAY",
            "Playable Economy Co",
            "food",
        )
    ).company
    await investment.deposit(world.session, state, FOUNDER, "PLAY", 1_000)
    await world.assert_supply(STARTING_POOL)

    # The second player leaves the state job, applies normally, and is hired
    # into a reusable private role with a grant that spans the remaining days.
    await employment.quit_job(world.session, state, WORKER)
    job = await companies.post_job(
        world.session,
        state,
        FOUNDER,
        "PLAY",
        "Cook",
        "Turn labor into private output.",
        daily_wage=3_000,
        equity_shares=18_000,
        vest_days=18,
    )
    await employment.apply_to_job(world.session, state, WORKER, job.id)
    applicants = await companies.list_applicants(
        world.session, state, FOUNDER, "PLAY"
    )
    assert [app.user_id for app in applicants] == [WORKER]
    await companies.hire(
        world.session, state, FOUNDER, "PLAY", applicants[0].application_id
    )

    dividend = None
    for private_day in range(1, 19):
        await _advance_to_business_day(world)

        # Pay a real two-shareholder dividend after 17 grant-vesting closes and
        # before the 30th close. The final grant slice vests at that close.
        if private_day == 18:
            assert await world.holding(company.id, WORKER) == 17_000
            dividend = await investment.pay_dividend(
                world.session, state, FOUNDER, "PLAY", 10_000
            )
            assert sum(p.gross for p in dividend.payouts) == 10_000
            assert {p.user_id for p in dividend.payouts} == {FOUNDER, WORKER}

        # The founder keeps earning at the state job while the worker powers the
        # private company. Both flows settle together through the same pool.
        await employment.clock_in(world.session, state, FOUNDER)
        await employment.clock_in(world.session, state, WORKER)
        report = await world.tick()
        assert report.is_business_day is True
        business_days += 1
        await world.assert_supply(STARTING_POOL)

    assert business_days == 30
    assert dividend is not None
    assert company.active is True
    assert company.treasury >= 0

    # The grant finished exactly and the core cap-table invariant still holds.
    assert await world.holding(company.id, FOUNDER) == SHARES_AT_FOUNDING
    assert await world.holding(company.id, WORKER) == 18_000
    held = int(
        await world.session.scalar(
            select(func.coalesce(func.sum(Holding.shares), 0)).where(
                Holding.company_id == company.id
            )
        )
    )
    assert company.total_shares == SHARES_AT_FOUNDING + 18_000 == held

    # Evidence that the path used the live economy rather than fixture funding.
    tx_types = set(
        (
            await world.session.execute(
                select(Transaction.type).where(
                    Transaction.guild_id == world.guild_id
                )
            )
        ).scalars()
    )
    assert {"state_wage", "founding_fee", "deposit", "revenue", "wage", "dividend"} <= tx_types
    await world.assert_supply(STARTING_POOL)

