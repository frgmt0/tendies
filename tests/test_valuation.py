"""Valuation and net worth (§13).

    nominal_value = treasury + avg_daily_revenue * 250 * revenue_multiple * sentiment

An event multiplier hits TWO things at once: that day's realized production
revenue AND the company's valuation / share price for the day.
"""

from __future__ import annotations

import pytest

from tendies import events, gameday, valuation
from tendies.config import (
    BUSINESS_DAYS_PER_YEAR,
    DEFAULT_PRODUCTIVITY,
    REVENUE_MULTIPLE,
)
from tendies.services import companies, employment

OWNER = 11001
WORKER = 11002


async def _company_with_revenue(w, ticks: int = 3):
    """Found a company, give it a worker, and run ``ticks`` producing days to
    accumulate realized-revenue rows. Returns the Company (treasury funded)."""
    state = w.state
    await w.make_rich(OWNER, 1_000_000)
    company = (await companies.found_company(w.session, state, OWNER, "VAL", "Val Co", "medicine")).company
    await w.session.flush()
    job = await companies.post_job(w.session, state, OWNER, "VAL", "W", "", 1_000, 0, 0)
    await w.session.flush()
    await employment.apply_to_job(w.session, state, WORKER, job.id)
    apps = await companies.list_applicants(w.session, state, OWNER, "VAL")
    await companies.hire(w.session, state, OWNER, "VAL", apps[0].application_id)
    await w.session.flush()
    # Keep the pool huge so no recession scaling; full production each day.
    produced = 0
    while produced < ticks:
        if not gameday.is_business_day(state.game_day):
            await w.tick()
            continue
        await employment.clock_in(w.session, state, WORKER)
        await w.session.flush()
        await w.tick()
        produced += 1
    return company


async def test_nominal_value_formula(world):
    w = world
    state = w.state
    # Park on a business day with no events so sentiment == 1.0.
    company = await _company_with_revenue(w, ticks=3)
    await w.clear_events()
    assert gameday.is_business_day(state.game_day)

    val = await valuation.company_valuation(w.session, state, company)
    assert val.frozen is False
    assert val.sentiment == 1.0

    expected_nominal = (
        company.treasury + val.avg_daily_revenue * BUSINESS_DAYS_PER_YEAR * REVENUE_MULTIPLE * 1.0
    )
    assert val.annual_revenue == pytest.approx(val.avg_daily_revenue * BUSINESS_DAYS_PER_YEAR)
    assert val.nominal_value == pytest.approx(expected_nominal)
    assert val.real_value == pytest.approx(val.nominal_value / state.inflation_index)
    assert val.share_price == pytest.approx(val.real_value / company.total_shares)

    # With clocked-in workers producing DEFAULT_PRODUCTIVITY each full day, the
    # average daily revenue should equal that productivity.
    assert val.avg_daily_revenue == pytest.approx(DEFAULT_PRODUCTIVITY)


async def test_event_multiplier_raises_valuation_and_share_price(world):
    w = world
    state = w.state
    company = await _company_with_revenue(w, ticks=3)
    await w.clear_events()
    assert gameday.is_business_day(state.game_day)

    base = await valuation.company_valuation(w.session, state, company)

    # Fire a medicine event for today (the company's industry) at x2.5.
    await events.create_admin_event(w.session, state, "medicine", 2.5, "breakthrough")
    await w.session.flush()

    boosted = await valuation.company_valuation(w.session, state, company)
    assert boosted.sentiment == pytest.approx(2.5)

    # Only the sentiment-scaled term moves; treasury term is unchanged.
    rev_term = base.avg_daily_revenue * BUSINESS_DAYS_PER_YEAR * REVENUE_MULTIPLE
    assert boosted.nominal_value == pytest.approx(company.treasury + rev_term * 2.5)
    assert boosted.nominal_value > base.nominal_value
    assert boosted.share_price > base.share_price
    # "Δ today" reflects the move vs. the previous (eventless) business day.
    assert boosted.delta_today > 0


async def test_event_multiplier_raises_realized_revenue(world):
    w = world
    state = w.state
    company = await _company_with_revenue(w, ticks=2)
    await w.clear_events()

    # We're on a business day; the worker is clocked OUT (auto). Clock in, then
    # schedule an event for the NEXT business day (the day the tick will produce on).
    next_business = gameday.next_day(state.game_day)
    while not gameday.is_business_day(next_business):
        next_business = gameday.next_day(next_business)

    # Place a medicine x2.0 event on the day production will land.
    from tendies.models import Event
    w.session.add(
        Event(
            guild_id=state.guild_id,
            game_day=next_business,
            industry="medicine",
            multiplier=2.0,
            source="admin",
            blurb="boom",
        )
    )
    await w.session.flush()

    # Advance to that business day with the worker clocked in.
    await employment.clock_in(w.session, state, WORKER)
    await w.session.flush()
    report = await w.tick()
    # Walk forward to the event day if a weekend intervened.
    while report.game_day != next_business:
        if gameday.is_business_day(state.game_day):
            await employment.clock_in(w.session, state, WORKER)
            await w.session.flush()
        report = await w.tick()

    me = next(c for c in report.companies if c.ticker == "VAL")
    assert me.sentiment == pytest.approx(2.0)
    # Production scaled by the event multiplier (x2.0 of one worker's productivity).
    assert me.tentative_revenue == int(DEFAULT_PRODUCTIVITY * 2.0)
    assert me.realized_revenue == int(DEFAULT_PRODUCTIVITY * 2.0)
