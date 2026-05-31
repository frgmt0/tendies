"""Player-lifecycle service (§6, §9 clock-in, §10, §13).

The path out of poverty: read the board (:func:`list_open_jobs`), apply
(:func:`apply_to_job`), clock in each business day (:func:`clock_in` /
:func:`clock_out`), build a stake, and eventually quit (:func:`quit_job`) keeping
whatever equity has vested. :func:`balance` and :func:`employment_summary` are
the read views.

Services take a :class:`ServerState` already fetched by the caller, mutate ORM
objects (and call :mod:`tendies.money` / :mod:`tendies.lifecycle` as needed), and
return dataclasses. They never commit and never import discord; user errors are
raised as :class:`~tendies.errors.GameError` subclasses with player-facing text.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import gameday, money, valuation
from ..errors import BadInput, GameError, NotFound
from ..lookups import get_employment
from ..models import (
    Application,
    Company,
    Employment,
    EquityGrant,
    Holding,
    Job,
    ServerState,
)


# ---------------------------------------------------------------------------
# Open jobs
# ---------------------------------------------------------------------------

@dataclass
class JobListing:
    job_id: int
    ticker: str
    company_name: str
    is_state: bool
    title: str
    daily_wage: int
    equity_shares: int | None
    vest_days: int | None


async def list_open_jobs(session: AsyncSession, state: ServerState) -> list[JobListing]:
    """Open jobs at active companies. State companies first, then by ticker."""
    rows = (
        await session.execute(
            select(Job, Company)
            .join(Company, Job.company_id == Company.id)
            .where(
                Company.guild_id == state.guild_id,
                Company.active == True,  # noqa: E712
                Job.open == True,  # noqa: E712
            )
        )
    ).all()

    listings = [
        JobListing(
            job_id=job.id,
            ticker=company.ticker,
            company_name=company.name,
            is_state=company.is_state,
            title=job.title,
            daily_wage=job.daily_wage,
            equity_shares=job.equity_shares,
            vest_days=job.vest_days,
        )
        for job, company in rows
    ]
    # State jobs first; within each group, by ticker then job id for stability.
    listings.sort(key=lambda j: (not j.is_state, j.ticker, j.job_id))
    return listings


# ---------------------------------------------------------------------------
# Applying / hiring entry point (state auto-accept)
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    auto_accepted: bool
    company_name: str
    title: str
    daily_wage: int
    ticker: str


async def apply_to_job(
    session: AsyncSession, state: ServerState, user_id: int, job_id: int
) -> ApplyResult:
    """File an application. State jobs auto-accept (the applicant must be
    unemployed first); private jobs queue a pending :class:`Application`."""
    job = await session.get(Job, job_id)
    if job is None or not job.open:
        raise NotFound(f"No open job with ID **{job_id}**.")
    company = await session.get(Company, job.company_id)
    if company is None or not company.active:
        raise NotFound(f"No open job with ID **{job_id}**.")

    if company.is_state:
        existing = await get_employment(session, state.guild_id, user_id)
        if existing is not None:
            raise GameError(
                "You already have a job — quit it first with `$quit` before "
                "taking a state job."
            )
        employment = Employment(
            company_id=company.id,
            user_id=user_id,
            job_id=job.id,
            daily_wage=job.daily_wage,
            productivity=0,  # state companies don't produce revenue.
            clocked_in=False,
            hired_at=state.game_day,
        )
        session.add(employment)
        session.add(
            Application(
                job_id=job.id,
                user_id=user_id,
                status="accepted",
                applied_at=state.game_day,
            )
        )
        await session.flush()
        return ApplyResult(
            auto_accepted=True,
            company_name=company.name,
            title=job.title,
            daily_wage=job.daily_wage,
            ticker=company.ticker,
        )

    # Private job — queue a pending application (one outstanding per job+user).
    pending = (
        await session.execute(
            select(Application).where(
                Application.job_id == job.id,
                Application.user_id == user_id,
                Application.status == "pending",
            )
        )
    ).scalars().first()
    if pending is not None:
        raise BadInput(
            f"You already have a pending application for **{company.ticker}** — "
            f"{job.title}."
        )
    session.add(
        Application(
            job_id=job.id,
            user_id=user_id,
            status="pending",
            applied_at=state.game_day,
        )
    )
    await session.flush()
    return ApplyResult(
        auto_accepted=False,
        company_name=company.name,
        title=job.title,
        daily_wage=job.daily_wage,
        ticker=company.ticker,
    )


# ---------------------------------------------------------------------------
# Clocking in / out
# ---------------------------------------------------------------------------

@dataclass
class ClockResult:
    company_name: str
    daily_wage: int
    already: bool


async def clock_in(
    session: AsyncSession, state: ServerState, user_id: int
) -> ClockResult:
    """Clock in for the day: collect today's wage at the tick and contribute to
    the employer's production. Closed on weekends; unemployed players can't."""
    if not gameday.is_business_day(state.game_day):
        raise BadInput(
            "The market is closed — it's the weekend. No clocking in until Monday."
        )
    employment = await get_employment(session, state.guild_id, user_id)
    if employment is None:
        raise NotFound(
            "You're not employed. Find a job with `$jobs` and `$apply <job_id>`."
        )
    already = employment.clocked_in
    employment.clocked_in = True
    company = await session.get(Company, employment.company_id)
    return ClockResult(
        company_name=company.name if company else "",
        daily_wage=employment.daily_wage,
        already=already,
    )


async def clock_out(
    session: AsyncSession, state: ServerState, user_id: int
) -> str:
    """Clock out for the day. Returns the company name. Unemployed → NotFound."""
    employment = await get_employment(session, state.guild_id, user_id)
    if employment is None:
        raise NotFound("You're not employed, so there's nothing to clock out of.")
    employment.clocked_in = False
    company = await session.get(Company, employment.company_id)
    return company.name if company else ""


# ---------------------------------------------------------------------------
# Employment summary (vesting view)
# ---------------------------------------------------------------------------

@dataclass
class EmploymentInfo:
    company_name: str
    ticker: str
    title: str
    vested: int
    unvested: int
    total_grant: int
    days_in: int


async def _resolve_employment(
    session: AsyncSession, state: ServerState, user_id: int, ticker: str | None
) -> Employment:
    """The user's employment, optionally constrained to a given company ticker."""
    employment = await get_employment(session, state.guild_id, user_id)
    if employment is None:
        raise NotFound("You're not employed anywhere.")
    if ticker is not None:
        company = await session.get(Company, employment.company_id)
        if company is None or company.ticker.upper() != ticker.strip().upper():
            raise NotFound(
                f"You don't work at **{ticker.strip().upper()}**."
            )
    return employment


async def employment_summary(
    session: AsyncSession,
    state: ServerState,
    user_id: int,
    ticker: str | None = None,
) -> EmploymentInfo:
    """The user's employment with its vesting breakdown (vested / unvested)."""
    employment = await _resolve_employment(session, state, user_id, ticker)
    company = await session.get(Company, employment.company_id)
    job = await session.get(Job, employment.job_id)

    grant = (
        await session.execute(
            select(EquityGrant).where(EquityGrant.employment_id == employment.id)
        )
    ).scalars().first()
    if grant is not None:
        total = int(grant.total_shares)
        vested = int(grant.vested_shares)
        unvested = max(0, total - vested)
        days_in = int(grant.days_elapsed)
    else:
        total = vested = unvested = 0
        # No grant: days_in is best expressed as business days since hire.
        days_in = gameday.business_days_in_span(employment.hired_at, state.game_day)
        days_in = max(0, days_in - 1)  # the hire day itself isn't a worked day.

    return EmploymentInfo(
        company_name=company.name if company else "",
        ticker=company.ticker if company else "",
        title=job.title if job else "",
        vested=vested,
        unvested=unvested,
        total_grant=total,
        days_in=days_in,
    )


# ---------------------------------------------------------------------------
# Quitting
# ---------------------------------------------------------------------------

@dataclass
class QuitResult:
    company_name: str
    ticker: str
    vested: int
    forfeited: int


async def quit_job(
    session: AsyncSession,
    state: ServerState,
    user_id: int,
    ticker: str | None = None,
) -> QuitResult:
    """Leave a job. Vested shares already live in holdings and are kept; unvested
    grant shares are forfeited. Deletes the employment and its grant."""
    employment = await _resolve_employment(session, state, user_id, ticker)
    company = await session.get(Company, employment.company_id)
    company_name = company.name if company else ""
    company_ticker = company.ticker if company else ""

    grant = (
        await session.execute(
            select(EquityGrant).where(EquityGrant.employment_id == employment.id)
        )
    ).scalars().first()
    if grant is not None:
        vested = int(grant.vested_shares)
        forfeited = max(0, int(grant.total_shares) - vested)
        await session.delete(grant)
    else:
        vested = 0
        forfeited = 0

    await session.delete(employment)
    await session.flush()

    return QuitResult(
        company_name=company_name,
        ticker=company_ticker,
        vested=vested,
        forfeited=forfeited,
    )


# ---------------------------------------------------------------------------
# Balance / net worth
# ---------------------------------------------------------------------------

@dataclass
class HoldingLine:
    ticker: str
    name: str
    shares: int
    value: float  # real


@dataclass
class BalanceInfo:
    wallet_real: float
    net_worth: float
    employment_label: str
    holdings: list[HoldingLine]


async def balance(
    session: AsyncSession, state: ServerState, user_id: int
) -> BalanceInfo:
    """Real wallet, employment label, real net worth, and real-valued holdings."""
    nw = await valuation.net_worth(session, state, user_id)

    employment = await get_employment(session, state.guild_id, user_id)
    if employment is None:
        employment_label = "Unemployed"
    else:
        company = await session.get(Company, employment.company_id)
        job = await session.get(Job, employment.job_id)
        title = job.title if job else "Employee"
        company_name = company.name if company else "?"
        employment_label = f"{title} @ {company_name}"

    holdings = [
        HoldingLine(ticker=h.ticker, name=h.name, shares=h.shares, value=h.value)
        for h in nw.holdings
    ]

    return BalanceInfo(
        wallet_real=nw.wallet_real,
        net_worth=nw.total,
        employment_label=employment_label,
        holdings=holdings,
    )
