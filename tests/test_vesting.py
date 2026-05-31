"""Equity vesting (§10).

A grant vests deterministically to exactly ``total_shares`` by ``vest_days``
business-day ticks, drift-free (``total * days_elapsed // vest_days``). Quitting
early keeps whatever vested (already in holdings) and forfeits the rest.
"""

from __future__ import annotations

from sqlalchemy import select

from tendies.models import EquityGrant
from tendies.services import companies, employment

OWNER = 7001
WORKER = 7002

TOTAL = 100_000
VEST_DAYS = 10


async def _hire_with_grant(w, worker):
    state = w.state
    await w.make_rich(OWNER, 1_000_000)
    company = (await companies.found_company(w.session, state, OWNER, "VST", "Vest Co", "tech")).company
    await w.session.flush()
    job = await companies.post_job(w.session, state, OWNER, "VST", "Grantee", "", 1_000, TOTAL, VEST_DAYS)
    await w.session.flush()
    await employment.apply_to_job(w.session, state, worker, job.id)
    apps = await companies.list_applicants(w.session, state, OWNER, "VST")
    await companies.hire(w.session, state, OWNER, "VST", apps[0].application_id)
    await w.session.flush()
    return company


async def test_grant_vests_to_exactly_total(world):
    w = world
    state = w.state
    company = await _hire_with_grant(w, WORKER)

    # Run exactly VEST_DAYS *business-day* ticks (each one vests once); track the
    # deterministic schedule. Vesting only happens on business-day ticks, so we
    # count by the tick report's business-day flag, not the pre-tick weekday.
    vested_seen = []
    for _ in range(VEST_DAYS):
        await w.tick_business_days(1)
        vested_seen.append(await w.holding(company.id, WORKER))

    # Deterministic schedule: after d vesting days, vested == TOTAL * d // VEST_DAYS.
    for d, held in enumerate(vested_seen, start=1):
        if d < VEST_DAYS:
            assert held == TOTAL * d // VEST_DAYS, f"day {d}: {held}"
    # Fully vested by the deadline, exactly the grant total.
    assert vested_seen[-1] == TOTAL
    assert await w.holding(company.id, WORKER) == TOTAL

    # Grant row is consumed once fully vested.
    grant = (
        await w.session.execute(
            select(EquityGrant).where(EquityGrant.total_shares == TOTAL)
        )
    ).scalars().first()
    assert grant is None


async def test_quitting_early_keeps_vested_forfeits_rest(world):
    w = world
    state = w.state
    company = await _hire_with_grant(w, WORKER)

    # Vest for 5 business days (half), then quit.
    HALF = 5
    await w.tick_business_days(HALF)

    vested_now = await w.holding(company.id, WORKER)
    assert vested_now == TOTAL * HALF // VEST_DAYS  # 50,000

    res = await employment.quit_job(w.session, state, WORKER, "VST")
    await w.session.flush()

    assert res.vested == vested_now
    assert res.forfeited == TOTAL - vested_now
    # Vested shares remain in holdings after quitting.
    assert await w.holding(company.id, WORKER) == vested_now
    # Grant is gone (forfeited).
    remaining = (
        await w.session.execute(
            select(EquityGrant).where(EquityGrant.total_shares == TOTAL)
        )
    ).scalars().first()
    assert remaining is None
