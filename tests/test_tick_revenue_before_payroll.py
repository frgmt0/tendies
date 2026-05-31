"""§18 BUG 1 — revenue must land BEFORE payroll.

A company whose treasury starts at 0 and whose only income is today's production
must still make full payroll on the tick: the worker is paid in full and the
company is not flagged insolvent.
"""

from __future__ import annotations

from tendies import gameday
from tendies.config import DEFAULT_PRODUCTIVITY
from tendies.services import companies, employment

OWNER = 2001
WORKER = 2002


async def test_revenue_lands_before_payroll(world):
    w = world
    state = w.state

    # Fund + found a company with an EMPTY treasury.
    await w.make_rich(OWNER, 100_000)
    company = (await companies.found_company(w.session, state, OWNER, "REV", "Rev Co", "tech")).company
    await w.session.flush()
    assert company.treasury == 0

    # Wage strictly below one worker's productivity, so production covers it.
    wage = DEFAULT_PRODUCTIVITY - 4_000  # 8,000
    assert wage < DEFAULT_PRODUCTIVITY
    job = await companies.post_job(w.session, state, OWNER, "REV", "Worker", "", wage, 0, 0)
    await w.session.flush()

    apply_res = await employment.apply_to_job(w.session, state, WORKER, job.id)
    apps = await companies.list_applicants(w.session, state, OWNER, "REV")
    await companies.hire(w.session, state, OWNER, "REV", apps[0].application_id)
    await w.session.flush()

    # Clock in on the current business day (Monday), then tick -> Tuesday produces.
    assert gameday.is_business_day(state.game_day)
    await employment.clock_in(w.session, state, WORKER)
    await w.session.flush()

    report = await w.tick()
    assert report.is_business_day

    me = next(c for c in report.companies if c.ticker == "REV")
    # Revenue landed first (== productivity, sentiment 1.0), then full payroll paid.
    assert me.realized_revenue == DEFAULT_PRODUCTIVITY
    assert me.payroll_due == wage
    assert me.payroll_paid == wage
    assert me.insolvent is False
    assert me.bankrupted is False
    assert company.insolvent_days == 0

    # Worker received gross wage; treasury keeps the profit (revenue - wage).
    assert await w.wallet(WORKER) == wage - int(wage * state.tax_rate)
    assert company.treasury == DEFAULT_PRODUCTIVITY - wage
