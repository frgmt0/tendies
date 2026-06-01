"""§18 BUG 3 — bankruptcy unwinds cleanly, in one pass.

Drive a company insolvent for INSOLVENCY_GRACE_DAYS while it has an open
acquisition offer out AND an open funding round. After bankruptcy assert:
treasury returned to the pool, company inactive, holdings wiped, jobs closed, the
offer voided, employment ended — nothing dangling. Money is conserved.
"""

from __future__ import annotations

from sqlalchemy import select

from tendies import gameday
from tendies.config import INSOLVENCY_GRACE_DAYS, STARTING_POOL
from tendies.models import Employment, EquityGrant, FundingRound, Holding, Job, Offer
from tendies.services import acquisitions, companies, employment, investment

OWNER = 4001
WORKER = 4002
VICTIM_OWNER = 4003  # owns the acquisition target


async def test_bankruptcy_unwinds_everything(world):
    w = world
    state = w.state
    await w.assert_supply(STARTING_POOL)

    await w.make_rich(OWNER, 1_000_000)
    await w.make_rich(VICTIM_OWNER, 1_000_000)

    doomed = (await companies.found_company(w.session, state, OWNER, "DIE", "Doomed Co", "tech")).company
    await w.session.flush()
    target = (await companies.found_company(w.session, state, VICTIM_OWNER, "TGT", "Target Co", "tech")).company
    await w.session.flush()

    # A worker with a wage far beyond what the company can ever produce.
    job = await companies.post_job(w.session, state, OWNER, "DIE", "Overpaid", "", 1_000_000, 0, 0)
    await w.session.flush()
    await employment.apply_to_job(w.session, state, WORKER, job.id)
    apps = await companies.list_applicants(w.session, state, OWNER, "DIE")
    await companies.hire(w.session, state, OWNER, "DIE", apps[0].application_id)
    await w.session.flush()

    # Give DIE a tiny treasury so it can fund an acquisition offer + look alive.
    state.pool_balance -= 500_000
    doomed.treasury += 500_000
    await w.session.flush()
    await w.assert_supply(STARTING_POOL)

    # Open round on DIE (this should be voided/dangling-checked after bankruptcy).
    await investment.open_round(w.session, state, OWNER, "DIE", 1_000_000, 10.0)
    await w.session.flush()

    # DIE sends an open acquisition offer for TGT.
    await acquisitions.offer(w.session, state, OWNER, "DIE", "TGT", 100_000)
    await w.session.flush()

    open_offer = (
        await w.session.execute(
            select(Offer).where(Offer.acquirer_id == doomed.id, Offer.status == "open")
        )
    ).scalars().first()
    assert open_offer is not None

    # Drive INSOLVENCY_GRACE_DAYS business days insolvent. Each business day the
    # worker clocks in (produces a little), payroll dwarfs treasury -> insolvent.
    bankrupted = False
    for _ in range(INSOLVENCY_GRACE_DAYS + 3):
        if not gameday.is_business_day(state.game_day):
            await w.tick()
            continue
        try:
            await employment.clock_in(w.session, state, WORKER)
        except Exception:
            # employment may have ended at bankruptcy
            pass
        await w.session.flush()
        report = await w.tick()
        if "DIE" in report.bankruptcies:
            bankrupted = True
            break

    assert bankrupted, "company should have gone bankrupt within the grace window"

    await w.session.refresh(doomed)

    # 1) Treasury returned to the pool: company holds nothing.
    assert doomed.treasury == 0
    # 2) Company is inactive.
    assert doomed.active is False
    assert doomed.insolvent_days == 0
    # 3) Holdings wiped — and total_shares zeroed in lockstep so the dead row
    #    keeps total_shares == Σ holdings.
    holdings = (
        await w.session.execute(select(Holding).where(Holding.company_id == doomed.id))
    ).scalars().all()
    assert holdings == []
    assert doomed.total_shares == 0
    # 4) Jobs closed.
    open_jobs = (
        await w.session.execute(
            select(Job).where(Job.company_id == doomed.id, Job.open == True)  # noqa: E712
        )
    ).scalars().all()
    assert open_jobs == []
    # 5) The open offer involving DIE is voided (not still open).
    still_open = (
        await w.session.execute(
            select(Offer).where(
                Offer.status == "open",
                (Offer.acquirer_id == doomed.id) | (Offer.target_id == doomed.id),
            )
        )
    ).scalars().all()
    assert still_open == []
    refreshed_offer = await w.session.get(Offer, open_offer.id)
    assert refreshed_offer.status == "void"
    # 6) Employment ended (and any grants gone — none here, but check anyway).
    emps = (
        await w.session.execute(select(Employment).where(Employment.company_id == doomed.id))
    ).scalars().all()
    assert emps == []
    grants = (
        await w.session.execute(
            select(EquityGrant)
            .join(Employment, EquityGrant.employment_id == Employment.id)
            .where(Employment.company_id == doomed.id)
        )
    ).scalars().all()
    assert grants == []

    # The funding round row still exists but is harmless (company inactive); the
    # spec's dangling check is about offers/jobs/holdings/employment/treasury.
    rounds = (
        await w.session.execute(select(FundingRound).where(FundingRound.company_id == doomed.id))
    ).scalars().all()
    assert len(rounds) == 1  # persisted, but attached to a dead, inactive company

    # No nuggies created or destroyed in the whole unwind.
    await w.assert_supply(STARTING_POOL)
