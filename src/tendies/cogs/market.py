"""Market & info views (§2, §4, §13).

This slice reads the engine directly — there is no service file. Four commands:

* ``$market`` / ``$stocks`` — the stock exchange table (prices, today's move,
  industry sentiment). Frozen on weekends.
* ``$leaderboard`` / ``$rich`` — players ranked by real net worth.
* ``$pool`` — the server treasury / money-supply snapshot.
* ``$today`` — the game calendar (weekday + market open/closed + event hints).

Cogs stay thin: open a session, fetch state via :func:`lookups.get_state`, read
the engine (valuation/events/money), and render with an embed or code block.
``GameError`` raised by the engine propagates to the bot's error handler.
"""

from __future__ import annotations

import datetime as dt

from discord.ext import commands

from .. import discordutil, events, formatting, gameday, lookups, money, valuation


def _week_monday(day: dt.date) -> dt.date:
    """The Monday of the business week containing ``day``."""
    return day - dt.timedelta(days=day.weekday())


class Market(commands.Cog):
    """Read-only market and info commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ market
    @commands.command(name="market", aliases=["stocks"])
    async def market(self, ctx: commands.Context) -> None:
        """The Nuggie Exchange — share prices, today's move, sentiment (§13)."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            rows = await valuation.market_table(session, state)
            open_market = gameday.is_business_day(state.game_day)
            weekday = gameday.weekday_name(state.game_day).capitalize()

            note = ""
            if open_market:
                movers = await events.active_multipliers(
                    session, state.guild_id, state.game_day
                )
                active = [
                    (ind, mult) for ind, mult in movers.items() if mult != 1.0
                ]
                if active:
                    active.sort(key=lambda x: x[1], reverse=True)
                    note = "  (" + ", ".join(
                        f"{ind.capitalize()} ×{mult:g}" for ind, mult in active
                    ) + " today)"

        header = f"📊 The Nuggie Exchange — {weekday}{note}"

        if not open_market:
            body = (
                "market CLOSED — prices frozen until Monday\n\n"
            )
        else:
            body = ""

        if not rows:
            desc = (
                f"{body}No companies are trading yet. "
                f"Found one with `{ctx.prefix}found`."
            )
            await ctx.send(embed=discordutil.embed(header, desc))
            return

        # Build a monospace table for alignment.
        lines = ["TICKER  PRICE(real)        Δ today     INDUSTRY"]
        for v in rows:
            ticker = f"{v.ticker:<6}"
            price = f"{formatting.fmt(v.share_price)} nug"
            price_col = f"{price:>16}"
            if v.frozen:
                delta_col = "—  frozen"
            elif v.delta_today > 0:
                delta_col = f"▲ {formatting.pct(v.delta_today)}"
            elif v.delta_today < 0:
                delta_col = f"▼ {formatting.pct(v.delta_today)}"
            else:
                delta_col = "—  0.0%"
            delta_col = f"{delta_col:<11}"
            lines.append(
                f"{ticker}  {price_col}  {delta_col} {v.industry.capitalize()}"
            )

        table = "```\n" + "\n".join(lines) + "\n```"
        await ctx.send(embed=discordutil.embed(header, body + table))

    # ------------------------------------------------------------ leaderboard
    @commands.command(name="leaderboard", aliases=["rich"])
    async def leaderboard(self, ctx: commands.Context) -> None:
        """Players ranked by real net worth, with their holdings (§13)."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            entries = await valuation.leaderboard(session, state, limit=10)

        if not entries:
            await ctx.send(
                embed=discordutil.embed(
                    "🏆 Richest tycoons (real net worth)",
                    "Nobody's on the board yet. Clock in and start stacking nuggies.",
                )
            )
            return

        lines = []
        for e in entries:
            who = discordutil.mention(e.user_id)
            worth = formatting.abbr(e.net_worth)
            if e.company_labels:
                holds = " · ".join(e.company_labels)
            else:
                holds = "(employee, no companies)"
            lines.append(f"{e.rank}. {who} — {worth} nug — {holds}")

        await ctx.send(
            embed=discordutil.embed(
                "🏆 Richest tycoons (real net worth)", "\n".join(lines)
            )
        )

    # ------------------------------------------------------------------- pool
    @commands.command(name="pool")
    async def pool(self, ctx: commands.Context) -> None:
        """The server treasury and money-supply snapshot (§2)."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            supply = await money.money_supply(session, state.guild_id)
            pool_nominal = state.pool_balance
            index = state.inflation_index
            tax_rate = state.tax_rate

        if index != 1.0:
            pool_line = (
                f"Pool: {formatting.fmt(pool_nominal)} nug "
                f"({formatting.fmt_real(pool_nominal, index)} real)"
            )
            index_line = (
                f"Inflation index: {index:.3f}  "
                f"(everything shown in real terms is ÷{index:.3f})"
            )
        else:
            pool_line = f"Pool: {formatting.fmt(pool_nominal)} nug"
            index_line = (
                f"Inflation index: {index:.3f}  "
                "(everything shown in real terms is ÷index)"
            )

        desc = "\n".join(
            [
                pool_line,
                index_line,
                f"Tax rate: {tax_rate * 100:.0f}%",
                "Money supply (pool + all wallets + all treasuries): "
                f"{formatting.fmt(supply)} nug",
            ]
        )
        await ctx.send(embed=discordutil.embed("🍗 Server Treasury", desc))

    # ------------------------------------------------------------------ today
    @commands.command(name="today")
    async def today(self, ctx: commands.Context) -> None:
        """The game calendar — weekday, market hours, event hints (§4)."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            weekday = gameday.weekday_name(state.game_day).capitalize()
            open_market = gameday.is_business_day(state.game_day)
            week_events = await events.events_for_week(
                session, state.guild_id, _week_monday(state.game_day)
            )

        if open_market:
            lines = [f"📅 {weekday} — market **OPEN**."]
            upcoming = [
                ev
                for ev in week_events
                if ev.game_day >= state.game_day
            ]
            if upcoming:
                movers = ", ".join(
                    f"{gameday.weekday_name(ev.game_day).capitalize()} "
                    f"{ev.blurb} (×{ev.multiplier:g})"
                    for ev in upcoming
                )
                lines.append(f"This week's movers: {movers}")
            else:
                lines.append(
                    f"No events left this week. See `{ctx.prefix}market` for today's prices."
                )
        else:
            lines = [
                f"📅 {weekday} — market **CLOSED**. Prices frozen until Monday.",
                "You can still found companies, post jobs, hire, and send "
                "acquisition offers.",
            ]

        await ctx.send(embed=discordutil.embed("📅 Game Calendar", "\n".join(lines)))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Market(bot))
