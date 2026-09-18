"""The accredited-investor gate (§11).

annualized_income = trailing_income(last 30 business days) * (250 / 30)
Below ACCREDITED_THRESHOLD (200,000/yr) -> NotAllowed. A broke user fails; a user
who has earned enough wage income (driven by ticks) clears the gate and invests.
"""

from __future__ import annotations

import pytest

from tendies import gameday
from tendies.config import (
    ACCREDITED_THRESHOLD,
    BUSINESS_DAYS_PER_YEAR,
    INCOME_WINDOW_DAYS,
)
from tendies.errors import NotAllowed
from tendies.money import trailing_income
from tendies.services import companies, employment, investment

from conftest import first_state_job

FOUNDER = 6001
BROKE = 6002
EARNER = 6003


async def _setup_round(w):
    """Found a company owned by FOUNDER and open a 10M / 10% round on it."""
    state = w.state
    await w.make_rich(FOUNDER, 1_000_000)
    await companies.found_company(w.session, state, FOUNDER, "RND", "Round Co", "tech")
    await w.session.flush()
    await investment.open_round(w.session, state, FOUNDER, "RND", 10_000_000, 10.0)
    await w.session.flush()


async def test_broke_user_is_rejected(world):
    w = world
    state = w.state
    await _setup_round(w)

    # Even with cash on hand, zero trailing income fails the gate.
    await w.make_rich(BROKE, 50_000_000)
    with pytest.raises(NotAllowed):
        await investment.invest(w.session, state, BROKE, "RND", 1_000_000)


async def test_earner_clears_gate_and_invests(world):
    w = world
    state = w.state
    await _setup_round(w)

    # EARNER grinds a state job (Fry Cook, 3,000/day) for enough business days to
    # clear the annualized threshold, then invests.
    cid, sjob = await first_state_job(w, "Fry Cook")
    await employment.apply_to_job(w.session, state, EARNER, sjob)
    await w.session.flush()

    # Work a full income window of business days.
    business_ticks = 0
    while business_ticks < INCOME_WINDOW_DAYS:
        if not gameday.is_business_day(state.game_day):
            await w.tick()
            continue
        await employment.clock_in(w.session, state, EARNER)
        await w.session.flush()
        await w.tick()
        business_ticks += 1

    # The thirtieth close leaves the cursor on Saturday.  Catch the calendar up
    # to Monday before attempting a market action; weekend closes add no pay.
    while not gameday.is_business_day(state.game_day):
        await w.tick()

    trailing = await trailing_income(
        w.session, state.guild_id, EARNER, state.game_day, INCOME_WINDOW_DAYS
    )
    annual = trailing * (BUSINESS_DAYS_PER_YEAR / INCOME_WINDOW_DAYS)
    assert annual >= ACCREDITED_THRESHOLD, f"annualized {annual} should clear the gate"

    # Now they can invest. Fund the wallet for the purchase (capital, not income).
    await w.make_rich(EARNER, 5_000_000)
    res = await investment.invest(w.session, state, EARNER, "RND", 5_000_000)
    await w.session.flush()
    assert res.amount_invested == 5_000_000
    assert res.shares > 0
    assert res.pct_of_company > 0
