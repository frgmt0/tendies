"""Tick scheduler.

Settles each guild at midnight in the configured server-local IANA timezone.
The persisted ``game_day`` is the open-date cursor, so startup and periodic
catch-up can recover missed closes without paying the same date twice.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.calendarinterval import CalendarIntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from . import emojis
from . import formatting as fmt
from . import gameday, tick
from .models import ServerState
from .services import employment
from .tick import TickReport

log = logging.getLogger("tendies.scheduler")


def midnight_trigger(timezone, *, start_date: dt.date | None = None):
    """The DST-aware production close trigger (kept testable in isolation).

    APScheduler 3's ``CronTrigger`` can skip the midnight after a spring DST
    change when used with ``zoneinfo``.  ``CalendarIntervalTrigger`` advances
    by civil dates and reliably emits every local midnight.
    """
    return CalendarIntervalTrigger(
        days=1,
        hour=0,
        minute=0,
        start_date=start_date or (dt.datetime.now(timezone).date() + dt.timedelta(days=1)),
        timezone=timezone,
    )


class TickScheduler:
    def __init__(self, bot):
        self.bot = bot
        self.timezone = gameday.calendar_timezone(
            getattr(bot.settings, "calendar_timezone", None)
        )
        self.accelerated = bool(getattr(bot.settings, "accelerated_mode", False))
        self._sched = AsyncIOScheduler(timezone=self.timezone)

    def start(self) -> None:
        if self.accelerated:
            interval = max(5, int(self.bot.settings.tick_interval_seconds))
            self._sched.add_job(
                self.tick_all_guilds,
                "interval",
                seconds=interval,
                id="tendies-tick",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            self._sched.add_job(
                self.remind_all_guilds,
                "interval",
                seconds=interval,
                id="tendies-reminder",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                next_run_time=dt.datetime.now(self.timezone)
                + dt.timedelta(seconds=max(5, interval // 2)),
            )
            description = f"accelerated every {interval}s"
        else:
            # CalendarIntervalTrigger advances by local civil dates and follows
            # 23/25-hour DST days. A fixed 86,400-second interval does not.
            self._sched.add_job(
                self.tick_all_guilds,
                midnight_trigger(self.timezone),
                id="tendies-tick",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            # Retry/reconcile frequently as well. The midnight trigger is the
            # exact close, while this sweep recovers a transient midnight DB or
            # process failure without waiting for another day or user command.
            self._sched.add_job(
                self.tick_all_guilds,
                "interval",
                seconds=60,
                id="tendies-catchup",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            self._sched.add_job(
                self.remind_all_guilds,
                CronTrigger(day_of_week="mon-fri", hour=12, minute=0,
                            timezone=self.timezone),
                id="tendies-reminder",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            description = f"calendar midnight in {self.timezone.key}"
        self._sched.start()
        log.info("Tick scheduler started (%s).", description)

    def shutdown(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    async def tick_all_guilds(self) -> None:
        """Settle every guild through the current local calendar date."""
        async with self.bot.db.session() as session:
            guild_ids = (await session.execute(select(ServerState.guild_id))).scalars().all()

        for guild_id in guild_ids:
            try:
                if self.accelerated:
                    report = await self.run_one(guild_id)
                    reports = [report] if report is not None else []
                else:
                    reports = await self.synchronize_guild(guild_id)
            except Exception:
                log.exception("Tick failed for guild %s", guild_id)
                continue
            business_reports = [report for report in reports if not report.closed]
            # A long outage may reconcile many business dates. Announce only
            # the latest close so recovery cannot flood the server channel.
            if business_reports:
                report = business_reports[-1]
                try:
                    await self.announce(guild_id, report)
                except Exception:
                    log.exception("Announce failed for guild %s", guild_id)

    async def catch_up_all_guilds(self) -> None:
        """Startup recovery for every persisted guild (calendar mode only).

        Unlike the periodic sweep, startup is strict: readiness waits until all
        missed closes commit, and a database failure aborts startup visibly.
        """
        if self.accelerated:
            return
        async with self.bot.db.session() as session:
            guild_ids = (
                await session.execute(select(ServerState.guild_id))
            ).scalars().all()
        for guild_id in guild_ids:
            await self.synchronize_guild(guild_id)

    async def synchronize_guild(
        self, guild_id: int, *, target_day: dt.date | None = None
    ) -> list[TickReport]:
        """Idempotently settle missed dates until ``target_day`` is open.

        Only recorded attendance on the first missed business date can be paid;
        each settlement clocks everyone out, so downtime never fabricates work.
        """
        target = target_day or gameday.local_date(
            getattr(self.bot.settings, "calendar_timezone", None)
        )
        async with self.bot.db.session() as session:
            state = (
                await session.execute(
                    select(ServerState)
                    .where(ServerState.guild_id == guild_id)
                    .with_for_update()
                )
            ).scalars().first()
            if state is None:
                return []
            reports: list[TickReport] = []
            while state.game_day < target:
                reports.append(await tick.run_tick(session, state))
            return reports

    async def run_one(self, guild_id: int) -> TickReport | None:
        async with self.bot.db.session() as session:
            # Row-lock the guild so a manual $forcetick can't double-advance the
            # same economy concurrently with the scheduled tick (Postgres).
            state = (
                await session.execute(
                    select(ServerState)
                    .where(ServerState.guild_id == guild_id)
                    .with_for_update()
                )
            ).scalars().first()
            if state is None:
                return None
            return await tick.run_tick(session, state)

    async def announce(self, guild_id: int, report: TickReport) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = _announce_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=render_tick_report(report))
        except Exception:
            log.warning("Could not post tick announcement in guild %s", guild_id)

    async def remind_all_guilds(self) -> None:
        """Ping opted-in players who haven't clocked in yet today (business days)."""
        async with self.bot.db.session() as session:
            states = (await session.execute(select(ServerState))).scalars().all()
            due: dict[int, list[int]] = {}
            for state in states:
                try:
                    user_ids = await employment.due_for_reminder(session, state)
                except Exception:
                    log.exception("Reminder query failed for guild %s", state.guild_id)
                    continue
                if user_ids:
                    due[state.guild_id] = user_ids
                    await employment.mark_reminded(session, state, user_ids)
            # Commit the marks before sending, so a failed send can't cause a
            # re-ping next sweep (best-effort, like the tick announcement).

        for guild_id, user_ids in due.items():
            try:
                await self._ping_forgetful(guild_id, user_ids)
            except Exception:
                log.exception("Reminder ping failed for guild %s", guild_id)

    async def _ping_forgetful(self, guild_id: int, user_ids: list[int]) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = _announce_channel(guild)
        if channel is None:
            return
        prefix = self.bot.settings.command_prefix
        mentions = " ".join(f"<@{uid}>" for uid in user_ids)
        try:
            await channel.send(
                f"{emojis.CLOCK_IN} {mentions} — you haven't clocked in today! "
                f"`{prefix}clockin` before the close to get paid and keep your streak."
            )
        except Exception:
            log.warning("Could not post clock-in reminder in guild %s", guild_id)


def _announce_channel(guild: discord.Guild) -> discord.TextChannel | None:
    me = guild.me
    if me is None:  # member object not cached yet — can't evaluate permissions
        return None
    sys_ch = guild.system_channel
    if sys_ch is not None and sys_ch.permissions_for(me).send_messages:
        return sys_ch
    for channel in guild.text_channels:
        if channel.permissions_for(me).send_messages:
            return channel
    return None


def render_tick_report(report: TickReport) -> discord.Embed:
    """Format a business-day close into the daily announcement embed (§9)."""
    weekday = report.weekday.capitalize()
    embed = discord.Embed(
        title=f"🧾 Daily close — {weekday}",
        color=0xE67E22 if report.state_crisis or report.bankruptcies else 0xF1C40F,
    )

    if report.rolled_events:
        embed.add_field(
            name=f"{emojis.BREAKING_NEWS} This week's roll",
            value="\n".join(report.rolled_events),
            inline=False,
        )
    elif report.todays_events:
        embed.add_field(
            name=f"{emojis.BREAKING_NEWS} Today",
            value="\n".join(report.todays_events),
            inline=False,
        )

    if report.recession_ratio < 1.0:
        embed.add_field(
            name=f"{emojis.RECESSION} Recession",
            value=(
                f"Pool too thin for full output — every company realized "
                f"**{report.recession_ratio * 100:.0f}%** of production today."
            ),
            inline=False,
        )

    movers = [c for c in report.companies if c.workers > 0]
    movers.sort(key=lambda c: c.realized_revenue, reverse=True)
    if movers:
        lines = []
        for c in movers[:10]:
            tag = f" {emojis.STOCK_DOWN} insolvent" if c.insolvent and not c.bankrupted else ""
            tag = f" {emojis.BANKRUPTCY} BANKRUPT" if c.bankrupted else tag
            sent = f" ×{c.sentiment:g}" if abs(c.sentiment - 1.0) > 1e-9 else ""
            lines.append(
                f"{emojis.industry(c.industry)} **{c.ticker}**{sent} — "
                f"produced {fmt.abbr(c.realized_revenue)}, "
                f"paid {fmt.abbr(c.payroll_paid)} to {c.workers}{tag}"
            )
        embed.add_field(
            name=f"{emojis.FACTORY} Production & payroll",
            value="\n".join(lines),
            inline=False,
        )

    summary = (
        f"Revenue realized: **{fmt.abbr(report.total_realized_revenue)}** nug\n"
        f"Payroll paid: **{fmt.abbr(report.total_payroll + report.state_payroll_paid)}** nug\n"
        f"Tax → pool: **{fmt.abbr(report.total_tax)}** nug"
    )
    if report.vested_shares_total:
        summary += f"\nShares vested today: **{fmt.fmt(report.vested_shares_total)}**"
    embed.add_field(name="Summary", value=summary, inline=False)

    if report.bankruptcies:
        embed.add_field(
            name=f"{emojis.BANKRUPTCY} Bankruptcies",
            value=", ".join(report.bankruptcies),
            inline=False,
        )
    if report.state_crisis:
        embed.add_field(
            name=f"{emojis.TREASURY_POOL} Treasury crisis",
            value="The pool couldn't cover state payroll. Raise taxes, cut jobs, or print — your move, Managers.",
            inline=False,
        )
    return embed
