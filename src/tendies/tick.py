"""The daily tick (§9). Order is load-bearing.

Runs once per game day for one guild, inside the caller's session transaction so
the whole tick is atomic. The sequence, exactly as specified:

1. Settle the current day (weekend → closed tick: only clock-out).
2. Events are read from the table on demand (no separate "apply" state).
3. Vest equity grants → holdings.
4. Produce, then realize revenue — the recession step. Tentative revenue is
   summed across *all* companies and, if it exceeds the aggregate cap (a
   fraction of the pool), *everyone* is scaled by the same ratio. pool → treasury.
5. Pay payroll (revenue already landed). Private: treasury → wallet, pro-rata
   when short, persistent insolvency → bankruptcy. State: pool → wallet.
6. Tax is withheld inside each payout (folded into step 5).
7. Auto clock-out everyone, record the close, then advance the calendar.

The three bugs §18 warns about live here, so the structure keeps them visible:
revenue (step 4) strictly precedes payroll (step 5); the recession ratio is
computed once on the *aggregate* and applied uniformly; bankruptcy unwinds in
one ordered pass.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import events as events_mod
from . import gameday
from . import lifecycle
from . import money
from . import valuation
from .config import INSOLVENCY_GRACE_DAYS, RECESSION_CAP_FRACTION
from .models import Company, Employment, EquityGrant, Holding, ServerState


@dataclass
class CompanyTickResult:
    company_id: int
    ticker: str
    name: str
    industry: str
    sentiment: float
    workers: int
    tentative_revenue: int
    realized_revenue: int
    payroll_due: int
    payroll_paid: int
    tax_withheld: int
    insolvent: bool
    bankrupted: bool


@dataclass
class TickReport:
    guild_id: int
    game_day: object  # datetime.date
    weekday: str
    is_business_day: bool
    closed: bool = False
    recession_ratio: float = 1.0
    companies: list[CompanyTickResult] = field(default_factory=list)
    bankruptcies: list[str] = field(default_factory=list)  # tickers
    total_realized_revenue: int = 0
    total_payroll: int = 0
    total_tax: int = 0
    vested_shares_total: int = 0
    state_payroll_paid: int = 0
    state_payroll_due: int = 0
    state_crisis: bool = False
    rolled_events: list[str] = field(default_factory=list)  # blurbs (Monday)
    todays_events: list[str] = field(default_factory=list)  # blurbs active today


def is_market_open(state: ServerState) -> bool:
    return gameday.is_business_day(state.game_day)


async def run_tick(
    session: AsyncSession,
    state: ServerState,
    *,
    rng: random.Random | None = None,
) -> TickReport:
    """Settle ``state.game_day``, then advance to the next open calendar date.

    Attendance belongs to the date players saw while clocking in.  Advancing
    first would therefore strand Friday attendance on a closed Saturday and
    apply admin events one day late.
    """
    current_day = state.game_day

    report = TickReport(
        guild_id=state.guild_id,
        game_day=current_day,
        weekday=gameday.weekday_name(current_day),
        is_business_day=gameday.is_business_day(current_day),
    )

    if gameday.is_monday(current_day):
        weekly = await events_mod.events_for_week(
            session, state.guild_id, current_day
        )
        report.rolled_events = [event.blurb for event in weekly]

    if not gameday.is_business_day(current_day):
        # Closed date: no production or pay.  Clearing stale attendance is safe
        # and catch-up then advances through each missed weekend date.
        await _clock_out_all(session, state.guild_id)
        report.closed = True
        await _advance_day(session, state, rng=rng)
        return report

    # ---- Step 2: today's sentiment (read from the events table) ----------
    multipliers = await events_mod.active_multipliers(
        session, state.guild_id, current_day
    )
    report.todays_events = [
        e.blurb for e in await events_mod._events_on(session, state.guild_id, current_day)
    ]

    # ---- Step 3: vest equity grants → holdings ---------------------------
    report.vested_shares_total = await _vest_grants(session, state)

    # ---- Step 4: produce, then realize revenue (the recession step) ------
    private = (
        await session.execute(
            select(Company).where(
                Company.guild_id == state.guild_id,
                Company.active == True,  # noqa: E712
                Company.is_state == False,  # noqa: E712
            )
        )
    ).scalars().all()

    # Gather clocked-in workers + tentative revenue per company.
    tentative: dict[int, int] = {}
    workers: dict[int, list[Employment]] = {}
    for company in private:
        emps = (
            await session.execute(
                select(Employment).where(
                    Employment.company_id == company.id,
                    Employment.clocked_in == True,  # noqa: E712
                )
            )
        ).scalars().all()
        workers[company.id] = emps
        raw = sum(e.productivity for e in emps)
        sentiment = multipliers.get(company.industry, 1.0)
        tentative[company.id] = int(raw * sentiment)

    total_tentative = sum(tentative.values())
    cap = int(math.floor(RECESSION_CAP_FRACTION * state.pool_balance))
    if total_tentative > cap and total_tentative > 0:
        ratio = cap / total_tentative
    else:
        ratio = 1.0
    report.recession_ratio = ratio

    # Realize revenue uniformly scaled, pool → treasury. Logged for every
    # active private company (even at 0) so the valuation average stays honest.
    company_by_id = {c.id: c for c in private}
    realized: dict[int, int] = {}
    for cid, tent in tentative.items():
        amount = int(math.floor(tent * ratio)) if ratio < 1.0 else tent
        realized[cid] = amount
        await money.realize_revenue(
            session, state, company_by_id[cid], amount, current_day
        )
        report.total_realized_revenue += amount

    # ---- Step 5: pay payroll (private) -----------------------------------
    for company in private:
        result = await _pay_private_payroll(
            session, state, company, workers[company.id], current_day,
            sentiment=multipliers.get(company.industry, 1.0),
            tentative_revenue=tentative[company.id],
            realized_revenue=realized[company.id],
        )
        report.companies.append(result)
        report.total_payroll += result.payroll_paid
        report.total_tax += result.tax_withheld
        if result.bankrupted:
            report.bankruptcies.append(result.ticker)

    # ---- Step 5b: pay payroll (state, straight from the pool) ------------
    await _pay_state_payroll(session, state, current_day, report)

    # ---- Step 7: auto clock-out ------------------------------------------
    await _clock_out_all(session, state.guild_id)

    # The close belongs to current_day.  Snapshot it before changing the state
    # cursor so valuation can reproduce the exact historical close.
    await valuation.record_closes(session, state)
    await _advance_day(session, state, rng=rng)

    return report


async def _advance_day(
    session: AsyncSession,
    state: ServerState,
    *,
    rng: random.Random | None,
) -> None:
    """Advance the persisted cursor and prepare a newly opened Monday."""
    new_day = gameday.next_day(state.game_day)
    state.game_day = new_day
    state.weekday = gameday.weekday_name(new_day)
    if gameday.is_monday(new_day):
        await events_mod.roll_weekly_events(session, state, new_day, rng=rng)


async def _vest_grants(session: AsyncSession, state: ServerState) -> int:
    """Advance every active grant deterministically and migrate newly-vested
    shares into holdings. Returns total shares vested this tick."""
    rows = (
        await session.execute(
            select(EquityGrant, Employment.user_id, Employment.company_id)
            .join(Employment, EquityGrant.employment_id == Employment.id)
            .join(Company, Employment.company_id == Company.id)
            .where(
                Company.guild_id == state.guild_id,
                Company.active == True,  # noqa: E712
            )
        )
    ).all()

    total = 0
    companies: dict[int, Company] = {}
    for grant, user_id, company_id in rows:
        if grant.days_elapsed >= grant.vest_days:
            await session.delete(grant)
            continue
        grant.days_elapsed += 1
        if grant.days_elapsed >= grant.vest_days:
            target = grant.total_shares
        else:
            target = grant.total_shares * grant.days_elapsed // grant.vest_days
        delta = target - grant.vested_shares
        if delta > 0:
            holding = await session.get(Holding, (company_id, user_id))
            if holding is None:
                holding = Holding(company_id=company_id, user_id=user_id, shares=0)
                session.add(holding)
            holding.shares += delta
            # Equity grants are dilutive: shares are minted as they vest, so the
            # company's total_shares grows in lockstep (keeping the invariant
            # total_shares == Σ holdings). Minting on vest also makes forfeiture
            # free — unvested shares were simply never created.
            company = companies.get(company_id)
            if company is None:
                company = await session.get(Company, company_id)
                companies[company_id] = company
            company.total_shares += delta
            grant.vested_shares = target
            total += delta
        if grant.days_elapsed >= grant.vest_days:
            await session.delete(grant)
    return total


async def _pay_private_payroll(
    session: AsyncSession,
    state: ServerState,
    company: Company,
    emps: list[Employment],
    game_day,
    *,
    sentiment: float,
    tentative_revenue: int,
    realized_revenue: int,
) -> CompanyTickResult:
    payroll_due = sum(e.daily_wage for e in emps)
    paid_total = 0
    tax_total = 0
    insolvent = False

    if payroll_due == 0:
        company.insolvent_days = 0
    elif company.treasury >= payroll_due:
        for e in emps:
            user = await money.get_or_create_user(session, state.guild_id, e.user_id)
            gross, tax = await money.pay_wage(session, state, company, user, e.daily_wage, game_day)
            paid_total += gross
            tax_total += tax
        company.insolvent_days = 0
    else:
        # Insolvent: pay pro-rata by wage from whatever the treasury holds.
        insolvent = True
        split = money.prorata_split(
            company.treasury, [(e.user_id, e.daily_wage) for e in emps]
        )
        for e in emps:
            share = split.get(e.user_id, 0)
            if share <= 0:
                continue
            user = await money.get_or_create_user(session, state.guild_id, e.user_id)
            gross, tax = await money.pay_wage(session, state, company, user, share, game_day)
            paid_total += gross
            tax_total += tax
        company.insolvent_days += 1

    bankrupted = False
    if insolvent and company.insolvent_days >= INSOLVENCY_GRACE_DAYS:
        await _bankrupt(session, state, company, game_day)
        bankrupted = True

    return CompanyTickResult(
        company_id=company.id,
        ticker=company.ticker,
        name=company.name,
        industry=company.industry,
        sentiment=sentiment,
        workers=len(emps),
        tentative_revenue=tentative_revenue,
        realized_revenue=realized_revenue,
        payroll_due=payroll_due,
        payroll_paid=paid_total,
        tax_withheld=tax_total,
        insolvent=insolvent,
        bankrupted=bankrupted,
    )


async def _bankrupt(
    session: AsyncSession, state: ServerState, company: Company, game_day
) -> None:
    """Unwind a dead company in one ordered pass (§18 bug 3): treasury → pool,
    void offers, close jobs, wipe holdings, end employment, deactivate."""
    await money.return_treasury_to_pool(session, state, company, game_day)
    await lifecycle.void_offers_involving(session, company)
    await lifecycle.close_funding_rounds(session, company)
    await lifecycle.close_jobs(session, company)
    await lifecycle.wipe_holdings(session, company)
    await lifecycle.end_all_employment(session, company)
    company.active = False
    company.insolvent_days = 0


async def _pay_state_payroll(
    session: AsyncSession, state: ServerState, game_day, report: TickReport
) -> None:
    """State payroll comes straight from the pool. If the pool can't cover it,
    pay pro-rata and ring the crisis bell."""
    state_companies = (
        await session.execute(
            select(Company).where(
                Company.guild_id == state.guild_id,
                Company.is_state == True,  # noqa: E712
                Company.active == True,  # noqa: E712
            )
        )
    ).scalars().all()

    # (company, employment) pairs for everyone clocked in at a state company.
    roster: list[tuple[Company, Employment]] = []
    for company in state_companies:
        emps = (
            await session.execute(
                select(Employment).where(
                    Employment.company_id == company.id,
                    Employment.clocked_in == True,  # noqa: E712
                )
            )
        ).scalars().all()
        for e in emps:
            roster.append((company, e))

    total_due = sum(e.daily_wage for _, e in roster)
    report.state_payroll_due = total_due
    if total_due == 0:
        return

    if state.pool_balance >= total_due:
        for company, e in roster:
            user = await money.get_or_create_user(session, state.guild_id, e.user_id)
            gross, tax = await money.pay_state_wage(session, state, company, user, e.daily_wage, game_day)
            report.state_payroll_paid += gross
            report.total_tax += tax
    else:
        # Crisis: the faucet ran dry. Pay pro-rata from the remaining pool.
        report.state_crisis = True
        split = money.prorata_split(
            state.pool_balance, [(e.id, e.daily_wage) for _, e in roster]
        )
        for company, e in roster:
            share = split.get(e.id, 0)
            if share <= 0:
                continue
            user = await money.get_or_create_user(session, state.guild_id, e.user_id)
            gross, tax = await money.pay_state_wage(session, state, company, user, share, game_day)
            report.state_payroll_paid += gross
            report.total_tax += tax


async def _clock_out_all(session: AsyncSession, guild_id: int) -> None:
    company_ids = select(Company.id).where(Company.guild_id == guild_id)
    await session.execute(
        update(Employment)
        .where(Employment.company_id.in_(company_ids))
        .values(clocked_in=False)
    )
