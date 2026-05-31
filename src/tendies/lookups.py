"""Shared lookups used across services.

These are the few read helpers every service needs (fetch the guild's state,
resolve a ticker, check ownership), centralized so each service doesn't
re-implement them and they raise consistent :class:`GameError` messages.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import NotAllowed, NotFound
from .models import Company, Employment, ServerState


async def get_state(
    session: AsyncSession, guild_id: int, *, for_update: bool = False
) -> ServerState:
    """The guild's economy. Bootstrap runs in the bot's before_invoke hook, so
    this normally exists; raises if called before bootstrap (e.g. in a test).

    Pass ``for_update=True`` on the tick paths to take a row lock so a manual
    ``$forcetick`` and the scheduled tick can't double-advance the same guild on
    Postgres (the clause is a no-op on SQLite, which serializes writers anyway).
    """
    if for_update:
        state = (
            await session.execute(
                select(ServerState).where(ServerState.guild_id == guild_id).with_for_update()
            )
        ).scalars().first()
    else:
        state = await session.get(ServerState, guild_id)
    if state is None:
        raise NotFound("This server's economy hasn't been set up yet.")
    return state


async def get_company(
    session: AsyncSession,
    guild_id: int,
    ticker: str,
    *,
    active_only: bool = True,
    private_only: bool = False,
) -> Company:
    """Resolve a company by ticker (case-insensitive). Raises :class:`NotFound`
    if missing/inactive, or if ``private_only`` and it's state-owned."""
    ticker_norm = (ticker or "").strip().upper()
    stmt = select(Company).where(
        Company.guild_id == guild_id,
        func.upper(Company.ticker) == ticker_norm,
    )
    if active_only:
        stmt = stmt.where(Company.active == True)  # noqa: E712
    company = (await session.execute(stmt)).scalars().first()
    if company is None:
        raise NotFound(f"No company with ticker **{ticker_norm}**.")
    if private_only and company.is_state:
        raise NotFound(f"**{company.ticker}** is a state-owned company — that doesn't apply to it.")
    return company


def require_owner(company: Company, user_id: int) -> None:
    """Raise :class:`NotAllowed` unless ``user_id`` owns ``company``."""
    if company.owner_id != user_id:
        raise NotAllowed(f"Only the owner of **{company.ticker}** can do that.")


async def get_employment(
    session: AsyncSession, guild_id: int, user_id: int
) -> Employment | None:
    """The user's current employment in this guild (at most one), or ``None``."""
    return (
        await session.execute(
            select(Employment)
            .join(Company, Employment.company_id == Company.id)
            .where(Company.guild_id == guild_id, Employment.user_id == user_id)
        )
    ).scalars().first()
