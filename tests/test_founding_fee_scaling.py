"""Founding-fee scaling (§8).

fee = base_founding_fee * fee_multiplier ** companies_currently_owned
With base 50,000 and multiplier 4: 50,000 / 200,000 / 800,000 ...

And the "currently owned" basis: offloading a company (acquisition / bankruptcy)
lowers the next founding fee.
"""

from __future__ import annotations

from sqlalchemy import select

from tendies.config import BASE_FOUNDING_FEE, FEE_MULTIPLIER
from tendies.models import Company
from tendies.services import acquisitions, companies

OWNER = 5001
BUYER_OWNER = 5002


async def test_fee_scales_with_owned_count(world):
    w = world
    state = w.state
    await w.make_rich(OWNER, 5_000_000)

    r1 = await companies.found_company(w.session, state, OWNER, "AAA", "One", "tech")
    await w.session.flush()
    assert r1.fee == 50_000
    assert r1.fee == BASE_FOUNDING_FEE * FEE_MULTIPLIER ** 0
    assert r1.companies_owned_after == 1

    r2 = await companies.found_company(w.session, state, OWNER, "BBB", "Two", "tech")
    await w.session.flush()
    assert r2.fee == 200_000
    assert r2.fee == BASE_FOUNDING_FEE * FEE_MULTIPLIER ** 1
    assert r2.companies_owned_after == 2

    r3 = await companies.found_company(w.session, state, OWNER, "CCC", "Three", "tech")
    await w.session.flush()
    assert r3.fee == 800_000
    assert r3.fee == BASE_FOUNDING_FEE * FEE_MULTIPLIER ** 2
    assert r3.companies_owned_after == 3


async def test_offloading_a_company_lowers_next_fee(world):
    w = world
    state = w.state
    await w.make_rich(OWNER, 5_000_000)
    await w.make_rich(BUYER_OWNER, 50_000_000)

    # OWNER founds two companies; the third would cost 800,000.
    await companies.found_company(w.session, state, OWNER, "AAA", "One", "tech")
    await w.session.flush()
    sell_me = (await companies.found_company(w.session, state, OWNER, "BBB", "Two", "tech")).company
    await w.session.flush()

    # BUYER_OWNER founds an acquirer and buys BBB off OWNER, dropping OWNER to 1.
    buyer = (await companies.found_company(w.session, state, BUYER_OWNER, "BUY", "Buyer", "tech")).company
    await w.session.flush()
    state.pool_balance -= 10_000_000
    buyer.treasury += 10_000_000
    await w.session.flush()

    await acquisitions.offer(w.session, state, BUYER_OWNER, "BUY", "BBB", 1_000_000)
    await w.session.flush()
    await acquisitions.accept(w.session, state, OWNER, "BUY")
    await w.session.flush()

    # BBB is no longer active / owned by OWNER, so OWNER currently owns 1 (AAA).
    await w.session.refresh(sell_me)
    assert sell_me.active is False

    owned = (
        await w.session.execute(
            select(Company).where(
                Company.guild_id == state.guild_id,
                Company.owner_id == OWNER,
                Company.active == True,  # noqa: E712
                Company.is_state == False,  # noqa: E712
            )
        )
    ).scalars().all()
    assert len(owned) == 1

    # OWNER's next founding fee is back to the 2nd-company price (200,000), not 800,000.
    r = await companies.found_company(w.session, state, OWNER, "CCC", "Three", "tech")
    await w.session.flush()
    assert r.fee == 200_000
    assert r.companies_owned_after == 2
