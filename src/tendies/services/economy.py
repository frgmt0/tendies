"""Macro / Manager-control service (Slice E).

This module owns the macro singleton — server bootstrap, money printing,
the tax rate, and the game-day cursor — plus the read used by ``$pool``.

It follows the service contract: each public coroutine takes ``(session, state, ...)``
where ``state`` is a :class:`~tendies.models.ServerState` already fetched (except
:func:`ensure_bootstrapped`, which *creates* the state and therefore takes the
guild id). Services mutate ORM objects and route every nuggie movement through
:mod:`tendies.money`; they never commit (the caller's session scope does) and
never import ``discord``. User errors raise :class:`~tendies.errors.GameError`
subclasses with player-facing messages.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config, events, gameday, money, valuation
from ..errors import BadInput
from ..models import Company, Employment, Job, ServerState, Transaction, User


# ---------------------------------------------------------------------------
# Bootstrap — idempotent server setup (§2, §7). Called before every command.
# ---------------------------------------------------------------------------

async def ensure_bootstrapped(
    session: AsyncSession, guild_id: int, today: dt.date
) -> ServerState:
    """Guarantee the guild's economy exists, seeded exactly once (idempotent).

    On first call for a guild this creates the macro singleton (pool seeded to
    :data:`config.STARTING_POOL`, inflation index ``1.0``, today's game day and
    weekday, default tax rate / founding-fee knobs), seeds every
    :data:`config.STATE_COMPANIES` as a share-less, revenue-less faucet with its
    always-open jobs, and best-effort rolls the current week's market events.

    On every subsequent call it is a no-op that simply returns the existing
    :class:`ServerState`. The bot's ``before_invoke`` hook calls this ahead of
    each command, so the guard MUST stay cheap and side-effect-free once seeded.
    """
    state = await session.get(ServerState, guild_id)
    if state is not None:
        return state

    # ---- macro singleton -------------------------------------------------
    state = ServerState(
        guild_id=guild_id,
        pool_balance=config.STARTING_POOL,
        inflation_index=1.0,
        game_day=today,
        weekday=gameday.weekday_name(today),
        tax_rate=config.DEFAULT_TAX_RATE,
        base_founding_fee=config.BASE_FOUNDING_FEE,
        fee_multiplier=config.FEE_MULTIPLIER,
    )
    session.add(state)
    await session.flush()

    # ---- state-owned companies + their always-open jobs (§7) -------------
    # Faucets: no shares, no treasury, no revenue. Jobs auto-accept (handled in
    # the employment service); here they're just opened at zero productivity so
    # state companies never produce in the tick.
    for seed in config.STATE_COMPANIES:
        # The ticker for a state company is derived from its name; it never
        # collides with a player ticker because state companies are seeded once
        # before anyone can found, and players pick 1–4 char codes by hand.
        company = Company(
            guild_id=guild_id,
            ticker=_state_ticker(seed.name),
            name=seed.name,
            owner_id=None,
            industry=seed.industry,
            treasury=0,
            total_shares=0,
            is_state=True,
            active=True,
            insolvent_days=0,
        )
        session.add(company)
        await session.flush()
        for job_seed in seed.jobs:
            session.add(
                Job(
                    company_id=company.id,
                    title=job_seed.title,
                    description=job_seed.description,
                    daily_wage=job_seed.daily_wage,
                    productivity=0,
                    equity_shares=None,
                    vest_days=None,
                    open=True,
                )
            )
    await session.flush()

    # ---- roll the current week's events (§5) -----------------------------
    # A guild may first use the bot mid-week.  Seed that week as well so the
    # event system is live immediately rather than waiting until next Monday.
    await events.roll_weekly_events(
        session, state, gameday.week_monday(today), not_before=today
    )

    return state


def _state_ticker(name: str) -> str:
    """Derive a stable, readable ticker (<=4 chars, uppercase) for a state
    company from its name. State companies are seeded before any player can
    found, so these never collide with player-chosen tickers."""
    letters = [c for c in name.upper() if c.isalnum()]
    return "".join(letters[:4]) if letters else "STAT"


# ---------------------------------------------------------------------------
# $print — money creation + inflation (§3)
# ---------------------------------------------------------------------------

def projected_index(inflation_index: float, amount: int, money_supply: int) -> float:
    """Preview the inflation index after printing ``amount`` (pure, no DB).

    Mirrors :func:`money.print_money`'s model:
    ``index_after = index_before × (1 + amount / supply_before)``. With an empty
    supply (only possible before any nuggies exist) the index is unchanged.
    """
    if money_supply <= 0:
        return inflation_index
    return inflation_index * (1 + amount / money_supply)


async def apply_print(session: AsyncSession, state: ServerState, amount: int) -> float:
    """Raise the pool by ``amount`` (must be positive) and return the new
    inflation index. The only minting path in the game (§3)."""
    if amount <= 0:
        raise BadInput("Print amount must be positive.")
    if amount > money.MAX_INT64 or state.pool_balance > money.MAX_INT64 - amount:
        raise BadInput("That print would exceed the economy's maximum balance.")
    return await money.print_money(session, state, amount, state.game_day)


# ---------------------------------------------------------------------------
# $taxrate — the refill knob (§9)
# ---------------------------------------------------------------------------

async def set_tax_rate(session: AsyncSession, state: ServerState, rate_fraction: float) -> None:
    """Set the wage + dividend tax rate. ``rate_fraction`` is a fraction in
    ``[0, 0.95)``; values at or above the ceiling are rejected so payroll can
    never round to zero take-home."""
    if not math.isfinite(rate_fraction) or rate_fraction < 0 or rate_fraction >= 0.95:
        raise BadInput("Tax rate must be between 0% and 95%.")
    state.tax_rate = rate_fraction


# ---------------------------------------------------------------------------
# $setday — correct calendar drift (§4)
# ---------------------------------------------------------------------------

async def set_day(session: AsyncSession, state: ServerState, weekday_name: str) -> None:
    """Re-anchor the game calendar to the next occurrence of ``weekday_name``
    on or after the current game day, keeping ``game_day``/``weekday`` in sync."""
    idx = gameday.parse_weekday(weekday_name)
    if idx is None:
        raise BadInput(f"Unknown weekday: **{weekday_name}**. Try e.g. `monday`.")
    state.game_day = gameday.date_for_weekday(state.game_day, idx)
    state.weekday = gameday.weekday_name(state.game_day)


# ---------------------------------------------------------------------------
# $pool — the macro snapshot (§2)
# ---------------------------------------------------------------------------

@dataclass
class PoolInfo:
    pool_balance: int
    inflation_index: float
    tax_rate: float
    money_supply: int


async def pool_info(session: AsyncSession, state: ServerState) -> PoolInfo:
    """The pool, inflation index, tax rate, and total money supply (§2)."""
    supply = await money.money_supply(session, state.guild_id)
    return PoolInfo(
        pool_balance=state.pool_balance,
        inflation_index=state.inflation_index,
        tax_rate=state.tax_rate,
        money_supply=supply,
    )


# ---------------------------------------------------------------------------
# $stats — the Manager macro dashboard. Everything here is *measured* from the
# ledger, holdings, and live valuations; nothing is stored or estimated.
# ---------------------------------------------------------------------------

#: Trailing window (business days) for the flow figures on the dashboard.
STATS_FLOW_WINDOW = 7


@dataclass
class MacroStats:
    # Money stock
    money_supply: int
    minted_since_start: int  # supply - STARTING_POOL == Σ all prints
    inflation_index: float
    pool_balance: int
    pool_real: float
    pool_pct: float  # pool as a share of supply
    wallets_total: int
    treasuries_total: int
    recession_cap: int  # 0.10 * pool — the aggregate daily revenue ceiling
    # Flows over the trailing window (nominal nuggies)
    flow_window_days: int
    flow_revenue: int
    flow_private_wages: int
    flow_state_wages: int
    flow_tax: int
    flow_dividends: int
    flow_printed: int
    # Real economy
    players_total: int
    players_employed: int
    players_clocked_in: int
    active_companies: int
    industry_counts: dict[str, int] = field(default_factory=dict)
    # Wealth concentration
    total_net_worth_real: float = 0.0
    gini: float = 0.0
    top_share: float = 0.0  # richest player's share of total net worth


def _gini(values: list[float]) -> float:
    """Gini coefficient (0 = perfect equality, →1 = one player holds everything)
    over non-negative net-worth values. Returns 0.0 for an empty/zero economy."""
    xs = sorted(v for v in values if v >= 0)
    n = len(xs)
    total = sum(xs)
    if n == 0 or total <= 0:
        return 0.0
    cumulative = sum(i * x for i, x in enumerate(xs, start=1))
    return (2.0 * cumulative) / (n * total) - (n + 1) / n


async def _flow(session: AsyncSession, guild_id: int, types, lo: dt.date, hi: dt.date) -> int:
    return int(await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.guild_id == guild_id,
            Transaction.type.in_(types),
            Transaction.game_day >= lo,
            Transaction.game_day <= hi,
        )
    ) or 0)


async def macro_stats(session: AsyncSession, state: ServerState) -> MacroStats:
    """Compute the Manager dashboard from real data (`$stats`)."""
    guild_id = state.guild_id
    supply = await money.money_supply(session, guild_id)
    wallets = int(await session.scalar(
        select(func.coalesce(func.sum(User.wallet), 0)).where(User.guild_id == guild_id)
    ) or 0)
    treasuries = int(await session.scalar(
        select(func.coalesce(func.sum(Company.treasury), 0)).where(Company.guild_id == guild_id)
    ) or 0)
    index = state.inflation_index or 1.0

    lo = gameday.business_days_before(state.game_day, STATS_FLOW_WINDOW - 1)
    hi = state.game_day

    # Labor & companies.
    players_total = int(await session.scalar(
        select(func.count()).select_from(User).where(User.guild_id == guild_id)
    ) or 0)
    players_employed = int(await session.scalar(
        select(func.count(func.distinct(Employment.user_id)))
        .select_from(Employment)
        .join(Company, Employment.company_id == Company.id)
        .where(Company.guild_id == guild_id)
    ) or 0)
    players_clocked_in = int(await session.scalar(
        select(func.count(func.distinct(Employment.user_id)))
        .select_from(Employment)
        .join(Company, Employment.company_id == Company.id)
        .where(Company.guild_id == guild_id, Employment.clocked_in == True)  # noqa: E712
    ) or 0)

    industry_rows = (await session.execute(
        select(Company.industry, func.count())
        .where(
            Company.guild_id == guild_id,
            Company.active == True,  # noqa: E712
            Company.is_state == False,  # noqa: E712
        )
        .group_by(Company.industry)
    )).all()
    industry_counts = {ind: int(n) for ind, n in industry_rows}
    active_companies = sum(industry_counts.values())

    # Wealth concentration over real net worth.
    price_map = await valuation.valuation_map(session, state)
    user_ids = (await session.execute(
        select(User.user_id).where(User.guild_id == guild_id)
    )).scalars().all()
    net_worths = [
        (await valuation.net_worth(session, state, uid, price_map=price_map)).total
        for uid in user_ids
    ]
    total_nw = sum(net_worths)
    top_share = (max(net_worths) / total_nw * 100) if net_worths and total_nw > 0 else 0.0

    return MacroStats(
        money_supply=supply,
        minted_since_start=supply - config.STARTING_POOL,
        inflation_index=index,
        pool_balance=state.pool_balance,
        pool_real=state.pool_balance / index,
        pool_pct=(state.pool_balance / supply * 100) if supply > 0 else 0.0,
        wallets_total=wallets,
        treasuries_total=treasuries,
        recession_cap=int(config.RECESSION_CAP_FRACTION * state.pool_balance),
        flow_window_days=STATS_FLOW_WINDOW,
        flow_revenue=await _flow(session, guild_id, ("revenue",), lo, hi),
        flow_private_wages=await _flow(session, guild_id, ("wage",), lo, hi),
        flow_state_wages=await _flow(session, guild_id, ("state_wage",), lo, hi),
        flow_tax=await _flow(session, guild_id, ("tax",), lo, hi),
        flow_dividends=await _flow(session, guild_id, ("dividend",), lo, hi),
        flow_printed=await _flow(session, guild_id, ("print",), lo, hi),
        players_total=players_total,
        players_employed=players_employed,
        players_clocked_in=players_clocked_in,
        active_companies=active_companies,
        industry_counts=industry_counts,
        total_net_worth_real=total_nw,
        gini=_gini(net_worths),
        top_share=top_share,
    )


__all__ = [
    "ensure_bootstrapped",
    "projected_index",
    "apply_print",
    "set_tax_rate",
    "set_day",
    "pool_info",
    "PoolInfo",
    "macro_stats",
    "MacroStats",
]
