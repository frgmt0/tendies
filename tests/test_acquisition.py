"""Acquisitions (§14).

On accept: the offer is paid pro-rata to the target's whole cap table (taxed),
the target's treasury is absorbed into the acquirer, the target's open jobs
transfer to the acquirer, wage-only employees are laid off, and the target
deactivates. Money is conserved. Accept fails if the acquirer's treasury can no
longer cover the offer.
"""

from __future__ import annotations

import pytest

from sqlalchemy import func, select

from tendies.config import STARTING_POOL
from tendies.errors import InsufficientFunds
from tendies.models import Employment, Holding, Job
from tendies.services import acquisitions, companies, employment

ACQ_OWNER = 9001
TGT_OWNER = 9002
TGT_INVESTOR = 9003
TGT_WORKER = 9004


async def _build(w):
    state = w.state
    await w.make_rich(ACQ_OWNER, 2_000_000)
    await w.make_rich(TGT_OWNER, 2_000_000)

    acquirer = (await companies.found_company(w.session, state, ACQ_OWNER, "ACQ", "Acquirer", "tech")).company
    await w.session.flush()
    target = (await companies.found_company(w.session, state, TGT_OWNER, "TGT", "Target", "food")).company
    await w.session.flush()

    # Target cap table: 900,000 owner / 100,000 investor.
    owner_h = await w.session.get(Holding, (target.id, TGT_OWNER))
    owner_h.shares = 900_000
    w.session.add(Holding(company_id=target.id, user_id=TGT_INVESTOR, shares=100_000))
    await w.session.flush()

    # A wage-only employee at the target.
    job = await companies.post_job(w.session, state, TGT_OWNER, "TGT", "Worker", "", 5_000, 0, 0)
    await w.session.flush()
    await employment.apply_to_job(w.session, state, TGT_WORKER, job.id)
    apps = await companies.list_applicants(w.session, state, TGT_OWNER, "TGT")
    await companies.hire(w.session, state, TGT_OWNER, "TGT", apps[0].application_id)
    await w.session.flush()

    # Give the target a treasury (absorbed at close) and the acquirer plenty.
    state.pool_balance -= 300_000
    target.treasury += 300_000
    state.pool_balance -= 50_000_000
    acquirer.treasury += 50_000_000
    await w.session.flush()
    return acquirer, target


async def test_accept_runs_full_close(world):
    w = world
    state = w.state
    acquirer, target = await _build(w)
    await w.assert_supply(STARTING_POOL)

    acq_treasury_before = acquirer.treasury
    target_treasury = target.treasury  # 300,000
    owner_wallet_before = await w.wallet(TGT_OWNER)
    inv_wallet_before = await w.wallet(TGT_INVESTOR)
    amount = 10_000_000

    await acquisitions.offer(w.session, state, ACQ_OWNER, "ACQ", "TGT", amount)
    await w.session.flush()
    res = await acquisitions.accept(w.session, state, TGT_OWNER, "ACQ")
    await w.session.flush()

    # Pro-rata payout to the target's cap table (90% / 10%).
    by_user = {uid: (gross, tax) for uid, gross, tax in res.payouts}
    assert by_user[TGT_OWNER][0] == 9_000_000
    assert by_user[TGT_INVESTOR][0] == 1_000_000
    assert sum(g for g, _ in by_user.values()) == amount

    # Tax withheld at 15% on each slice.
    assert by_user[TGT_OWNER][1] == 1_350_000
    assert by_user[TGT_INVESTOR][1] == 150_000
    total_tax = sum(t for _, t in by_user.values())

    # Wallets rise by net (gross - tax) on top of whatever they held before.
    assert await w.wallet(TGT_OWNER) - owner_wallet_before == 9_000_000 - 1_350_000
    assert await w.wallet(TGT_INVESTOR) - inv_wallet_before == 1_000_000 - 150_000

    # Treasury absorbed.
    assert res.treasury_absorbed == target_treasury
    # Acquirer treasury: -offer (paid out) + absorbed target treasury.
    assert acquirer.treasury == acq_treasury_before - amount + target_treasury
    assert target.treasury == 0

    # Open jobs transferred to the acquirer.
    assert res.jobs_transferred == 1
    transferred = (
        await w.session.execute(
            select(Job).where(Job.company_id == acquirer.id, Job.open == True)  # noqa: E712
        )
    ).scalars().all()
    assert len(transferred) == 1

    # Wage-only employee laid off; target has no employment.
    assert res.employees_laid_off == 1
    emps = (
        await w.session.execute(select(Employment).where(Employment.company_id == target.id))
    ).scalars().all()
    assert emps == []

    # Target deactivated; offer marked accepted.
    await w.session.refresh(target)
    assert target.active is False

    # Conservation: payout tax to pool, treasury moved around, nothing minted.
    await w.assert_supply(STARTING_POOL)
    assert total_tax == 1_500_000


async def test_accept_dissolves_target_shares_and_holds_invariant(world):
    """After a close, the bought-out cap table ceases to exist: the target has
    no holdings and total_shares == 0, and the global share invariant
    (Σ all holdings == Σ active companies' total_shares) still holds."""
    w = world
    state = w.state
    acquirer, target = await _build(w)

    await acquisitions.offer(w.session, state, ACQ_OWNER, "ACQ", "TGT", 10_000_000)
    await w.session.flush()
    await acquisitions.accept(w.session, state, TGT_OWNER, "ACQ")
    await w.session.flush()
    await w.session.refresh(target)

    # The dissolved target: no holdings, zero shares (the fix).
    target_holdings = (
        await w.session.execute(
            select(func.coalesce(func.sum(Holding.shares), 0)).where(
                Holding.company_id == target.id
            )
        )
    ).scalar_one()
    assert target_holdings == 0
    assert target.total_shares == 0

    # Global invariant across *active* companies.
    from tendies.models import Company

    active_total_shares = (
        await w.session.execute(
            select(func.coalesce(func.sum(Company.total_shares), 0)).where(
                Company.guild_id == w.guild_id,
                Company.active == True,  # noqa: E712
            )
        )
    ).scalar_one()
    all_holdings = (
        await w.session.execute(
            select(func.coalesce(func.sum(Holding.shares), 0))
            .join(Company, Holding.company_id == Company.id)
            .where(Company.active == True)  # noqa: E712
        )
    ).scalar_one()
    assert active_total_shares == all_holdings
    await w.assert_supply(STARTING_POOL)


async def test_accept_fails_if_treasury_insufficient(world):
    w = world
    state = w.state
    acquirer, target = await _build(w)

    amount = 10_000_000
    await acquisitions.offer(w.session, state, ACQ_OWNER, "ACQ", "TGT", amount)
    await w.session.flush()

    # Drain the acquirer's treasury below the offer after it was sent.
    state.pool_balance += acquirer.treasury
    acquirer.treasury = 0
    await w.session.flush()

    with pytest.raises(InsufficientFunds):
        await acquisitions.accept(w.session, state, TGT_OWNER, "ACQ")
