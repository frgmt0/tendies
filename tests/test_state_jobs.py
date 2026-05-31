"""State-owned company jobs (§7).

State jobs auto-accept; clocking in then ticking pays the wage straight from the
POOL (no treasury), with tax withheld to the pool, and auto clock-out after the
tick. A Fry Cook nets ~2,550 from a 3,000 gross wage at 15% tax.
"""

from __future__ import annotations

from sqlalchemy import select

from tendies import gameday
from tendies.config import STARTING_POOL
from tendies.models import Company, Employment, Transaction
from tendies.services import employment

from conftest import first_state_job

WORKER = 12001


async def test_state_job_auto_accepts(world):
    w = world
    state = w.state
    cid, sjob = await first_state_job(w, "Fry Cook")

    res = await employment.apply_to_job(w.session, state, WORKER, sjob)
    await w.session.flush()
    assert res.auto_accepted is True
    assert res.daily_wage == 3_000

    emp = (
        await w.session.execute(
            select(Employment).where(
                Employment.company_id == cid, Employment.user_id == WORKER
            )
        )
    ).scalars().first()
    assert emp is not None
    assert emp.productivity == 0  # state companies don't produce revenue


async def test_fry_cook_paid_from_pool_taxed_and_clocked_out(world):
    w = world
    state = w.state
    cid, sjob = await first_state_job(w, "Fry Cook")
    await employment.apply_to_job(w.session, state, WORKER, sjob)
    await w.session.flush()
    await w.assert_supply(STARTING_POOL)

    assert gameday.is_business_day(state.game_day)
    await employment.clock_in(w.session, state, WORKER)
    await w.session.flush()

    pool_before = state.pool_balance
    report = await w.tick()
    assert report.is_business_day

    # Wage came from the pool (state payroll), tax returned to the pool.
    gross = 3_000
    tax = int(gross * state.tax_rate)  # 450
    net = gross - tax  # 2,550
    assert net == 2_550

    assert report.state_payroll_paid == gross
    assert await w.wallet(WORKER) == net

    # Net pool change: -gross (paid out) + tax (withheld) = -(gross - tax) = -net.
    assert state.pool_balance == pool_before - net

    # The state company's treasury never moved (faucet, not a profit center).
    company = await w.session.get(Company, cid)
    assert company.treasury == 0

    # Auto clock-out after the tick.
    emp = (
        await w.session.execute(
            select(Employment).where(
                Employment.company_id == cid, Employment.user_id == WORKER
            )
        )
    ).scalars().first()
    assert emp.clocked_in is False

    # The wage shows up in the ledger as a state_wage from the pool.
    txs = (
        await w.session.execute(
            select(Transaction).where(
                Transaction.guild_id == state.guild_id,
                Transaction.type == "state_wage",
                Transaction.user_id == WORKER,
            )
        )
    ).scalars().all()
    assert len(txs) == 1
    assert txs[0].amount == gross
    assert txs[0].src == "pool"

    # Conservation holds across the whole pay cycle.
    await w.assert_supply(STARTING_POOL)
