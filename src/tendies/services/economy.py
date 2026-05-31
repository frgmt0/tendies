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
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from .. import config, events, gameday, money
from ..errors import BadInput
from ..models import Company, Job, ServerState


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

    # ---- best-effort roll of the current week's events (§5) --------------
    monday = today - dt.timedelta(days=today.weekday())
    await events.roll_weekly_events(session, state, monday)

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
    return await money.print_money(session, state, amount, state.game_day)


# ---------------------------------------------------------------------------
# $taxrate — the refill knob (§9)
# ---------------------------------------------------------------------------

async def set_tax_rate(session: AsyncSession, state: ServerState, rate_fraction: float) -> None:
    """Set the wage + dividend tax rate. ``rate_fraction`` is a fraction in
    ``[0, 0.95)``; values at or above the ceiling are rejected so payroll can
    never round to zero take-home."""
    if rate_fraction < 0 or rate_fraction >= 0.95:
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


__all__ = [
    "ensure_bootstrapped",
    "projected_index",
    "apply_print",
    "set_tax_rate",
    "set_day",
    "pool_info",
    "PoolInfo",
]
