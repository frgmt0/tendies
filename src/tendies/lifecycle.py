"""Shared company-lifecycle teardown helpers.

Both bankruptcy (the tick) and acquisition (a target dissolving into the
acquirer) tear a company down, and they must agree on the mechanics — what
happens to its employment, equity grants, open offers, jobs, and holdings. They
differ only in *which* steps apply:

* **Bankruptcy** (§9 step 5): treasury → pool, void offers, close jobs, wipe
  holdings, end employment, deactivate.
* **Acquisition** (§14): cap table already cashed out and treasury already
  transferred, so the target voids offers, ends employment (layoffs), forfeits
  unvested grants, transfers *open jobs* to the acquirer, then deactivates.

Centralizing the primitives keeps the two paths from drifting apart.
"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Company, EquityGrant, Employment, Holding, Job, Offer


async def end_all_employment(session: AsyncSession, company: Company) -> int:
    """End every employment at ``company``. Equity grants tied to those jobs are
    deleted (unvested shares forfeit; vested shares already live in holdings).
    Returns the number of employments ended."""
    emp_ids = (
        await session.execute(
            select(Employment.id).where(Employment.company_id == company.id)
        )
    ).scalars().all()
    if emp_ids:
        await session.execute(
            delete(EquityGrant).where(EquityGrant.employment_id.in_(emp_ids))
        )
        await session.execute(
            delete(Employment).where(Employment.id.in_(emp_ids))
        )
    return len(emp_ids)


async def void_offers_involving(session: AsyncSession, company: Company) -> None:
    """Void every open acquisition offer where ``company`` is acquirer or target."""
    await session.execute(
        update(Offer)
        .where(
            Offer.status == "open",
            (Offer.acquirer_id == company.id) | (Offer.target_id == company.id),
        )
        .values(status="void")
    )


async def close_jobs(session: AsyncSession, company: Company) -> None:
    """Close all of ``company``'s job postings."""
    await session.execute(
        update(Job).where(Job.company_id == company.id).values(open=False)
    )


async def wipe_holdings(session: AsyncSession, company: Company) -> None:
    """Destroy all equity in ``company`` (shares cease to exist)."""
    await session.execute(
        delete(Holding).where(Holding.company_id == company.id)
    )


async def transfer_open_jobs(
    session: AsyncSession, source: Company, dest: Company
) -> int:
    """Re-point ``source``'s open jobs to ``dest`` (acquisition: the acquirer
    absorbs the target's open positions / production capacity). Returns count."""
    job_ids = (
        await session.execute(
            select(Job.id).where(Job.company_id == source.id, Job.open == True)  # noqa: E712
        )
    ).scalars().all()
    if job_ids:
        await session.execute(
            update(Job).where(Job.id.in_(job_ids)).values(company_id=dest.id)
        )
    return len(job_ids)


async def cap_table(session: AsyncSession, company: Company) -> list[tuple[int, int]]:
    """The company's holders as ``[(user_id, shares), ...]`` (shares > 0),
    sorted by shares descending. The basis for every pro-rata payout."""
    rows = (
        await session.execute(
            select(Holding.user_id, Holding.shares)
            .where(Holding.company_id == company.id, Holding.shares > 0)
            .order_by(Holding.shares.desc())
        )
    ).all()
    return [(int(uid), int(sh)) for uid, sh in rows]
