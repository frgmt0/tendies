"""Wall-clock calendar settlement, recovery, and DST behavior."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from tendies import events, gameday, tick
from tendies.config import Settings
from tendies.models import Company, Event, Job, ServerState, Transaction, User
from tendies.scheduler import TickScheduler, midnight_trigger
from tendies.services import companies, economy, employment

from cog_harness import Harness


def test_calendar_mode_is_default_and_acceleration_is_explicit(monkeypatch):
    monkeypatch.delenv("GAME_TIME_MODE", raising=False)
    monkeypatch.delenv("GAME_TIMEZONE", raising=False)
    settings = Settings.from_env()
    assert settings.accelerated_mode is False
    assert settings.calendar_timezone is None
    assert isinstance(gameday.calendar_timezone(), ZoneInfo)

    monkeypatch.setenv("GAME_TIME_MODE", "accelerated")
    monkeypatch.setenv("GAME_TIMEZONE", "America/New_York")
    settings = Settings.from_env()
    assert settings.accelerated_mode is True
    assert settings.calendar_timezone == "America/New_York"


async def test_manual_calendar_commands_are_disabled_in_calendar_mode():
    harness = await Harness.create()
    try:
        harness.bot.settings.accelerated_mode = False
        forced = await harness.invoke(
            harness.ctx(1, manager=True), "forcetick"
        )
        changed = await harness.invoke(
            harness.ctx(1, manager=True), "setday", "friday"
        )
        assert "accelerated/testing mode" in forced.last_text()
        assert "accelerated/testing mode" in changed.last_text()
    finally:
        await harness.close()


async def test_tick_settles_event_on_the_visible_current_day(world):
    owner = 8801
    worker = 8802
    await world.make_rich(owner, 1_000_000)
    company = (await companies.found_company(
        world.session, world.state, owner, "NOW", "Current Day", "tech"
    )).company
    job = await companies.post_job(
        world.session, world.state, owner, "NOW", "Builder", "", 1, 0, 0
    )
    await employment.apply_to_job(world.session, world.state, worker, job.id)
    applicants = await companies.list_applicants(
        world.session, world.state, owner, "NOW"
    )
    await companies.hire(
        world.session, world.state, owner, "NOW", applicants[0].application_id
    )
    world.session.add(Event(
        guild_id=world.guild_id,
        game_day=world.state.game_day,
        industry="tech",
        multiplier=2.0,
        source="admin",
        blurb="today means today",
    ))
    await employment.clock_in(world.session, world.state, worker)
    await world.session.flush()

    visible_day = world.state.game_day
    report = await tick.run_tick(world.session, world.state)
    result = next(item for item in report.companies if item.company_id == company.id)

    assert report.game_day == visible_day
    assert report.todays_events == ["today means today"]
    assert result.tentative_revenue == 24_000
    assert world.state.game_day == visible_day + dt.timedelta(days=1)


async def test_restart_catchup_pays_recorded_friday_once_and_skips_weekend(db):
    guild_id = 9911
    worker = 9912
    friday = dt.date(2024, 3, 8)
    monday = dt.date(2024, 3, 11)

    async with db.session() as session:
        state = await economy.ensure_bootstrapped(session, guild_id, friday)
        job = (await session.execute(
            select(Job)
            .join(Company, Job.company_id == Company.id)
            .where(Company.guild_id == guild_id, Company.is_state == True)  # noqa: E712
            .order_by(Job.id)
        )).scalars().first()
        assert job is not None
        await employment.apply_to_job(session, state, worker, job.id)
        await employment.clock_in(session, state, worker)

    settings = SimpleNamespace(
        calendar_timezone="America/Los_Angeles",
        accelerated_mode=False,
        tick_interval_seconds=5,
        command_prefix="$",
    )
    bot = SimpleNamespace(db=db, settings=settings)
    scheduler = TickScheduler(bot)

    first = await scheduler.synchronize_guild(guild_id, target_day=monday)
    second = await scheduler.synchronize_guild(guild_id, target_day=monday)

    assert [report.game_day for report in first] == [
        dt.date(2024, 3, 8),
        dt.date(2024, 3, 9),
        dt.date(2024, 3, 10),
    ]
    assert [report.closed for report in first] == [False, True, True]
    assert second == []

    async with db.session() as session:
        state = await session.get(ServerState, guild_id)
        user = await session.get(User, (guild_id, worker))
        paid = await session.scalar(select(func.count()).select_from(Transaction).where(
            Transaction.guild_id == guild_id,
            Transaction.user_id == worker,
            Transaction.type == "state_wage",
        ))
        assert state.game_day == monday
        assert user is not None and user.wallet > 0
        assert paid == 1


async def test_midweek_bootstrap_rolls_only_remaining_week(monkeypatch, db):
    calls = []

    async def fake_roll(session, state, monday, *, rng=None, not_before=None):
        calls.append((monday, not_before))
        return []

    monkeypatch.setattr(events, "roll_weekly_events", fake_roll)
    wednesday = dt.date(2024, 4, 3)
    async with db.session() as session:
        state = await economy.ensure_bootstrapped(session, 9921, wednesday)
        assert state.game_day == wednesday

    assert calls == [(dt.date(2024, 4, 1), wednesday)]


def _utc_gap(first: dt.datetime, second: dt.datetime) -> dt.timedelta:
    utc = ZoneInfo("UTC")
    return second.astimezone(utc) - first.astimezone(utc)


def test_midnight_trigger_tracks_spring_dst_boundary():
    zone = gameday.calendar_timezone("America/Los_Angeles")
    start = dt.datetime(2024, 3, 10, tzinfo=zone)
    trigger = midnight_trigger(zone, start_date=start.date())
    first = trigger.get_next_fire_time(None, start)
    second = trigger.get_next_fire_time(first, first)

    assert first == start
    assert second == dt.datetime(2024, 3, 11, tzinfo=zone)
    assert _utc_gap(
        first, second
    ) == dt.timedelta(hours=23)


def test_midnight_trigger_tracks_fall_dst_boundary():
    zone = gameday.calendar_timezone("America/Los_Angeles")
    start = dt.datetime(2024, 11, 3, tzinfo=zone)
    trigger = midnight_trigger(zone, start_date=start.date())
    first = trigger.get_next_fire_time(None, start)
    second = trigger.get_next_fire_time(first, first)

    assert first == start
    assert second == dt.datetime(2024, 11, 4, tzinfo=zone)
    assert _utc_gap(
        first, second
    ) == dt.timedelta(hours=25)
