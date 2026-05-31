"""§18 BUG 2 — aggregate recession scaling.

The realizable cap applies to *total* tentative revenue across all companies; a
single ratio scales everyone uniformly. Two companies with identical production,
a pool too small for both at full output, must realize the SAME fraction — not
first-come-first-served.
"""

from __future__ import annotations

import math

from tendies.config import DEFAULT_PRODUCTIVITY, RECESSION_CAP_FRACTION
from tendies.services import companies, employment

OWNER_A = 3001
OWNER_B = 3002
WORKER_A = 3003
WORKER_B = 3004


async def test_recession_scales_everyone_uniformly(world):
    w = world
    state = w.state

    await w.make_rich(OWNER_A, 100_000)
    await w.make_rich(OWNER_B, 100_000)
    a = (await companies.found_company(w.session, state, OWNER_A, "AAA", "A Co", "tech")).company
    await w.session.flush()
    b = (await companies.found_company(w.session, state, OWNER_B, "BBB", "B Co", "food")).company
    await w.session.flush()

    # Each company: one worker producing DEFAULT_PRODUCTIVITY, low wage they can't
    # cover from an empty treasury (irrelevant to the scaling assertion).
    for ticker, owner, worker in (("AAA", OWNER_A, WORKER_A), ("BBB", OWNER_B, WORKER_B)):
        job = await companies.post_job(w.session, state, owner, ticker, "W", "", 1_000, 0, 0)
        await w.session.flush()
        await employment.apply_to_job(w.session, state, worker, job.id)
        apps = await companies.list_applicants(w.session, state, owner, ticker)
        await companies.hire(w.session, state, owner, ticker, apps[0].application_id)
        await w.session.flush()
        await employment.clock_in(w.session, state, worker)
        await w.session.flush()

    # Shrink the pool so the aggregate cap is half the total tentative revenue.
    total_tentative = 2 * DEFAULT_PRODUCTIVITY
    # want cap == total_tentative / 2  =>  floor(0.10 * pool) == total/2
    target_cap = total_tentative // 2
    state.pool_balance = int(math.ceil(target_cap / RECESSION_CAP_FRACTION))
    await w.session.flush()

    cap = math.floor(RECESSION_CAP_FRACTION * state.pool_balance)
    expected_ratio = cap / total_tentative
    assert expected_ratio < 1.0  # genuinely capped

    report = await w.tick()
    assert report.is_business_day

    ra = next(c for c in report.companies if c.ticker == "AAA")
    rb = next(c for c in report.companies if c.ticker == "BBB")

    # Same ratio, same realized amount (identical production) — within 1 nug rounding.
    assert report.recession_ratio == expected_ratio
    assert abs(ra.realized_revenue - rb.realized_revenue) <= 1
    expected_each = math.floor(DEFAULT_PRODUCTIVITY * expected_ratio)
    assert ra.realized_revenue == expected_each
    assert rb.realized_revenue == expected_each

    # Aggregate realized revenue never exceeds the cap.
    assert ra.realized_revenue + rb.realized_revenue <= cap
