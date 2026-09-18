"""Market & info views (§2, §4, §13).

This slice reads the engine directly — there is no service file. Four commands:

* ``$market`` / ``$stocks`` — the valuation table (prices, today's move,
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

from .. import discordutil, emojis, events, formatting, gameday, lookups, money, valuation


def _week_monday(day: dt.date) -> dt.date:
    """The Monday of the business week containing ``day``."""
    return day - dt.timedelta(days=day.weekday())


class Market(commands.Cog):
    """Read-only market and info commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ market
    @commands.command(name="market", aliases=["stocks"])
    async def market(self, ctx: commands.Context, page: int = 1) -> None:
        """Company valuations — share prices, today's move, sentiment (§13)."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            rows = await valuation.market_table(session, state)
            rounds = await self._open_rounds(session, state.guild_id)
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
                        f"{emojis.industry(ind)} {ind.capitalize()} ×{mult:g}"
                        for ind, mult in active
                    ) + " today)"

        header = f"{emojis.STOCK_UP} Company valuations — {weekday}{note}"

        if not open_market:
            body = (
                "market CLOSED — prices frozen until Monday\n\n"
            )
        else:
            body = ""

        if not rows:
            desc = (
                f"{body}No private companies yet. "
                f"Found one with `{ctx.prefix}found`."
            )
            await ctx.send(embed=discordutil.embed(header, desc))
            return

        pages = max(1, (len(rows) + 19) // 20)
        page = max(1, min(page, pages))
        rows = rows[(page - 1) * 20:page * 20]
        body += f"Page {page}/{pages} · use `{ctx.prefix}market <page>`\n"
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
        emb = discordutil.embed(header, body + table)
        if rounds:
            emb.add_field(
                name=f"{emojis.NUGGIE} Open rounds",
                value="\n".join(
                    f"**{r['ticker']}** — raising {formatting.fmt(r['amount'])} nug "
                    f"for {r['equity_pct']:g}% · "
                    f"{formatting.fmt(r['remaining'])} nug left · "
                    f"`{ctx.prefix}invest {r['ticker']} <amount>`"
                    for r in rounds[:10]
                ),
                inline=False,
            )
        await ctx.send(embed=emb)

    async def _open_rounds(self, session, guild_id: int) -> list[dict]:
        """Companies currently raising: ticker, target, equity %, remaining.

        Read off the models directly — the investment service has no
        "list open rounds" entry point and its signatures are owned elsewhere.
        """
        from sqlalchemy import select

        from ..models import Company, FundingRound

        rows = (
            await session.execute(
                select(FundingRound, Company)
                .join(Company, FundingRound.company_id == Company.id)
                .where(
                    Company.guild_id == guild_id,
                    Company.active == True,  # noqa: E712
                    FundingRound.status == "open",
                )
                .order_by(Company.ticker.asc())
            )
        ).all()
        return [
            {
                "ticker": company.ticker,
                "amount": int(rnd.amount),
                "equity_pct": float(rnd.equity_pct),
                "remaining": max(0, int(rnd.amount) - int(rnd.amount_raised)),
            }
            for rnd, company in rows
        ]

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
                    f"{emojis.LEADERBOARD} Richest tycoons (real net worth)",
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
                f"{emojis.LEADERBOARD} Richest tycoons (real net worth)", "\n".join(lines)
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
        await ctx.send(embed=discordutil.embed(f"{emojis.TREASURY_POOL} Server Treasury", desc))

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

        if getattr(self.bot.settings, "accelerated_mode", False):
            lines.append("Accelerated development calendar.")
        else:
            zone = gameday.calendar_timezone(getattr(self.bot.settings, "calendar_timezone", None))
            close = dt.datetime.combine(state.game_day + dt.timedelta(days=1), dt.time(), tzinfo=zone)
            lines.append(f"Date: **{state.game_day.isoformat()}** · server timezone **{zone.key}**")
            lines.append(f"Next day boundary: <t:{int(close.timestamp())}:F> (<t:{int(close.timestamp())}:R>).")
        await ctx.send(embed=discordutil.embed("📅 Game Calendar", "\n".join(lines)))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Market(bot))
