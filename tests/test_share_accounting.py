"""Share-accounting invariants — regressions for two bugs found in adversarial
review that the original suite missed.

1. Equity grants must be dilutive: when granted shares vest they are minted into
   the company's ``total_shares``, so the invariant ``total_shares == Σ holdings``
   holds (otherwise ownership percentages exceed 100% and share prices read high).
2. An ``$invest`` contribution too small to buy even one share must be refused
   before any money moves (otherwise the investor is charged for nothing).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from tendies import money
from tendies.config import SHARES_AT_FOUNDING
from tendies.errors import BadInput
from tendies.models import Company, Holding
from tendies.services import companies, employment, investment


async def _sum_holdings(world, company_id: int) -> int:
    return int(
        await world.session.scalar(
            select(func.coalesce(func.sum(Holding.shares), 0)).where(
                Holding.company_id == company_id
            )
        )
    )


async def test_vesting_dilutes_total_shares(world):
    """A 90,000-share grant that fully vests grows total_shares to 1,090,000,
    keeping total_shares == Σ holdings (the founder is diluted, not erased)."""
    owner, worker = 1001, 1002
    await world.make_rich(owner, 100_000)
    result = await companies.found_company(
        world.session, world.state, owner, "MOON", "Moon Mining Inc.", "materials"
    )
    cid = result.company.id
    await world.session.flush()

    job = await companies.post_job(
        world.session, world.state, owner, "MOON",
        "Driller", "dig", daily_wage=8000, equity_shares=90_000, vest_days=90,
    )
    await world.session.flush()
    await employment.apply_to_job(world.session, world.state, worker, job.id)
    apps = await companies.list_applicants(world.session, world.state, owner, "MOON")
    await companies.hire(world.session, world.state, owner, "MOON", apps[0].application_id)
    await world.session.flush()

    # Before vesting: only the founder's shares exist.
    company = await world.session.get(Company, cid)
    assert company.total_shares == SHARES_AT_FOUNDING
    assert await _sum_holdings(world, cid) == SHARES_AT_FOUNDING

    # Run a full vesting period of business-day ticks.
    await world.tick_business_days(90)

    company = await world.session.get(Company, cid)
    assert await world.holding(cid, worker) == 90_000  # fully vested
    assert company.total_shares == SHARES_AT_FOUNDING + 90_000  # minted, dilutive
    assert await _sum_holdings(world, cid) == company.total_shares  # the invariant


async def test_partial_vesting_keeps_invariant(world):
    """Mid-vest, total_shares == Σ holdings too (mint-on-vest, not mint-at-hire)."""
    owner, worker = 2001, 2002
    await world.make_rich(owner, 100_000)
    await companies.found_company(
        world.session, world.state, owner, "GKAS", "Gary's Kitchen", "food"
    )
    co = (await world.session.execute(select(Company).where(Company.ticker == "GKAS"))).scalar_one()
    job = await companies.post_job(
        world.session, world.state, owner, "GKAS",
        "Sauna Attendant", "steam", daily_wage=4000, equity_shares=10_000, vest_days=100,
    )
    await world.session.flush()
    await employment.apply_to_job(world.session, world.state, worker, job.id)
    apps = await companies.list_applicants(world.session, world.state, owner, "GKAS")
    await companies.hire(world.session, world.state, owner, "GKAS", apps[0].application_id)
    await world.session.flush()

    await world.tick_business_days(40)  # 40% through a 100-day vest

    co = await world.session.get(Company, co.id)
    worker_shares = await world.holding(co.id, worker)
    assert 0 < worker_shares < 10_000  # partially vested
    assert co.total_shares == SHARES_AT_FOUNDING + worker_shares
    assert await _sum_holdings(world, co.id) == co.total_shares


async def test_invest_too_small_to_buy_a_share_is_refused(world):
    """A tiny contribution that floors to 0 shares is rejected before any money
    moves — the investor isn't charged for nothing."""
    owner, investor = 3001, 3002
    await world.make_rich(owner, 100_000)
    await companies.found_company(
        world.session, world.state, owner, "SPCE", "Spacey Adventures", "tech"
    )
    # 10% of 1,000,000 shares -> 111,111 new shares for a 5,000,000 raise.
    await investment.open_round(world.session, world.state, owner, "SPCE", 5_000_000, 10)
    await world.session.flush()

    # Make the investor accredited via recorded wage income (no money moved).
    money.record_tx(
        world.session, guild_id=world.guild_id, game_day=world.state.game_day,
        type="wage", amount=40_000, user_id=investor,
    )
    await world.make_rich(investor, 1_000_000)
    await world.session.flush()

    supply_before = await world.money_supply()
    wallet_before = await world.wallet(investor)

    # 1 nug buys floor(111_111 * 1 / 5_000_000) == 0 shares -> must be refused.
    with pytest.raises(BadInput):
        await investment.invest(world.session, world.state, investor, "SPCE", 1)

    await world.session.flush()
    assert await world.wallet(investor) == wallet_before  # not charged
    await world.assert_supply(supply_before)  # nothing moved


async def test_open_round_rejects_unmintable_stake(world):
    """A stake so small it would mint zero new shares is refused at $raise time."""
    owner = 4001
    await world.make_rich(owner, 100_000)
    await companies.found_company(
        world.session, world.state, owner, "TINY", "Tiny Co", "finance"
    )
    await world.session.flush()
    # 1,000,000 * 0.000001 / (100-0.000001) rounds to 0 new shares.
    with pytest.raises(BadInput):
        await investment.open_round(world.session, world.state, owner, "TINY", 1000, 0.000001)
