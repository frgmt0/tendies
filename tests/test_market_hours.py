"""Market hours (§4).

A weekend tick is a *closed tick*: the day advances, nothing produces, no wages
are paid, and share prices freeze (valuation.frozen, delta 0). Clocking in on a
weekend raises BadInput.
"""

from __future__ import annotations

import pytest

from tendies import gameday, valuation
from tendies.config import STARTING_POOL
from tendies.errors import BadInput
from tendies.services import companies, employment

OWNER = 13001
WORKER = 13002


async def _company_on_friday(w):
    """Found a company with a clocked-history worker, then anchor on Friday."""
    state = w.state
    await w.make_rich(OWNER, 1_000_000)
    company = (await companies.found_company(w.session, state, OWNER, "WKD", "Weekend Co", "tech")).company
    await w.session.flush()
    job = await companies.post_job(w.session, state, OWNER, "WKD", "W", "", 1_000, 0, 0)
    await w.session.flush()
    await employment.apply_to_job(w.session, state, WORKER, job.id)
    apps = await companies.list_applicants(w.session, state, OWNER, "WKD")
    await companies.hire(w.session, state, OWNER, "WKD", apps[0].application_id)
    await w.session.flush()

    # One producing day so the company has revenue history (Monday -> Tuesday).
    await employment.clock_in(w.session, state, WORKER)
    await w.session.flush()
    await w.tick()

    # Jump the calendar to Friday so the next tick is a weekend (Saturday).
    await w.set_weekday("friday")
    await w.clear_events()
    assert state.weekday == "friday"
    return company


async def test_weekend_tick_is_closed_and_freezes_prices(world):
    w = world
    state = w.state
    company = await _company_on_friday(w)

    # Valuation while the market is OPEN (Friday).
    open_val = await valuation.company_valuation(w.session, state, company)
    assert open_val.frozen is False

    # Worker clocks in on Friday; the tick lands on Saturday (closed).
    await employment.clock_in(w.session, state, WORKER)
    await w.session.flush()

    pool_before = state.pool_balance
    treasury_before = company.treasury
    worker_wallet_before = await w.wallet(WORKER)

    report = await w.tick()

    assert report.closed is True
    assert report.is_business_day is False
    assert gameday.is_weekend(state.game_day)
    # Nothing produced, nothing paid.
    assert report.companies == []
    assert report.total_realized_revenue == 0
    assert report.total_payroll == 0
    assert report.state_payroll_paid == 0
    assert state.pool_balance == pool_before
    assert company.treasury == treasury_before
    assert await w.wallet(WORKER) == worker_wallet_before

    # Still employed (employment_summary would raise NotFound otherwise), and
    # auto clock-out still happens on the closed tick.
    emp = await employment.employment_summary(w.session, state, WORKER, "WKD")
    assert emp.ticker == "WKD"
    from tendies.lookups import get_employment
    live = await get_employment(w.session, state.guild_id, WORKER)
    assert live is not None
    assert live.clocked_in is False

    # Prices are frozen on the weekend: sentiment 1.0, delta 0.
    weekend_val = await valuation.company_valuation(w.session, state, company)
    assert weekend_val.frozen is True
    assert weekend_val.sentiment == 1.0
    assert weekend_val.delta_today == 0.0

    # Conservation across the closed tick.
    await w.assert_supply(STARTING_POOL)


async def test_clock_in_on_weekend_raises(world):
    w = world
    state = w.state
    company = await _company_on_friday(w)

    # Advance Friday -> Saturday (closed tick), leaving game_day on the weekend.
    await w.tick()
    assert gameday.is_weekend(state.game_day)

    with pytest.raises(BadInput):
        await employment.clock_in(w.session, state, WORKER)
