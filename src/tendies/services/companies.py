"""Companies & jobs service (SLICE B, DESIGN §8/§9).

Founding a private company, inspecting it, posting jobs, reviewing applicants,
and hiring / firing. Every money movement flows through :mod:`tendies.money`
and every teardown through :mod:`tendies.lifecycle`; this module only mutates
ORM objects and never commits (the caller's session context does that). User
errors are raised as :class:`~tendies.errors.GameError` subclasses with
player-facing messages.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lifecycle, lookups, money
from .. import valuation as valuation_mod
from ..config import (
    DEFAULT_PRODUCTIVITY,
    INDUSTRIES,
    MAX_JOB_GRANT_FRACTION,
    MIN_GRANT_VEST_DAYS,
    SHARES_AT_FOUNDING,
    normalize_industry,
)
from ..errors import BadInput, GameError, InsufficientFunds, NotAllowed, NotFound
from ..formatting import fmt
from ..models import (
    Application,
    Company,
    Employment,
    EquityGrant,
    Holding,
    Job,
    ServerState,
)
from ..valuation import CompanyValuation


# ---------------------------------------------------------------------------
# Founding
# ---------------------------------------------------------------------------

@dataclass
class FoundResult:
    company: Company
    fee: int
    companies_owned_after: int


def _valid_ticker(ticker: str) -> str | None:
    """Normalize + validate a ticker. Returns the UPPERCASE ticker or ``None``."""
    t = (ticker or "").strip().upper()
    if not (1 <= len(t) <= 4) or not t.isalnum():
        return None
    return t


async def found_company(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    ticker: str,
    name: str,
    industry: str,
) -> FoundResult:
    """Create a private company owned by ``owner_id`` and mint its founding
    shares (100% to the founder). Charges the scaling founding fee to the pool.
    """
    ticker_norm = _valid_ticker(ticker)
    if ticker_norm is None:
        raise BadInput(
            "Ticker must be 1-4 alphanumeric characters (e.g. `MOON`)."
        )

    name_clean = (name or "").strip()
    if not name_clean:
        raise BadInput("A company needs a name.")

    industry_norm = normalize_industry(industry)
    if industry_norm is None:
        raise BadInput(
            f"Unknown industry **{industry}**. Pick one of: "
            f"{', '.join(INDUSTRIES)}."
        )

    # Ticker must be unique per server, active or not.
    existing = (
        await session.execute(
            select(Company).where(
                Company.guild_id == state.guild_id,
                func.upper(Company.ticker) == ticker_norm,
            )
        )
    ).scalars().first()
    if existing is not None:
        raise BadInput(f"Ticker **{ticker_norm}** is taken.")

    # Fee scales with companies currently owned (active, non-state).
    owned = await session.scalar(
        select(func.count())
        .select_from(Company)
        .where(
            Company.guild_id == state.guild_id,
            Company.owner_id == owner_id,
            Company.is_state == False,  # noqa: E712
            Company.active == True,  # noqa: E712
        )
    )
    owned = int(owned or 0)
    fee = int(state.base_founding_fee * (state.fee_multiplier ** owned))
    if fee > money.MAX_INT64:
        raise BadInput("The founding fee is too large to store safely.")

    user = await money.get_or_create_user(session, state.guild_id, owner_id)
    if user.wallet < fee:
        raise InsufficientFunds(
            f"Founding your {_ordinal(owned + 1)} company costs **{fmt(fee)} nug**, "
            f"but you only have **{fmt(user.wallet)} nug**."
        )

    await money.charge_fee(
        session,
        state,
        user,
        fee,
        state.game_day,
        note=f"founding {ticker_norm}",
    )

    company = Company(
        guild_id=state.guild_id,
        ticker=ticker_norm,
        name=name_clean,
        owner_id=user.user_id,
        industry=industry_norm,
        treasury=0,
        total_shares=SHARES_AT_FOUNDING,
        is_state=False,
        active=True,
    )
    session.add(company)
    await session.flush()

    session.add(
        Holding(
            company_id=company.id,
            user_id=user.user_id,
            shares=SHARES_AT_FOUNDING,
        )
    )
    await session.flush()

    return FoundResult(
        company=company,
        fee=fee,
        companies_owned_after=owned + 1,
    )


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', etc."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------------------
# Company detail
# ---------------------------------------------------------------------------

@dataclass
class CompanyDetail:
    company: Company
    cap_table: list[tuple[int, int]]
    clocked_in: int
    total_employees: int
    valuation: "CompanyValuation | None"  # None for state companies


async def company_detail(
    session: AsyncSession, state: ServerState, ticker: str
) -> CompanyDetail:
    """Assemble the full picture for ``$company`` (DESIGN §8)."""
    company = await lookups.get_company(session, state.guild_id, ticker)

    cap = await lifecycle.cap_table(session, company)

    total_employees = int(
        await session.scalar(
            select(func.count())
            .select_from(Employment)
            .where(Employment.company_id == company.id)
        )
        or 0
    )
    clocked_in = int(
        await session.scalar(
            select(func.count())
            .select_from(Employment)
            .where(
                Employment.company_id == company.id,
                Employment.clocked_in == True,  # noqa: E712
            )
        )
        or 0
    )

    valuation: CompanyValuation | None = None
    if not company.is_state:
        valuation = await valuation_mod.company_valuation(session, state, company)

    return CompanyDetail(
        company=company,
        cap_table=cap,
        clocked_in=clocked_in,
        total_employees=total_employees,
        valuation=valuation,
    )


# ---------------------------------------------------------------------------
# Posting jobs
# ---------------------------------------------------------------------------

async def post_job(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    ticker: str,
    title: str,
    description: str,
    daily_wage: int,
    equity_shares: int,
    vest_days: int,
) -> Job:
    """Owner posts a new open job at their private company."""
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    lookups.require_owner(company, owner_id)

    title_clean = (title or "").strip()
    if not title_clean:
        raise BadInput("A job needs a title.")

    if isinstance(daily_wage, bool) or not isinstance(daily_wage, int) or daily_wage <= 0:
        raise BadInput("Daily wage must be a positive whole number of nuggies.")
    if daily_wage > money.MAX_INT64:
        raise BadInput("Daily wage is too large to store safely.")
    if isinstance(equity_shares, bool) or not isinstance(equity_shares, int) or equity_shares < 0:
        raise BadInput("Equity shares must be a non-negative whole number.")
    if equity_shares > money.MAX_INT64:
        raise BadInput("Equity grant is too large to store safely.")
    if isinstance(vest_days, bool) or not isinstance(vest_days, int):
        raise BadInput("Vesting period must be a whole number of business days.")
    if equity_shares > 0 and vest_days <= 0:
        raise BadInput("An equity grant needs a positive vesting period (in business days).")
    if equity_shares > 0:
        # §10: a grant dilutes every existing shareholder, so one posting can
        # only ever hand out a slice of the current cap table, and it has to
        # vest over enough closes that investors can react.
        max_grant = int(company.total_shares * MAX_JOB_GRANT_FRACTION)
        if equity_shares > max_grant:
            raise BadInput(
                f"An equity grant can't exceed "
                f"{MAX_JOB_GRANT_FRACTION:.0%} of **{company.ticker}**'s "
                f"{fmt(company.total_shares)} shares — "
                f"that's {fmt(max_grant)} shares, and you posted "
                f"{fmt(equity_shares)}."
            )
        if vest_days < MIN_GRANT_VEST_DAYS:
            raise BadInput(
                f"An equity grant has to vest over at least "
                f"{MIN_GRANT_VEST_DAYS} business days."
            )

    stored_equity = equity_shares if equity_shares > 0 else None
    stored_vest = vest_days if equity_shares > 0 else None

    job = Job(
        company_id=company.id,
        title=title_clean,
        description=(description or "").strip(),
        daily_wage=int(daily_wage),
        productivity=DEFAULT_PRODUCTIVITY,
        equity_shares=stored_equity,
        vest_days=stored_vest,
        open=True,
    )
    session.add(job)
    await session.flush()
    return job


# ---------------------------------------------------------------------------
# Applicants
# ---------------------------------------------------------------------------

@dataclass
class ApplicantInfo:
    application_id: int
    user_id: int
    job_id: int
    job_title: str
    net_worth: float
    current_label: str


async def _current_label(
    session: AsyncSession, state: ServerState, user_id: int
) -> str:
    """The user's current job as ``Title @ Company``, or ``unemployed``."""
    emp = await lookups.get_employment(session, state.guild_id, user_id)
    if emp is None:
        return "unemployed"
    job = await session.get(Job, emp.job_id)
    company = await session.get(Company, emp.company_id)
    title = job.title if job is not None else "Worker"
    name = company.name if company is not None else "?"
    return f"{title} @ {name}"


async def list_applicants(
    session: AsyncSession, state: ServerState, owner_id: int, ticker: str
) -> list[ApplicantInfo]:
    """Pending applications across all of the company's jobs (owner-only),
    in stable application-id order."""
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    lookups.require_owner(company, owner_id)

    rows = (
        await session.execute(
            select(Application, Job.title)
            .join(Job, Application.job_id == Job.id)
            .where(
                Job.company_id == company.id,
                Application.status == "pending",
            )
            .order_by(Application.id.asc())
        )
    ).all()

    # Compute net worth once over the shared price map.
    price_map = await valuation_mod.valuation_map(session, state)

    result: list[ApplicantInfo] = []
    for application, job_title in rows:
        nw = await valuation_mod.net_worth(
            session, state, application.user_id, price_map=price_map
        )
        label = await _current_label(session, state, application.user_id)
        result.append(
            ApplicantInfo(
                application_id=application.id,
                user_id=application.user_id,
                job_id=application.job_id,
                job_title=job_title,
                net_worth=nw.total,
                current_label=label,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Hiring
# ---------------------------------------------------------------------------

@dataclass
class HireResult:
    user_id: int
    job_title: str
    daily_wage: int
    equity_shares: int
    vest_days: int


async def hire(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    ticker: str,
    application_id: int,
) -> HireResult:
    """Accept an application and start the employee (owner-only). Snapshots wage
    + productivity from the job and opens an equity grant if the job carries one."""
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    lookups.require_owner(company, owner_id)

    application = await session.get(Application, application_id)
    if application is None:
        raise NotFound(f"No application #{application_id}.")
    if application.status != "pending":
        raise BadInput(f"Application #{application_id} is no longer pending.")

    job = await session.get(Job, application.job_id)
    if job is None or job.company_id != company.id:
        raise NotFound(f"Application #{application_id} isn't for **{company.ticker}**.")
    if not job.open:
        raise BadInput(f"The **{job.title}** role is closed.")

    # An owner can't be their own employee (self-dealt grant + company-funded
    # wage). Mirrored in ``employment.apply_to_job`` so neither path admits it.
    if application.user_id == company.owner_id:
        raise BadInput(
            f"You own **{company.ticker}** — you can't hire yourself."
        )

    # The applicant must be currently unemployed.
    existing = await lookups.get_employment(session, state.guild_id, application.user_id)
    if existing is not None:
        raise GameError(
            "That applicant already has a job — they have to quit it before they can be hired."
        )

    employment = Employment(
        company_id=company.id,
        user_id=application.user_id,
        job_id=job.id,
        daily_wage=job.daily_wage,
        productivity=job.productivity,
        clocked_in=False,
        hired_at=state.game_day,
    )
    session.add(employment)
    await session.flush()

    application.status = "accepted"

    equity_shares = 0
    vest_days = 0
    if job.equity_shares:
        equity_shares = int(job.equity_shares)
        vest_days = int(job.vest_days)
        session.add(
            EquityGrant(
                employment_id=employment.id,
                total_shares=equity_shares,
                vest_days=vest_days,
                days_elapsed=0,
                vested_shares=0,
                daily_vest=equity_shares / vest_days,
            )
        )
        await session.flush()

    return HireResult(
        user_id=application.user_id,
        job_title=job.title,
        daily_wage=int(job.daily_wage),
        equity_shares=equity_shares,
        vest_days=vest_days,
    )


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------

async def fire(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    ticker: str,
    target_user_id: int,
) -> None:
    """Owner ends a target's employment at this company. Deletes the employment
    and its equity grant (vested shares already live in holdings; unvested are
    forfeit via the cascade)."""
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    lookups.require_owner(company, owner_id)

    employment = (
        await session.execute(
            select(Employment).where(
                Employment.company_id == company.id,
                Employment.user_id == target_user_id,
            )
        )
    ).scalars().first()
    if employment is None:
        raise NotFound(
            f"No one with that user works at **{company.ticker}**."
        )

    if employment.clocked_in:
        raise NotAllowed(
            "That employee is clocked in today. Finish today's tick before firing "
            "them so earned payroll is honored."
        )

    await session.delete(employment)
    await session.flush()


@dataclass
class PromoteResult:
    ticker: str
    company_name: str
    user_id: int
    old_wage: int
    new_wage: int
    pct: float


async def promote(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    target_user_id: int,
    pct: float,
) -> PromoteResult:
    """Give an employee a raise of ``pct`` percent on their daily wage.

    The company is resolved from the target's (single) employment, and the
    caller must own it. Only the wage changes — future payroll is paid from the
    treasury at the new rate from the next tick. Raises are positive; the new
    wage must round up to a real increase.
    """
    if not math.isfinite(pct) or pct <= 0:
        raise BadInput("A raise has to be a positive percentage, e.g. `10` for +10%.")
    if pct > 1000:
        raise BadInput("That's an absurd raise (max 1000%). Pick a smaller number.")

    employment = await lookups.get_employment(session, state.guild_id, target_user_id)
    if employment is None:
        raise NotFound("That user isn't employed, so there's nothing to raise.")

    company = await session.get(Company, employment.company_id)
    if company is None or company.is_state:
        raise NotAllowed("State wages are fixed — you can't promote a state employee.")
    lookups.require_owner(company, owner_id)

    old_wage = employment.daily_wage
    new_wage = int(round(old_wage * (1 + pct / 100)))
    if new_wage > money.MAX_INT64:
        raise BadInput("That raise would make the wage too large to store safely.")
    if new_wage <= old_wage:
        raise BadInput(
            "That raise rounds to no change at this wage — bump the percentage."
        )
    employment.daily_wage = new_wage
    await session.flush()

    return PromoteResult(
        ticker=company.ticker,
        company_name=company.name,
        user_id=target_user_id,
        old_wage=old_wage,
        new_wage=new_wage,
        pct=pct,
    )
