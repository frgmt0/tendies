"""Tick scheduler.

Advances every guild's economy by one game day each cadence
(``TICK_INTERVAL_SECONDS``) and posts a daily-close announcement. Game time
lives in the database, so a restart resumes from the right day — the scheduler
only decides *when* to advance, never *what day it is*.
"""

from __future__ import annotations

import logging

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from . import emojis
from . import formatting as fmt
from . import gameday, tick
from .models import ServerState
from .tick import TickReport

log = logging.getLogger("tendies.scheduler")


class TickScheduler:
    def __init__(self, bot):
        self.bot = bot
        self._sched = AsyncIOScheduler()

    def start(self) -> None:
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
        self._sched.start()
        log.info("Tick scheduler started (every %ds).", interval)

    def shutdown(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    async def tick_all_guilds(self) -> None:
        """Run one game-day tick for every guild that has an economy."""
        async with self.bot.db.session() as session:
            guild_ids = (await session.execute(select(ServerState.guild_id))).scalars().all()

        for guild_id in guild_ids:
            try:
                report = await self.run_one(guild_id)
            except Exception:
                log.exception("Tick failed for guild %s", guild_id)
                continue
            # Announcing is best-effort: a failure here (e.g. no sendable
            # channel) must never starve the remaining guilds' ticks.
            if report is not None and not report.closed:
                try:
                    await self.announce(guild_id, report)
                except Exception:
                    log.exception("Announce failed for guild %s", guild_id)

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
