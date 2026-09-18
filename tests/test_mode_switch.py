"""Switching a guild from accelerated playtesting back to calendar mode (§4).

Accelerated mode (and `$setday`) can push ``server_state.game_day`` past the
host's real date. In calendar mode the cursor only ever moves *forward* toward
today, so a future cursor means no date ever closes again — silently. Startup
synchronization must notice, warn loudly with both dates, and snap the cursor
back to today *without* settling anything.
"""

from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

from sqlalchemy import func, select

from tendies import gameday, money
from tendies.config import STARTING_POOL
from tendies.models import (
    Company, Employment, Job, MarketClose, ServerState, Transaction,
)
from tendies.scheduler import TickScheduler
from tendies.services import companies, economy, employment

GUILD = 60_001
WORKER = 60_002


def _scheduler(db, *, accelerated: bool = False) -> TickScheduler:
    settings = SimpleNamespace(
        calendar_timezone="America/Los_Angeles",
        accelerated_mode=accelerated,
        tick_interval_seconds=5,
        command_prefix="$",
    )
    return TickScheduler(SimpleNamespace(db=db, settings=settings))


async def _bootstrap_in_the_future(db, *, today: dt.date, ahead: dt.date) -> None:
    """Bootstrap the guild, put a worker on a state payroll and clock them in,
    then leave the cursor parked in the future the way a playtest would."""
    async with db.session() as session:
        state = await economy.ensure_bootstrapped(session, GUILD, today)
        job = (
            await session.execute(
                select(Job)
                .join(Company, Job.company_id == Company.id)
                .where(Company.guild_id == GUILD, Company.is_state == True)  # noqa: E712
                .order_by(Job.id)
            )
        ).scalars().first()
        await employment.apply_to_job(session, state, WORKER, job.id)
        await employment.clock_in(session, state, WORKER)
        state.game_day = ahead
        state.weekday = gameday.weekday_name(ahead)


async def test_calendar_startup_snaps_a_future_cursor_back_to_today(db, caplog):
    today = dt.date(2024, 3, 11)  # a Monday
    ahead = dt.date(2024, 5, 20)  # ten weeks of accelerated playtesting later
    await _bootstrap_in_the_future(db, today=today, ahead=ahead)

    scheduler = _scheduler(db)
    with caplog.at_level(logging.WARNING, logger="tendies.scheduler"):
        reports = await scheduler.synchronize_guild(GUILD, target_day=today)

    # Nothing was settled.
    assert reports == []

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a future calendar cursor must warn"
    message = warnings[-1].getMessage()
    assert str(ahead) in message and str(today) in message

    async with db.session() as session:
        state = await session.get(ServerState, GUILD)
        assert state.game_day == today
        assert state.weekday == gameday.weekday_name(today)

        # No payouts, and the clocked-in shift was neither paid nor invented.
        paid = await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.guild_id == GUILD, Transaction.type == "state_wage")
        )
        assert paid == 0
        emp = (
            await session.execute(
                select(Employment)
                .join(Company, Employment.company_id == Company.id)
                .where(Company.guild_id == GUILD, Employment.user_id == WORKER)
            )
        ).scalars().first()
        assert emp is not None and emp.clocked_in is True
        assert await money.money_supply(session, GUILD) == STARTING_POOL


async def test_after_the_snap_back_days_close_normally_again(db):
    today = dt.date(2024, 3, 11)
    ahead = dt.date(2024, 5, 20)
    await _bootstrap_in_the_future(db, today=today, ahead=ahead)

    scheduler = _scheduler(db)
    assert await scheduler.synchronize_guild(GUILD, target_day=today) == []

    # The next real day settles Monday and opens Tuesday — the economy lives.
    tuesday = today + dt.timedelta(days=1)
    reports = await scheduler.synchronize_guild(GUILD, target_day=tuesday)
    assert [r.game_day for r in reports] == [today]

    async with db.session() as session:
        state = await session.get(ServerState, GUILD)
        assert state.game_day == tuesday
        paid = await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.guild_id == GUILD, Transaction.type == "state_wage")
        )
        assert paid == 1


async def test_accelerated_mode_leaves_a_future_cursor_alone(db):
    """In accelerated mode the wall clock is not the authority; don't rewind."""
    today = dt.date(2024, 3, 11)
    ahead = dt.date(2024, 5, 20)
    await _bootstrap_in_the_future(db, today=today, ahead=ahead)

    scheduler = _scheduler(db, accelerated=True)
    assert await scheduler.synchronize_guild(GUILD, target_day=today) == []

    async with db.session() as session:
        state = await session.get(ServerState, GUILD)
        assert state.game_day == ahead


async def test_snap_back_refuses_to_rewind_onto_an_already_settled_date(db, caplog):
    """A westward ``GAME_TIMEZONE`` change can put local "today" *behind* dates
    this guild already closed. Snapping back then would re-close a settled date,
    double-advancing vesting and overwriting its immutable market closes."""
    monday = dt.date(2024, 3, 11)
    tuesday = dt.date(2024, 3, 12)
    wednesday = dt.date(2024, 3, 13)
    await _bootstrap_in_the_future(db, today=monday, ahead=monday)
    # A private company, so closing a date leaves the market_closes rows that
    # mark it settled (state companies are never quoted).
    async with db.session() as session:
        state = await session.get(ServerState, GUILD)
        user = await money.get_or_create_user(session, GUILD, WORKER)
        state.pool_balance -= 1_000_000
        user.wallet += 1_000_000
        await companies.found_company(session, state, WORKER, "SNAP", "Snap Co", "tech")

    scheduler = _scheduler(db)
    # Settle Monday and Tuesday normally; the cursor opens Wednesday.
    assert [r.game_day for r in await scheduler.synchronize_guild(
        GUILD, target_day=wednesday
    )] == [monday, tuesday]

    # Now the host moves west: local "today" reads as Monday again.
    with caplog.at_level(logging.WARNING, logger="tendies.scheduler"):
        assert await scheduler.synchronize_guild(GUILD, target_day=monday) == []

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the refused snap-back must warn"
    message = warnings[-1].getMessage()
    assert "already settled" in message
    assert str(tuesday) in message and str(monday) in message

    async with db.session() as session:
        state = await session.get(ServerState, GUILD)
        assert state.game_day == wednesday, "the cursor must not rewind"
        # Tuesday was settled exactly once.
        settled = await session.scalar(
            select(func.count())
            .select_from(MarketClose)
            .join(Company, Company.id == MarketClose.company_id)
            .where(Company.guild_id == GUILD, MarketClose.game_day == tuesday)
        )
        assert settled >= 1
