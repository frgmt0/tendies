"""Capital-markets commands — Slice C (§11, §12, §14).

Thin Discord edge over :mod:`tendies.services.investment` and
:mod:`tendies.services.acquisitions`:

* ``$raise``    — open a funding round.
* ``$closeround`` — owner/Manager closes an incomplete funding round.
* ``$invest``   — fill a round (gated on annualized income).
* ``$deposit``  — owner moves wallet funds into company treasury.
* ``$dividend`` — owner pays a pro-rata dividend from treasury.
* ``$acquire``  — send a company-to-company acquisition offer.
* ``$accept`` / ``$decline`` — the target owner answers an offer.

Each command opens a session, fetches state via :func:`lookups.get_state`, calls
the service, and renders the result. :class:`~tendies.errors.GameError` is left
to propagate so the bot's ``on_command_error`` renders it.
"""

from __future__ import annotations

from discord.ext import commands

from .. import discordutil, emojis, lookups
from ..discordutil import parse_amount, parse_percent
from ..formatting import fmt
from ..services import acquisitions, investment

DISPLAY_ROWS = 20


class CapitalCog(commands.Cog, name="Capital"):
    """Raising capital, investing, dividends, and acquisitions."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------------
    # $raise <ticker> <amount> <equity%>
    # -------------------------------------------------------------------
    @commands.command(name="raise", aliases=["fundraise"])
    async def raise_round(
        self, ctx: commands.Context, ticker: str, amount: str, equity_pct: str
    ) -> None:
        """Open a funding round: $raise <ticker> <amount> <equity%>."""
        nuggies = parse_amount(amount)
        # Shared parser, so `10`, `10%`, and `2.5` all work (a bare number is
        # always a percent) — the bare `float` converter rejected `10%`.
        pct = parse_percent(equity_pct, label="equity percentage")
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await investment.open_round(
                session, state, ctx.author.id, ticker, nuggies, pct
            )

        desc = (
            f"**{result.ticker}** is raising **{fmt(result.amount)} nug** for "
            f"**{result.equity_pct:g}%** ({fmt(result.new_shares)} new sh).\n"
            f"Implied valuation: **{fmt(result.implied_valuation)} nug**. "
            f"Open to accredited investors.\n"
            f"`$invest {result.ticker} <amount>` to take a slice."
        )
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.STOCK_UP} {result.ticker} funding round open", desc
            )
        )

    @commands.command(name="closeround")
    async def close_round(self, ctx: commands.Context, ticker: str) -> None:
        """Close an incomplete funding round: $closeround <ticker>."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await investment.close_round(
                session,
                state,
                ctx.author.id,
                ticker,
                manager=discordutil.is_manager(ctx),
            )
        await ctx.send(
            embed=discordutil.embed(
                f"🔒 {result.ticker} funding round closed",
                f"Raised **{fmt(result.amount_raised)} / {fmt(result.target_amount)} nug** "
                f"and minted **{fmt(result.shares_minted)} shares**. Existing "
                "investments remain in place.",
            )
        )

    # -------------------------------------------------------------------
    # $invest <ticker> <amount>
    # -------------------------------------------------------------------
    @commands.command(name="invest")
    async def invest(self, ctx: commands.Context, ticker: str, amount: str) -> None:
        """Buy into an open funding round: $invest <ticker> <amount>."""
        nuggies = parse_amount(amount)
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await investment.invest(
                session, state, ctx.author.id, ticker, nuggies
            )

        closed_note = (
            "\n🔒 The round is now **fully subscribed and closed**."
            if result.round_closed
            else ""
        )
        desc = (
            f"You invested **{fmt(result.amount_invested)} nug** in "
            f"**{result.ticker}** for {fmt(result.shares)} sh "
            f"(**{result.pct_of_company:.2f}%** of the company).{closed_note}"
        )
        await ctx.send(
            embed=discordutil.embed(f"{emojis.STOCK_UP} Invested in {result.ticker}", desc)
        )

    @commands.command(name="deposit")
    async def deposit(self, ctx: commands.Context, ticker: str, amount: str) -> None:
        """Move your wallet funds into your company: $deposit <ticker> <amount>."""
        nuggies = parse_amount(amount)
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await investment.deposit(
                session, state, ctx.author.id, ticker, nuggies
            )
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.TREASURY_POOL} Deposited into {result.ticker}",
                f"Moved **{fmt(result.amount)} nug** from your wallet into the "
                f"company treasury. Treasury: **{fmt(result.treasury_after)} nug**.",
            )
        )

    # -------------------------------------------------------------------
    # $dividend <ticker> <amount>
    # -------------------------------------------------------------------
    @commands.command(name="dividend", aliases=["div"])
    async def dividend(self, ctx: commands.Context, ticker: str, amount: str) -> None:
        """Pay a pro-rata dividend from treasury (owner): $dividend <ticker> <amount>."""
        nuggies = parse_amount(amount)
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await investment.pay_dividend(
                session, state, ctx.author.id, ticker, nuggies
            )

        total_shares = sum(p.shares for p in result.payouts)
        lines = [
            f"**{result.ticker}** paid a {fmt(result.amount)} nug dividend across "
            f"{fmt(total_shares)} sh ({result.per_share:.2f}/sh)."
        ]
        for p in result.payouts[:DISPLAY_ROWS]:
            lines.append(
                f"  {discordutil.mention(p.user_id)} ({fmt(p.shares)} sh) → "
                f"{fmt(p.gross)} (−{fmt(p.tax)} tax) = {fmt(p.net)} nug"
            )
        if len(result.payouts) > DISPLAY_ROWS:
            lines.append(f"  … and {len(result.payouts) - DISPLAY_ROWS} more shareholders")
        lines.append(f"Tax → pool: {fmt(result.total_tax)} nug.")
        await ctx.send(
            embed=discordutil.embed(f"{emojis.DIVIDEND} Dividend paid", "\n".join(lines))
        )

    # -------------------------------------------------------------------
    # $acquire <acquirer> <target> <offer>
    # -------------------------------------------------------------------
    @commands.command(name="acquire")
    async def acquire(
        self, ctx: commands.Context, acquirer: str, target: str, offer: str
    ) -> None:
        """Send an acquisition offer: $acquire <acquirer> <target> <offer>."""
        nuggies = parse_amount(offer)
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await acquisitions.offer(
                session, state, ctx.author.id, acquirer, target, nuggies
            )

        desc = (
            f"**{result.acquirer_ticker}** offers **{fmt(result.amount)} nug** to "
            f"acquire **{result.target_ticker}**.\n"
            f"{discordutil.mention(result.target_owner_id)}: "
            f"`$accept {result.acquirer_ticker} {result.target_ticker}` or "
            f"`$decline {result.acquirer_ticker} {result.target_ticker}`."
        )
        await ctx.send(
            embed=discordutil.embed(f"{emojis.ACQUISITION} Acquisition offer", desc)
        )

    # -------------------------------------------------------------------
    # $accept <acquirer>
    # -------------------------------------------------------------------
    @commands.command(name="accept")
    async def accept(
        self, ctx: commands.Context, acquirer: str, target: str | None = None
    ) -> None:
        """Accept an offer: $accept <acquirer> [target]."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await acquisitions.accept(
                session, state, ctx.author.id, acquirer, target
            )

        lines = [
            f"Deal closed. **{result.acquirer_ticker}** acquires "
            f"**{result.target_ticker}** for {fmt(result.amount)} nug.",
            "Cap table paid out:",
        ]
        for user_id, gross, tax in result.payouts[:DISPLAY_ROWS]:
            share_pct = (gross / result.amount * 100) if result.amount > 0 else 0.0
            lines.append(
                f"  {discordutil.mention(user_id)} ({share_pct:.0f}%) → "
                f"{fmt(gross)} nug (−{fmt(tax)} tax)"
            )
        if len(result.payouts) > DISPLAY_ROWS:
            lines.append(f"  … and {len(result.payouts) - DISPLAY_ROWS} more shareholders")

        absorbed = (
            f"**{result.target_ticker}**'s treasury "
            f"(+{fmt(result.treasury_absorbed)} nug) + "
            f"{result.jobs_transferred} job(s) → **{result.acquirer_ticker}**."
        )
        lines.append(absorbed)
        if result.employees_laid_off:
            lines.append(
                f"{result.employees_laid_off} employee(s) laid off "
                f"(re-apply to **{result.acquirer_ticker}** if wanted)."
            )
        lines.append(
            f"{emojis.STOCK_UP} **{result.acquirer_ticker}**'s production base changed. "
            f"Watch its price at the next tick."
        )
        await ctx.send(
            embed=discordutil.embed(f"{emojis.ACQUISITION} Deal closed", "\n".join(lines))
        )

    # -------------------------------------------------------------------
    # $decline <acquirer>
    # -------------------------------------------------------------------
    @commands.command(name="decline")
    async def decline(
        self, ctx: commands.Context, acquirer: str, target: str | None = None
    ) -> None:
        """Decline an offer: $decline <acquirer> [target]."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            await acquisitions.decline(
                session, state, ctx.author.id, acquirer, target
            )
        await ctx.send(
            embed=discordutil.embed(
                "🚫 Offer declined",
                f"You declined **{acquirer.strip().upper()}**'s acquisition offer.",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CapitalCog(bot))
