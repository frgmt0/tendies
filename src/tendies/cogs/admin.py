"""Manager-only macro controls (Slice E) — the central bank + ops desk.

Commands: ``$print``, ``$taxrate``, ``$setday``, ``$event``, ``$forcetick``.
Every command gates on the Manager role first; everything else (parsing,
confirmations, rendering) is the thin Discord edge over the
:mod:`tendies.services.economy` service. Game-rule errors raised by services
propagate to the bot's ``on_command_error`` for uniform display.
"""

from __future__ import annotations

import shlex

from discord.ext import commands

from .. import config, discordutil, emojis, events, formatting as fmt, gameday, lookups, money, tick
from ..discordutil import parse_amount
from ..errors import BadInput
from ..scheduler import render_tick_report
from ..services import economy


def _parse_percent(raw: str) -> float:
    """Parse a tax rate as ``"15"``, ``"15%"``, or ``"0.15"`` into a fraction.

    A value greater than 1 is treated as a percentage (divided by 100); a value
    in ``[0, 1]`` is treated as an already-normalized fraction.
    """
    if raw is None:
        raise BadInput("Give me a rate, e.g. `15` or `15%`.")
    token = raw.strip().rstrip("%").strip()
    try:
        value = float(token)
    except ValueError:
        raise BadInput(f"Couldn't read **{raw}** as a percentage. Try `15` or `15%`.")
    if value < 0:
        raise BadInput("Tax rate can't be negative.")
    return value / 100 if value > 1 else value


class AdminCog(commands.Cog, name="Manager"):
    """Macro/central-bank controls reserved for Tendies Managers."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # $print — add money to the pool (inflationary). §3
    # ------------------------------------------------------------------
    @commands.command(name="print")
    async def print_money_cmd(self, ctx: commands.Context, amount: str) -> None:
        if not await discordutil.require_manager(ctx):
            return
        nuggies = parse_amount(amount)

        # Preview the inflation hit from a read-only snapshot before confirming.
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            supply = await money.money_supply(session, ctx.guild.id)
            index_before = state.inflation_index
            index_after = economy.projected_index(index_before, nuggies, supply)

        real_drop = (1 - index_before / index_after) if index_after > 0 else 0.0
        warning = (
            f"{emojis.MONEY_PRINTER} ⚠️ This will raise the pool by **{fmt.fmt(nuggies)}** nug and push "
            f"inflation from **{index_before:.3f} → {index_after:.3f}**. "
            f"Every real balance on the server drops ~{real_drop * 100:.1f}%. "
            f"React ✅ within 60s to confirm."
        )
        if not await discordutil.confirm(ctx, warning, timeout=60):
            await ctx.send("Print cancelled. The savers breathe easy.")
            return

        # Confirmed: mint in a fresh transaction so the snapshot session is closed.
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            new_index = await economy.apply_print(session, state, nuggies)
            pool_after = state.pool_balance

        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.MONEY_PRINTER} Money printed",
                f"Printed **{fmt.abbr(nuggies)}** nug. "
                f"Pool: **{fmt.fmt(pool_after)}** nug. "
                f"Inflation index: **{new_index:.3f}**.\n"
                f"The market will remember this.",
            )
        )

    # ------------------------------------------------------------------
    # $taxrate — set the wage + dividend tax. §9
    # ------------------------------------------------------------------
    @commands.command(name="taxrate")
    async def taxrate_cmd(self, ctx: commands.Context, percent: str) -> None:
        if not await discordutil.require_manager(ctx):
            return
        rate = _parse_percent(percent)
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            await economy.set_tax_rate(session, state, rate)
            new_rate = state.tax_rate
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.TREASURY_POOL} Tax rate updated",
                f"Wage + dividend tax is now **{new_rate * 100:.1f}%**. "
                f"Every payout from here on withholds at this rate into the pool.",
            )
        )

    # ------------------------------------------------------------------
    # $setday — correct calendar drift. §4
    # ------------------------------------------------------------------
    @commands.command(name="setday")
    async def setday_cmd(self, ctx: commands.Context, weekday: str) -> None:
        if not await discordutil.require_manager(ctx):
            return
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            await economy.set_day(session, state, weekday)
            day_name = state.weekday
            game_day = state.game_day
            is_open = gameday.is_business_day(game_day)

        status = (
            f"{emojis.STOCK_UP} market **OPEN**"
            if is_open
            else "🌙 market **CLOSED** (prices frozen until Monday)"
        )
        await ctx.send(
            embed=discordutil.embed(
                "📅 Calendar corrected",
                f"Game day is now **{day_name.capitalize()}** ({game_day.isoformat()}) — {status}.",
            )
        )

    # ------------------------------------------------------------------
    # $event — fire an admin event for today. §5
    # ------------------------------------------------------------------
    @commands.command(name="event")
    async def event_cmd(self, ctx: commands.Context, *, raw: str = "") -> None:
        if not await discordutil.require_manager(ctx):
            return
        industry, multiplier, blurb = self._parse_event_args(raw)

        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            event = await events.create_admin_event(
                session, state, industry, multiplier, blurb
            )
            event_blurb = event.blurb
            today = state.weekday

        if industry == events.MARKET_WIDE:
            label, ind_emoji = "every industry", emojis.STOCK_DOWN
        else:
            label, ind_emoji = industry.capitalize(), emojis.industry(industry)
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.BREAKING_NEWS} BREAKING — {event_blurb}",
                f"{ind_emoji} **{label} ×{multiplier:g}** for the rest of today "
                f"({today.capitalize()}). It hits both production revenue and "
                f"valuation. Trade accordingly.",
            )
        )

    def _parse_event_args(self, raw: str) -> tuple[str, float, str]:
        """Parse ``<industry> <multiplier> "<blurb>"`` into its three parts.

        ``industry`` is a canonical industry (via :func:`config.normalize_industry`)
        or the literal ``all``/``market`` for a server-wide event; ``multiplier``
        must be a positive float; ``blurb`` is the (optionally quoted) remainder.
        """
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        if len(tokens) < 3:
            raise BadInput(
                'Usage: `$event <industry> <multiplier> "<blurb>"` — '
                'e.g. `$event energy 0.4 "Pipeline rupture in the Gulf"`.'
            )

        industry_raw, multiplier_raw = tokens[0], tokens[1]
        blurb = " ".join(tokens[2:]).strip()
        if not blurb:
            raise BadInput("Give the event a headline blurb in quotes.")

        key = industry_raw.strip().lower()
        if key in ("all", "market"):
            industry = events.MARKET_WIDE
        else:
            normalized = config.normalize_industry(industry_raw)
            if normalized is None:
                raise BadInput(
                    f"Unknown industry **{industry_raw}**. Pick one of "
                    f"{', '.join(config.INDUSTRIES)} — or `all` for a market-wide event."
                )
            industry = normalized

        try:
            multiplier = float(multiplier_raw)
        except ValueError:
            raise BadInput(f"Couldn't read **{multiplier_raw}** as a multiplier.")
        if multiplier <= 0:
            raise BadInput("Multiplier must be greater than 0.")

        return industry, multiplier, blurb

    # ------------------------------------------------------------------
    # $forcetick — advance the game one day now (ops/testing). §9
    # ------------------------------------------------------------------
    @commands.command(name="forcetick")
    async def forcetick_cmd(self, ctx: commands.Context) -> None:
        if not await discordutil.require_manager(ctx):
            return
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id, for_update=True)
            report = await tick.run_tick(session, state)

        if report.closed:
            await ctx.send(
                embed=discordutil.embed(
                    f"🌙 Closed tick — {report.weekday.capitalize()}",
                    "The day advanced but the market was closed — no production, "
                    "wages, or events. Back-office actions still work.",
                )
            )
            return

        await ctx.send(embed=render_tick_report(report))


async def setup(bot) -> None:
    await bot.add_cog(AdminCog(bot))
