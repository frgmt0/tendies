"""Clock-in streaks + milestone bonuses, $promote, and reminder selection.

Streaks count consecutive *business* days clocked in (a weekend in between
doesn't break them); milestone bonuses are paid once-ever from the pool (taxed,
conserving). $promote raises an employee's wage. due_for_reminder picks the
opted-in employees who haven't clocked in today.
"""

from __future__ import annotations

import pytest

from tendies import config, money
from tendies.config import STARTING_POOL
from tendies.errors import BadInput, NotAllowed, NotFound
from tendies.services import companies, employment

from conftest import first_state_job

WORKER = 7001
OWNER = 7002
OTHER = 7003


async def _employ_state(w, uid: int) -> None:
    """Put ``uid`` on a state job (auto-accepted)."""
    _, job_id = await first_state_job(w)
    await employment.apply_to_job(w.session, w.state, uid, job_id)
    await w.session.flush()


async def _clock_through_business_days(w, uid: int, days: int) -> int:
    """Clock in for ``days`` consecutive business days (ticking between them).
    Returns the final streak."""
    streak = 0
    for _ in range(days):
        # Advance to the next business day if we're not on one.
        while not w.state.weekday in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            await w.tick()
        result = await employment.clock_in(w.session, w.state, uid)
        streak = result.streak
        await w.tick()  # close the day (auto clock-out + advance)
    return streak


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

async def test_streak_increments_across_a_weekend(world):
    w = world
    await _employ_state(w, WORKER)
    # Mon..Fri then Mon = 6 business days clocked in; the weekend doesn't break it.
    final = await _clock_through_business_days(w, WORKER, 6)
    assert final == 6


async def test_streak_resets_after_a_missed_business_day(world):
    w = world
    await _employ_state(w, WORKER)

    r1 = await employment.clock_in(w.session, w.state, WORKER)
    assert r1.streak == 1
    await w.tick()  # -> Tuesday
    # Skip Tuesday entirely (no clock-in).
    await w.tick()  # -> Wednesday
    r2 = await employment.clock_in(w.session, w.state, WORKER)
    assert r2.streak == 1  # missed Tuesday -> reset


async def test_clocking_in_twice_keeps_streak(world):
    w = world
    await _employ_state(w, WORKER)
    r1 = await employment.clock_in(w.session, w.state, WORKER)
    r2 = await employment.clock_in(w.session, w.state, WORKER)  # same day again
    assert r1.streak == 1 and r2.already and r2.streak == 1


# ---------------------------------------------------------------------------
# Milestone bonuses
# ---------------------------------------------------------------------------

async def test_milestone_bonus_paid_from_pool_once(world, monkeypatch):
    w = world
    # Small milestone so the test is quick; bonus comes from the pool.
    monkeypatch.setattr(config, "STREAK_MILESTONES", ((2, 50_000),))
    await _employ_state(w, WORKER)
    await w.assert_supply(STARTING_POOL)

    # Day 1: streak 1, no bonus yet.
    r1 = await employment.clock_in(w.session, w.state, WORKER)
    assert r1.bonus_milestone == 0
    await w.tick()

    pool_before = w.state.pool_balance
    wallet_before = await w.wallet(WORKER)
    # Day 2: streak 2 -> milestone bonus.
    r2 = await employment.clock_in(w.session, w.state, WORKER)
    assert r2.streak == 2
    assert r2.bonus_milestone == 2
    assert r2.bonus_gross == 50_000
    tax = money.tax_amount(50_000, w.state.tax_rate)
    assert r2.bonus_tax == tax
    # Wallet rose by net, pool fell by net (gross out, tax back) — conserving.
    assert await w.wallet(WORKER) - wallet_before == 50_000 - tax
    assert w.state.pool_balance == pool_before - 50_000 + tax
    await w.assert_supply(STARTING_POOL)
    await w.tick()

    # Day 3: streak 3, but the 2-day milestone is already claimed — no repeat.
    r3 = await employment.clock_in(w.session, w.state, WORKER)
    assert r3.streak == 3
    assert r3.bonus_milestone == 0
    await w.assert_supply(STARTING_POOL)


async def test_milestone_not_reawarded_after_streak_resets(world, monkeypatch):
    w = world
    monkeypatch.setattr(config, "STREAK_MILESTONES", ((2, 50_000),))
    await _employ_state(w, WORKER)

    await employment.clock_in(w.session, w.state, WORKER)  # day1 streak1
    await w.tick()
    r2 = await employment.clock_in(w.session, w.state, WORKER)  # day2 streak2 -> bonus
    assert r2.bonus_milestone == 2
    await w.tick()  # -> day3
    await w.tick()  # skip day3 -> day4 (missed a business day)
    r = await employment.clock_in(w.session, w.state, WORKER)
    assert r.streak == 1  # reset
    await w.tick()
    r = await employment.clock_in(w.session, w.state, WORKER)
    assert r.streak == 2
    assert r.bonus_milestone == 0  # one-time ever, not re-awarded
    await w.assert_supply(STARTING_POOL)


# ---------------------------------------------------------------------------
# $promote
# ---------------------------------------------------------------------------

async def _found_with_employee(w):
    await w.make_rich(OWNER, 2_000_000)
    co = (await companies.found_company(w.session, w.state, OWNER, "RAIS", "Raise Co", "tech")).company
    await w.session.flush()
    job = await companies.post_job(w.session, w.state, OWNER, "RAIS", "Worker", "", 1_000, 0, 0)
    await w.session.flush()
    await employment.apply_to_job(w.session, w.state, WORKER, job.id)
    apps = await companies.list_applicants(w.session, w.state, OWNER, "RAIS")
    await companies.hire(w.session, w.state, OWNER, "RAIS", apps[0].application_id)
    await w.session.flush()
    return co


async def test_promote_raises_wage(world):
    w = world
    await _found_with_employee(w)
    res = await companies.promote(w.session, w.state, OWNER, WORKER, 25)
    assert res.old_wage == 1_000
    assert res.new_wage == 1_250
    emp = await employment.get_employment(w.session, w.guild_id, WORKER)
    assert emp.daily_wage == 1_250


async def test_promote_rejects_non_owner(world):
    w = world
    await _found_with_employee(w)
    with pytest.raises(NotAllowed):
        await companies.promote(w.session, w.state, OTHER, WORKER, 10)


async def test_promote_rejects_non_positive(world):
    w = world
    await _found_with_employee(w)
    with pytest.raises(BadInput):
        await companies.promote(w.session, w.state, OWNER, WORKER, 0)
    with pytest.raises(BadInput):
        await companies.promote(w.session, w.state, OWNER, WORKER, -5)


async def test_promote_unknown_employee(world):
    w = world
    await _found_with_employee(w)
    with pytest.raises(NotFound):
        await companies.promote(w.session, w.state, OWNER, 999999, 10)


# ---------------------------------------------------------------------------
# Reminder selection
# ---------------------------------------------------------------------------

async def test_due_for_reminder_selects_opted_in_not_clocked_in(world):
    w = world
    await _employ_state(w, WORKER)
    await _employ_state(w, OTHER)
    # WORKER opts in; OTHER does not.
    await employment.set_reminder_opt_in(w.session, w.state, WORKER, True)
    await employment.set_reminder_opt_in(w.session, w.state, OTHER, False)
    await w.session.flush()

    due = await employment.due_for_reminder(w.session, w.state)
    assert WORKER in due
    assert OTHER not in due

    # Once WORKER clocks in, they drop off the list.
    await employment.clock_in(w.session, w.state, WORKER)
    await w.session.flush()
    assert WORKER not in await employment.due_for_reminder(w.session, w.state)


async def test_mark_reminded_dedupes_same_day(world):
    w = world
    await _employ_state(w, WORKER)
    await employment.set_reminder_opt_in(w.session, w.state, WORKER, True)
    await w.session.flush()
    assert WORKER in await employment.due_for_reminder(w.session, w.state)

    await employment.mark_reminded(w.session, w.state, [WORKER])
    await w.session.flush()
    assert WORKER not in await employment.due_for_reminder(w.session, w.state)


async def test_no_reminders_on_weekend(db):
    from conftest import bootstrap_world

    w = await bootstrap_world(db, weekday="saturday")
    await _employ_state(w, WORKER)
    await employment.set_reminder_opt_in(w.session, w.state, WORKER, True)
    await w.session.flush()
    assert await employment.due_for_reminder(w.session, w.state) == []
    await w.session.commit()
    await w.session.close()
