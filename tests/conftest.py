"""Shared fixtures + helpers for the Tendies engine/service test-suite.

Never imports ``discord``. Everything runs against an in-memory SQLite database
created fresh per test, with a ``StaticPool`` so the single in-memory connection
is shared across the async session's checkouts (otherwise each checkout would get
its own, empty, database).

pytest is configured with ``asyncio_mode = auto`` (see ``pyproject.toml``), so the
async fixtures and async test functions need no decorators.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.pool import StaticPool

from tendies import money, tick
from tendies.db import Database
from tendies.models import Company, Event, Holding, ServerState, User
from tendies.services import economy


# A guild id reused across tests; each test gets its own fresh DB so this is fine.
GUILD_ID = 123456789


# ---------------------------------------------------------------------------
# Database fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    """A fresh in-memory SQLite database per test.

    StaticPool keeps the one in-memory connection alive across the async
    session's connection checkouts, so the schema and data persist for the
    lifetime of the test.
    """
    database = Database(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    await database.create_all()
    try:
        yield database
    finally:
        await database.dispose()


# ---------------------------------------------------------------------------
# A tiny harness that owns one persistent session for a test.
#
# Services don't commit; the surrounding ``Database.session()`` context commits
# on exit. For tests we want a long-lived session we can ``flush`` on and read
# back from, so the harness opens one session directly off the sessionmaker and
# flushes after each mutating step (and the StaticPool keeps everything visible).
# ---------------------------------------------------------------------------

class World:
    """A bootstrapped guild + an open session, plus economy helpers."""

    def __init__(self, db: Database, session, state: ServerState):
        self.db = db
        self.session = session
        self.state = state

    @property
    def guild_id(self) -> int:
        return self.state.guild_id

    # -- money helpers ----------------------------------------------------

    async def make_rich(self, user_id: int, amount: int) -> User:
        """Fund a user's wallet straight from the pool (a conserving transfer).

        Does not record a ledger row, so it does NOT count as income for the
        accredited gate — tests that need real income drive ticks instead.
        """
        user = await money.get_or_create_user(self.session, self.guild_id, user_id)
        self.state.pool_balance -= amount
        user.wallet += amount
        await self.session.flush()
        return user

    async def wallet(self, user_id: int) -> int:
        user = await self.session.get(User, (self.guild_id, user_id))
        return int(user.wallet) if user else 0

    async def holding(self, company_id: int, user_id: int) -> int:
        h = await self.session.get(Holding, (company_id, user_id))
        return int(h.shares) if h else 0

    async def money_supply(self) -> int:
        return await money.money_supply(self.session, self.guild_id)

    async def assert_supply(self, expected: int) -> None:
        actual = await self.money_supply()
        assert actual == expected, f"money_supply {actual:,} != expected {expected:,}"

    # -- calendar / tick helpers -----------------------------------------

    async def set_weekday(self, name: str) -> None:
        """Re-anchor the calendar so ``game_day`` falls on ``name`` (>= today)."""
        await economy.set_day(self.session, self.state, name)
        await self.session.flush()

    async def tick(self, *, rng=None) -> tick.TickReport:
        """Settle the current day, then advance the calendar by one date."""
        report = await tick.run_tick(self.session, self.state, rng=rng)
        await self.session.flush()
        return report

    async def tick_business_days(self, n: int, *, rng=None) -> list[tick.TickReport]:
        """Run ``n`` *business-day* (producing) ticks, skipping past weekends.

        Workers must be clocked in before each producing tick; callers clock in
        between ticks as needed. This helper just guarantees each returned report
        landed on a business day.
        """
        reports: list[tick.TickReport] = []
        produced = 0
        guard = 0
        while produced < n and guard < n * 5 + 10:
            guard += 1
            report = await self.tick(rng=rng)
            if report.is_business_day:
                reports.append(report)
                produced += 1
        assert produced == n, "failed to advance the requested business days"
        return reports

    async def clear_events(self) -> None:
        """Delete every event for the guild (random rolls) for determinism."""
        await self.session.execute(delete(Event).where(Event.guild_id == self.guild_id))
        await self.session.flush()


@pytest_asyncio.fixture
async def world(db):
    """A bootstrapped guild anchored on Monday with no random events, plus a
    live session. The session is committed at the end so nothing leaks."""
    session = db.sessionmaker()
    # Bootstrap on a known Monday (2024-01-01 is a Monday).
    state = await economy.ensure_bootstrapped(session, GUILD_ID, dt.date(2024, 1, 1))
    await session.flush()
    w = World(db, session, state)
    await w.clear_events()  # deterministic: drop any randomly-rolled events
    try:
        yield w
        await session.commit()
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Generic bootstrap helper (for tests that want their own guild/day)
# ---------------------------------------------------------------------------

async def bootstrap_world(db: Database, *, guild_id: int = GUILD_ID, weekday: str = "monday") -> World:
    """Bootstrap a guild on a chosen weekday, returning a ready :class:`World`.

    Anchors ``game_day`` to the requested weekday, on/after 2024-01-01 (a Monday),
    and clears auto-rolled random events for determinism.
    """
    session = db.sessionmaker()
    state = await economy.ensure_bootstrapped(session, guild_id, dt.date(2024, 1, 1))
    await session.flush()
    w = World(db, session, state)
    await w.clear_events()
    if weekday != "monday":
        await w.set_weekday(weekday)
        await w.clear_events()
    return w


# ---------------------------------------------------------------------------
# Convenience: resolve a seeded state company + one of its job ids
# ---------------------------------------------------------------------------

async def first_state_job(world: World, title_contains: str | None = None) -> tuple[int, int]:
    """Return ``(company_id, job_id)`` for the first state job (optionally one
    whose title contains ``title_contains``)."""
    from tendies.models import Job

    rows = (
        await world.session.execute(
            select(Job, Company)
            .join(Company, Job.company_id == Company.id)
            .where(Company.guild_id == world.guild_id, Company.is_state == True)  # noqa: E712
            .order_by(Job.id.asc())
        )
    ).all()
    for job, company in rows:
        if title_contains is None or title_contains.lower() in job.title.lower():
            return company.id, job.id
    raise AssertionError("no matching state job found")
