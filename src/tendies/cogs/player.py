"""Player-lifecycle commands (§6, §9 clock-in, §10): the path from broke to
employed to founder. Thin wrappers over :mod:`tendies.services.employment` —
parse args, open a session, call the service, render an embed/string.

Commands:
    $balance / $bal   — wallet (real), employment, net worth, holdings.
    $jobs [page]      — paginated list of open positions.
    $apply <job_id>   — apply (state jobs auto-accept).
    $clockin          — collect today's wage + contribute production.
    $clockout         — stop contributing for the day.
    $quit [ticker]    — leave a job, keeping vested equity (confirm first).
"""

from __future__ import annotations

import datetime as dt

import discord
from discord.ext import commands
from sqlalchemy import func, select

from .. import discordutil, emojis, formatting, gameday, lookups
from ..discordutil import mention
from ..models import Application, Company, Job, PlayerProfile, Transaction
from ..services import employment

#: Open-jobs page size for ``$jobs``.
JOBS_PER_PAGE = 8

#: Ledger types that count as "what you took home at the last close".
_EARNING_TYPES = ("wage", "state_wage", "streak_bonus")


class PlayerCog(commands.Cog, name="Player"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -------------------------------------------------------------------
    # $balance / $bal
    # -------------------------------------------------------------------
    @commands.command(name="balance", aliases=["bal"])
    async def balance(self, ctx: commands.Context) -> None:
        """Your wallet (real), job, net worth, and holdings."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            info = await employment.balance(session, state, ctx.author.id)
            job_line, clock_line = await self._work_lines(
                session, state, ctx.author.id, ctx.prefix
            )
            streak = await self._streak(session, state.guild_id, ctx.author.id)
            earned_day, earned_gross = await self._last_close_earnings(
                session, state, ctx.author.id
            )

        lines = [
            f"**{formatting.fmt(info.wallet_real)} nug** (real)",
            info.employment_label,
        ]
        if job_line:
            lines.append(job_line)
        if clock_line:
            lines.append(clock_line)
        lines.append(
            f"🔥 Clock-in streak: **{streak}** business "
            f"{'day' if streak == 1 else 'days'}"
        )
        if earned_day is not None:
            lines.append(
                f"Earned at last close ({earned_day.strftime('%a')}): "
                f"**+{formatting.fmt(earned_gross)} nug** (gross, before tax)"
            )
        lines.append(f"Next close: {self._next_close(state)}")
        lines.append(f"Net worth: **{formatting.abbr(info.net_worth)} nug** (real)")
        if info.holdings:
            lines.append("")
            lines.append("**Holdings**")
            for h in info.holdings:
                lines.append(
                    f"  {h.ticker} — {formatting.fmt(h.shares)} sh "
                    f"— {formatting.abbr(h.value)} nug (real)"
                )
        else:
            lines.append("No holdings.")

        emb = discordutil.embed(
            f"{emojis.NUGGIE} {ctx.author.display_name}",
            "\n".join(lines),
        )
        await ctx.send(embed=emb)

    # -- onboarding data the balance card needs -------------------------
    #
    # ``employment.balance`` returns a rendered label only, and its signature is
    # owned elsewhere, so these read the models directly through the same
    # session.

    async def _work_lines(
        self, session, state, user_id: int, prefix: str
    ) -> tuple[str, str]:
        """(wage line, clock-in line) for the caller — the two facts a new
        player needs and `$balance` never told them."""
        emp = await lookups.get_employment(session, state.guild_id, user_id)
        if emp is None:
            return "", f"Not employed yet — `{prefix}jobs` lists everyone hiring."
        wage_line = f"Daily wage: **{formatting.fmt(emp.daily_wage)} nug/day**"
        business_day = gameday.is_business_day(state.game_day)
        if emp.clocked_in:
            clock_line = f"{emojis.CLOCK_IN} Clocked in today — you'll be paid at the close."
        elif not business_day:
            clock_line = "🌙 Weekend — no clock-in, no wage today."
        else:
            clock_line = "⏰ **Not clocked in today** — no wage at the close."
        return wage_line, clock_line

    async def _streak(self, session, guild_id: int, user_id: int) -> int:
        profile = await session.get(PlayerProfile, (guild_id, user_id))
        return int(profile.clockin_streak) if profile else 0

    async def _last_close_earnings(self, session, state, user_id: int):
        """(game day, gross nuggies) of the caller's most recent *settled* close.

        Restricted to days strictly before the open cursor: ``streak_bonus`` is
        credited at clock-in on the still-open current date, and reporting that
        as "earned at last close" would announce a day that hasn't closed yet.
        Once the date does close, its bonus counts — it is real income paid on a
        settled day — which is why the type stays in :data:`_EARNING_TYPES`.

        Cheap: one max() and one sum() over the already-indexed ledger.
        Returns ``(None, 0)`` before their first payday.
        """
        guild_id = state.guild_id
        day = await session.scalar(
            select(func.max(Transaction.game_day)).where(
                Transaction.guild_id == guild_id,
                Transaction.user_id == user_id,
                Transaction.type.in_(_EARNING_TYPES),
                Transaction.game_day < state.game_day,
            )
        )
        if day is None:
            return None, 0
        total = await session.scalar(
            select(func.sum(Transaction.amount)).where(
                Transaction.guild_id == guild_id,
                Transaction.user_id == user_id,
                Transaction.type.in_(_EARNING_TYPES),
                Transaction.game_day == day,
            )
        )
        return day, int(total or 0)

    def _next_close(self, state) -> str:
        """The next daily close as a Discord relative timestamp (same pattern
        as ``$today``): midnight ending the current game day, in the economy's
        calendar timezone."""
        zone = gameday.calendar_timezone(
            getattr(self.bot.settings, "calendar_timezone", None)
        )
        close = dt.datetime.combine(
            state.game_day + dt.timedelta(days=1), dt.time(), tzinfo=zone
        )
        return f"<t:{int(close.timestamp())}:R> (<t:{int(close.timestamp())}:F>)"

    # -------------------------------------------------------------------
    # $jobs [page]
    # -------------------------------------------------------------------
    @commands.command(name="jobs")
    async def jobs(self, ctx: commands.Context, page: int = 1) -> None:
        """List open positions across the server, including state jobs."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            listings = await employment.list_open_jobs(session, state)
            applied = await self._pending_job_ids(session, ctx.author.id)

        if not listings:
            await ctx.send(
                embed=discordutil.embed(
                    f"{emojis.HIRING} Open positions",
                    "No open positions right now. Found a company with `$found` "
                    "to make some.",
                )
            )
            return

        total_pages = (len(listings) + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE
        page = max(1, min(page, total_pages))
        start = (page - 1) * JOBS_PER_PAGE
        chunk = listings[start : start + JOBS_PER_PAGE]

        lines = []
        for job in chunk:
            prefix = "[STATE] " if job.is_state else ""
            equity = ""
            if job.equity_shares and job.vest_days:
                equity = (
                    f" + {formatting.fmt(job.equity_shares)} sh "
                    f"vesting/{job.vest_days}d"
                )
            marker = " **(applied)**" if job.job_id in applied else ""
            lines.append(
                f"{prefix}{job.company_name} ({job.ticker}) — {job.title} — "
                f"{formatting.fmt(job.daily_wage)} nug/day{equity} "
                f"— `$apply {job.job_id}`{marker}"
            )

        emb = discordutil.embed(
            f"{emojis.HIRING} Open positions (page {page}/{total_pages})",
            "\n".join(lines),
        )
        await ctx.send(embed=emb)

    async def _pending_job_ids(self, session, user_id: int) -> set[int]:
        """Job ids this player already has a pending application for, so the
        list can say so instead of letting them re-apply into an error."""
        rows = (
            await session.execute(
                select(Application.job_id).where(
                    Application.user_id == user_id,
                    Application.status == "pending",
                )
            )
        ).scalars().all()
        return {int(j) for j in rows}

    # -------------------------------------------------------------------
    # $apply <job_id>
    # -------------------------------------------------------------------
    @commands.command(name="apply")
    async def apply(self, ctx: commands.Context, job_id: int) -> None:
        """Apply to a job. State jobs auto-accept; private jobs queue you."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await employment.apply_to_job(
                session, state, ctx.author.id, job_id
            )
            owner_id = await self._job_owner_id(session, job_id)

        if result.auto_accepted:
            await ctx.send(
                embed=discordutil.embed(
                    f"{emojis.HIRING} Hired at {result.company_name}",
                    f"You're now **{result.title}** at **{result.company_name}** "
                    f"({result.ticker}) — state job, auto-accepted.\n"
                    f"Wage **{formatting.fmt(result.daily_wage)} nug/day**. "
                    f"`$clockin` on weekdays to get paid.",
                )
            )
        else:
            # Nothing else tells the owner an application is waiting, so ping
            # them here — the one send that deliberately opts back in to
            # mentions (the bot suppresses them globally).
            owner_tag = mention(owner_id) if owner_id else "The owner"
            await ctx.send(
                f"{owner_tag} — new applicant for **{result.title}** at "
                f"**{result.ticker}**. Review with `{ctx.prefix}applicants "
                f"{result.ticker}`.",
                embed=discordutil.embed(
                    f"{emojis.HIRING} Application filed",
                    f"Applied to **{result.company_name}** ({result.ticker}) — "
                    f"**{result.title}** ({formatting.fmt(result.daily_wage)} "
                    f"nug/day).\nThe owner will review it with "
                    f"`$applicants {result.ticker}`.",
                ),
                allowed_mentions=(
                    discord.AllowedMentions(users=[discord.Object(id=owner_id)])
                    if owner_id
                    else discord.AllowedMentions.none()
                ),
            )

    async def _job_owner_id(self, session, job_id: int) -> int | None:
        """The owner of the company posting ``job_id`` (``None`` for state jobs)."""
        return await session.scalar(
            select(Company.owner_id)
            .join(Job, Job.company_id == Company.id)
            .where(Job.id == job_id)
        )

    # -------------------------------------------------------------------
    # $clockin
    # -------------------------------------------------------------------
    @commands.command(name="clockin")
    async def clockin(self, ctx: commands.Context) -> None:
        """Collect today's wage and contribute to your employer's production."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await employment.clock_in(session, state, ctx.author.id)

        if result.already:
            streak_line = (
                f"\n🔥 **{result.streak}-day streak** going."
                if result.streak > 1
                else ""
            )
            await ctx.send(
                embed=discordutil.embed(
                    f"{emojis.CLOCK_IN} Already clocked in",
                    f"You're already clocked in at **{result.company_name}** today. "
                    f"You'll be paid {formatting.fmt(result.daily_wage)} nug at the "
                    f"tick.{streak_line}",
                )
            )
        else:
            lines = [
                f"You'll be paid **{formatting.fmt(result.daily_wage)} nug** at "
                f"today's tick and you're contributing to "
                f"**{result.company_name}**'s production. See you at close."
            ]
            if result.streak > 1:
                lines.append(f"🔥 **{result.streak}-day streak** — keep it going.")
            else:
                lines.append("🔥 Day **1** of a new streak.")
            if result.bonus_milestone:
                net = result.bonus_gross - result.bonus_tax
                lines.append(
                    f"🎉 **{result.bonus_milestone}-day milestone!** Loyalty bonus "
                    f"**+{formatting.fmt(net)} nug** (after tax), straight from the pool."
                )
            await ctx.send(
                embed=discordutil.embed(
                    f"{emojis.CLOCK_IN} Clocked in at {result.company_name}",
                    "\n".join(lines),
                )
            )

        # First-ever clock-in: ask whether we may ping them if they forget.
        if result.ask_reminder:
            opted = await discordutil.confirm(
                ctx,
                "⏰ Want me to **ping you if you forget to clock in** on a future "
                "business day? React ✅ within 60s.",
                timeout=60,
            )
            async with self.bot.db.session() as session:
                state = await lookups.get_state(session, ctx.guild.id)
                await employment.set_reminder_opt_in(
                    session, state, ctx.author.id, opted
                )
            await ctx.send(
                f"👍 You're in — I'll ping you if you forget to clock in. "
                f"Turn it off anytime with `{ctx.prefix}reminders off`."
                if opted
                else f"No pings from me. Enable them later with `{ctx.prefix}reminders on`."
            )

    # -------------------------------------------------------------------
    # $reminders <on|off>
    # -------------------------------------------------------------------
    @commands.command(name="reminders")
    async def reminders(self, ctx: commands.Context, setting: str = "") -> None:
        """Toggle clock-in reminder pings: `$reminders on` / `$reminders off`."""
        choice = setting.strip().lower()
        if choice in ("on", "yes", "enable", "true"):
            opt_in = True
        elif choice in ("off", "no", "disable", "false"):
            opt_in = False
        else:
            await ctx.send(
                f"Use `{ctx.prefix}reminders on` or `{ctx.prefix}reminders off` "
                f"to control whether I ping you when you forget to clock in."
            )
            return
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            await employment.set_reminder_opt_in(session, state, ctx.author.id, opt_in)
        await ctx.send(
            f"{emojis.CLOCK_IN} Clock-in reminders are now **{'ON' if opt_in else 'OFF'}**."
        )

    # -------------------------------------------------------------------
    # $clockout
    # -------------------------------------------------------------------
    @commands.command(name="clockout")
    async def clockout(self, ctx: commands.Context) -> None:
        """Stop contributing for the day (you're auto-clocked-out nightly anyway)."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            company_name = await employment.clock_out(session, state, ctx.author.id)

        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.CLOCK_IN} Clocked out",
                f"You've clocked out of **{company_name}** for today. No production, "
                f"no wage at the next tick.",
            )
        )

    # -------------------------------------------------------------------
    # $quit [ticker]
    # -------------------------------------------------------------------
    @commands.command(name="quit")
    async def quit(self, ctx: commands.Context, ticker: str | None = None) -> None:
        """Leave a job. Keep vested equity; forfeit the rest. Confirms first."""
        # Preview in one session (read-only).
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            summary = await employment.employment_summary(
                session, state, ctx.author.id, ticker
            )

        if summary.total_grant > 0:
            prompt = (
                f"You've vested **{formatting.fmt(summary.vested)} / "
                f"{formatting.fmt(summary.total_grant)} sh** at "
                f"**{summary.ticker}** ({summary.days_in} business days in).\n"
                f"Quitting keeps the {formatting.fmt(summary.vested)} vested and "
                f"forfeits {formatting.fmt(summary.unvested)}. React ✅ to confirm."
            )
        else:
            prompt = (
                f"Leave **{summary.title} @ {summary.company_name}** "
                f"({summary.ticker})? You have no equity grant here, so nothing is "
                f"forfeited. Your wage stops. React ✅ to confirm."
            )

        if not await discordutil.confirm(ctx, prompt):
            await ctx.send("Cancelled — you're still employed.")
            return

        # On confirm, open a NEW session and perform the quit.
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await employment.quit_job(
                session, state, ctx.author.id, ticker
            )

        if result.forfeited > 0:
            tail = (
                f"\n**{formatting.fmt(result.forfeited)} {result.ticker}** unvested "
                f"shares forfeited."
            )
        else:
            tail = ""
        if result.vested > 0:
            kept = (
                f" **{formatting.fmt(result.vested)} {result.ticker}** shares are "
                f"yours."
            )
        else:
            kept = ""

        await ctx.send(
            embed=discordutil.embed(
                f"👋 You left {result.company_name}",
                f"{mention(ctx.author.id)} left **{result.company_name}** "
                f"({result.ticker}). Wage stopped.{kept}{tail}",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayerCog(bot))
