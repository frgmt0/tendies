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

from discord.ext import commands

from .. import discordutil, formatting, lookups
from ..discordutil import mention
from ..services import employment

#: Open-jobs page size for ``$jobs``.
JOBS_PER_PAGE = 8


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

        lines = [
            f"**{formatting.fmt(info.wallet_real)} nug** (real)",
            info.employment_label,
            f"Net worth: **{formatting.abbr(info.net_worth)} nug** (real)",
        ]
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
            f"💰 {ctx.author.display_name}",
            "\n".join(lines),
        )
        await ctx.send(embed=emb)

    # -------------------------------------------------------------------
    # $jobs [page]
    # -------------------------------------------------------------------
    @commands.command(name="jobs")
    async def jobs(self, ctx: commands.Context, page: int = 1) -> None:
        """List open positions across the server, including state jobs."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            listings = await employment.list_open_jobs(session, state)

        if not listings:
            await ctx.send(
                embed=discordutil.embed(
                    "🍗 Open positions",
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
            lines.append(
                f"{prefix}{job.company_name} ({job.ticker}) — {job.title} — "
                f"{formatting.fmt(job.daily_wage)} nug/day{equity} "
                f"— `$apply {job.job_id}`"
            )

        emb = discordutil.embed(
            f"🍗 Open positions (page {page}/{total_pages})",
            "\n".join(lines),
        )
        await ctx.send(embed=emb)

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

        if result.auto_accepted:
            await ctx.send(
                embed=discordutil.embed(
                    f"✅ Hired at {result.company_name}",
                    f"You're now **{result.title}** at **{result.company_name}** "
                    f"({result.ticker}) — state job, auto-accepted.\n"
                    f"Wage **{formatting.fmt(result.daily_wage)} nug/day**. "
                    f"`$clockin` on weekdays to get paid.",
                )
            )
        else:
            await ctx.send(
                embed=discordutil.embed(
                    "📨 Application filed",
                    f"Applied to **{result.company_name}** ({result.ticker}) — "
                    f"**{result.title}** ({formatting.fmt(result.daily_wage)} "
                    f"nug/day).\nThe owner will review it with "
                    f"`$applicants {result.ticker}`.",
                )
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
            await ctx.send(
                embed=discordutil.embed(
                    "⏰ Already clocked in",
                    f"You're already clocked in at **{result.company_name}** today. "
                    f"You'll be paid {formatting.fmt(result.daily_wage)} nug at the "
                    f"tick.",
                )
            )
            return

        await ctx.send(
            embed=discordutil.embed(
                f"⏰ Clocked in at {result.company_name}",
                f"You'll be paid **{formatting.fmt(result.daily_wage)} nug** at "
                f"today's tick and you're contributing to "
                f"**{result.company_name}**'s production. See you at close.",
            )
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
                "🕔 Clocked out",
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
