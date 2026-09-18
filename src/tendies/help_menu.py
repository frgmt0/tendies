"""The custom ``$help`` menu — onboarding for someone who's never touched the bot.

discord.py lets you replace its plain help with a :class:`commands.HelpCommand`
subclass; :class:`TendiesHelp` is ours. It handles ``$help`` (a curated
getting-started landing page), ``$help <command>`` (per-command detail with
game-specific context), ``$help <Category>``, and unknown-command errors.

Design rule (matches the rest of the codebase): the numbers shown here are
*derived*, never hand-typed. The industry list comes from :mod:`config`, the
accredited threshold / tax / windows come from config constants, and the
``$found`` fee ladder is computed live from the guild's :class:`ServerState`
(``base_founding_fee``/``fee_multiplier``) and the caller's current company
count — so the help can't drift out of sync with the actual game.
"""

from __future__ import annotations

import discord
from discord.ext import commands
from sqlalchemy import func, select

from . import config, emojis
from .formatting import fmt
from .models import Company, ServerState

NUGGIE_GOLD = 0xF1C40F


# ---------------------------------------------------------------------------
# Curated, authoritative command catalog. Keyed by primary command name.
# ---------------------------------------------------------------------------

#: One-line summary shown in the landing menu.
SUMMARY: dict[str, str] = {
    "balance": "your wallet, job, net worth, and holdings",
    "jobs": "list every open position (state jobs are always hiring)",
    "apply": "apply to a job by id (state jobs hire you instantly)",
    "clockin": "show up for work — earn your wage, build a streak",
    "clockout": "skip work for the day (no wage, no production)",
    "reminders": "toggle a ping when you forget to clock in",
    "quit": "leave a job; keep vested equity, forfeit the rest",
    "found": "start your own company (costs a scaling fee)",
    "company": "inspect a company: treasury, cap table, valuation",
    "postjob": "post a job opening at your company",
    "applicants": "review who applied to your company",
    "hire": "hire an applicant",
    "fire": "let an employee go",
    "promote": "give one of your employees a raise",
    "raise": "open a funding round to sell equity for cash",
    "deposit": "put your wallet cash into your company treasury",
    "closeround": "close a funding round and keep funds already raised",
    "invest": "buy equity in an open round (accredited investors)",
    "dividend": "pay treasury cash to shareholders, pro-rata",
    "acquire": "offer to buy another company",
    "accept": "accept an acquisition offer made to your company",
    "decline": "decline an acquisition offer",
    "market": "company valuations and moves since the last close",
    "leaderboard": "richest players by real net worth",
    "pool": "the server treasury, money supply, tax, inflation",
    "today": "what game-day it is and whether markets are open",
    "print": "mint nuggies into the pool (inflationary)",
    "taxrate": "set the wage + dividend tax rate",
    "setday": "set the testing calendar (accelerated mode only)",
    "event": "fire a market event on an industry",
    "stats": "macro dashboard: pool health, flows, wealth concentration",
    "forcetick": "close a testing day (accelerated mode only)",
}

#: Friendly usage string (without the prefix). Hand-written because the raw
#: function signatures (e.g. ``*, args``) don't read well.
USAGE: dict[str, str] = {
    "balance": "balance",
    "jobs": "jobs [page]",
    "apply": "apply <job_id>",
    "clockin": "clockin",
    "clockout": "clockout",
    "reminders": "reminders <on | off>",
    "quit": "quit [ticker]",
    "found": "found <ticker> <name> <industry>",
    "company": "company <ticker>",
    "postjob": "postjob <ticker>",
    "applicants": "applicants <ticker>",
    "hire": "hire <ticker> <letter | @user>",
    "fire": "fire <ticker> <@user>",
    "promote": "promote <@employee> <% raise>",
    "raise": "raise <ticker> <amount> <equity%>",
    "deposit": "deposit <ticker> <amount>",
    "closeround": "closeround <ticker>",
    "invest": "invest <ticker> <amount>",
    "dividend": "dividend <ticker> <amount>",
    "acquire": "acquire <yourTicker> <targetTicker> <offer>",
    "accept": "accept <acquirerTicker>",
    "decline": "decline <acquirerTicker>",
    "market": "market [page]",
    "leaderboard": "leaderboard",
    "pool": "pool",
    "today": "today",
    "print": "print <amount>",
    "taxrate": "taxrate <percent>",
    "setday": "setday <weekday>",
    "event": 'event <industry> <multiplier> "<blurb>"',
    "stats": "stats",
    "forcetick": "forcetick",
}

#: "Good to know" context for the commands a newcomer most needs explained.
#: Values referencing config constants are interpolated at import time, so they
#: stay correct if a knob changes. ``found`` is handled dynamically (below).
EXTRAS: dict[str, str] = {
    "apply": (
        "State employers (McNuggie's, Public Works, The Postal Service) accept "
        "you instantly. Private jobs queue you until the owner picks you with "
        "`$hire`."
    ),
    "clockin": (
        "Wages aren't paid instantly — you're paid at the **daily close** (the "
        "tick), and only if you clocked in. Markets are closed on weekends. Some "
        "jobs also vest equity the longer you stay.\n"
        "Clocking in every business day builds a **streak** 🔥 — hit "
        f"{', '.join(str(d) for d, _ in config.STREAK_MILESTONES)} days for "
        "one-time loyalty bonuses. The first time you clock in, I'll offer to "
        "**ping you if you forget** (toggle anytime with `reminders on/off`)."
    ),
    "reminders": (
        "Opt in or out of a daily ping when you've forgotten to clock in on a "
        "business day — sent partway through the day so you've still got time."
    ),
    "promote": (
        "Owner only. Raises the employee's daily wage by your percentage, paid "
        "from your company's treasury from the next tick on. Resolves the company "
        "from where they work, so you just mention the person."
    ),
    "quit": (
        "Vested shares are yours to keep forever; unvested shares are forfeited. "
        "You'll get a ✅ confirmation prompt first."
    ),
    "raise": (
        "You sell brand-new shares for cash into your treasury, which **dilutes** "
        "existing holders. A bigger equity% raises more but dilutes you more."
    ),
    "invest": (
        f"Open only to **accredited** investors: your income must annualize to at "
        f"least **{fmt(config.ACCREDITED_THRESHOLD)} nug/yr**, measured over your "
        f"last {config.INCOME_WINDOW_DAYS} business days of earnings. Keep clocking "
        f"in to qualify — or found your own company."
    ),
    "dividend": (
        "Paid pro-rata to every shareholder by stake. Like wages, dividends are "
        "taxed and the tax flows to the pool."
    ),
    "acquire": (
        "A company-to-company deal: the offer is paid from **your** company's "
        "treasury to the target's shareholders. The target's treasury and "
        "open job listings fold into yours; its staff are laid off and may re-apply."
    ),
    "pool": (
        "Wallets, valuations, and net worth are shown in **real** terms (adjusted "
        "for inflation). Wages, tax, and revenue are shown nominal."
    ),
    "market": (
        "Business-day valuations update as the economy changes; daily moves compare "
        "with the previous close. Weekend prices retain the last close. Buy equity "
        "through funding rounds; player-to-player share trading is not available."
    ),
    "print": (
        "**Manager only.** Adds brand-new nuggies to the pool and raises the "
        "inflation index, shrinking everyone's real wealth. Needs ✅ confirmation."
    ),
    "taxrate": (
        "**Manager only.** Tax is withheld from every wage and dividend and "
        "flows to the pool — the main way to refill it."
    ),
    "event": (
        "**Manager only.** A multiplier below 1 is a slump, above 1 a boom; use "
        "`all` to hit every industry. It moves both production revenue and "
        "valuations for the rest of the day."
    ),
    "forcetick": "**Manager only.** Ops/testing — runs a daily close immediately.",
    "stats": (
        "**Manager only.** Your governance cockpit: money supply and prints, "
        "pool health (and the recession cap), the last few days of flows in/out "
        "of the pool, who's working, and wealth concentration (Gini). Use it to "
        "decide when to adjust taxes or print."
    ),
}

#: Ordered categories for the landing menu: (emoji, heading, command names).
CATEGORIES: list[tuple[str, str, list[str]]] = [
    (emojis.HIRING, "Player & jobs",
     ["balance", "jobs", "apply", "clockin", "clockout", "reminders", "quit"]),
    (emojis.FACTORY, "Your companies",
     ["found", "company", "postjob", "applicants", "hire", "fire", "promote"]),
    (emojis.STOCK_UP, "Capital markets",
     ["deposit", "raise", "closeround", "invest", "dividend", "acquire", "accept", "decline"]),
    (emojis.LEADERBOARD, "Markets & info",
     ["market", "leaderboard", "pool", "today"]),
    (emojis.MONEY_PRINTER, "Managers · central bank",
     ["print", "taxrate", "setday", "event", "stats", "forcetick"]),
]

#: Title-bar emoji per command (falls back to the nuggie).
_TITLE_EMOJI: dict[str, str] = {
    name: emoji
    for emoji, _heading, names in CATEGORIES
    for name in names
}


# ---------------------------------------------------------------------------
# Pure builders (unit-testable; no Discord state, no DB).
# ---------------------------------------------------------------------------

_ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th")


def _ordinal(n: int) -> str:
    return _ORDINALS[n - 1] if 1 <= n <= len(_ORDINALS) else f"{n}th"


def fee_ladder(base: int, multiplier: int, *, rungs: int = 5) -> list[tuple[str, int]]:
    """The founding-fee schedule: ``[(ordinal, fee), ...]`` for the first
    ``rungs`` companies, using the same formula as the founding service
    (``fee = base * multiplier ** owned``)."""
    return [(_ordinal(i + 1), int(base * (multiplier ** i))) for i in range(rungs)]


def industries_field() -> str:
    """A scannable, emoji-tagged list of the canonical industries + a hint that
    common aliases are accepted."""
    cells = [f"{emojis.industry(i)} {i.capitalize()}" for i in config.INDUSTRIES]
    body = " · ".join(cells)
    return (
        f"{body}\n_Common aliases work too — e.g. `mining`→materials, "
        f"`pharma`→medicine, `retail`→consumer._"
    )


def build_landing_embed(prefix: str) -> discord.Embed:
    """The ``$help`` getting-started page."""
    desc = (
        "**Tendies** is a server-run economy. Everyone starts broke — grind a "
        "job, save nuggies (`nug`), found a company, hire workers, raise money, "
        "and climb the leaderboard.\n\n"
        f"**New here? Do this:**\n"
        f"`{prefix}jobs` → find work → `{prefix}apply <id>` → take it → "
        f"`{prefix}clockin` every weekday → watch `{prefix}balance` grow → "
        f"`{prefix}found` your own company.\n\n"
        "Wages settle on weekdays at the **daily close** — "
        "not the instant you clock in.\n\n"
        f"Type `{prefix}help <command>` for the full rundown — e.g. "
        f"`{prefix}help found`. Found a bug? Use **/bug** to prepare a GitHub issue."
    )
    embed = discord.Embed(
        title=f"{emojis.NUGGIE} Tendies — how to play",
        description=desc,
        color=NUGGIE_GOLD,
    )
    for emoji, heading, names in CATEGORIES:
        lines = [
            f"`{prefix}{name}` — {SUMMARY[name]}"
            for name in names
            if name in SUMMARY
        ]
        embed.add_field(name=f"{emoji} {heading}", value="\n".join(lines), inline=False)
    return embed


def build_command_embed(
    prefix: str,
    name: str,
    *,
    aliases: list[str] | None = None,
    full_help: str | None = None,
    extra_fields: list[tuple[str, str]] | None = None,
) -> discord.Embed:
    """Per-command detail page."""
    title_emoji = _TITLE_EMOJI.get(name, emojis.NUGGIE)
    summary = SUMMARY.get(name, "")
    if not summary and full_help:
        summary = full_help.strip().splitlines()[0]
    embed = discord.Embed(
        title=f"{title_emoji} {prefix}{name}",
        description=summary or None,
        color=NUGGIE_GOLD,
    )
    usage = USAGE.get(name, name)
    embed.add_field(name="Usage", value=f"`{prefix}{usage}`", inline=False)
    if aliases:
        embed.add_field(
            name="Aliases",
            value=", ".join(f"`{prefix}{a}`" for a in aliases),
            inline=False,
        )
    note = EXTRAS.get(name)
    if note:
        embed.add_field(name="Good to know", value=note, inline=False)
    for fname, fvalue in (extra_fields or []):
        embed.add_field(name=fname, value=fvalue, inline=False)
    return embed


def build_cog_embed(prefix: str, heading: str, names: list[str]) -> discord.Embed:
    embed = discord.Embed(title=f"{emojis.NUGGIE} {heading}", color=NUGGIE_GOLD)
    for name in names:
        if name not in SUMMARY:
            continue
        embed.add_field(
            name=f"`{prefix}{USAGE.get(name, name)}`",
            value=SUMMARY[name],
            inline=False,
        )
    return embed


# ---------------------------------------------------------------------------
# The HelpCommand subclass wired into the bot.
# ---------------------------------------------------------------------------

class TendiesHelp(commands.HelpCommand):
    """Custom help. ``$help`` → landing page; ``$help <cmd>`` → detail."""

    def __init__(self) -> None:
        super().__init__(
            command_attrs={"help": "Show how to play, or details for a command."}
        )

    async def send_bot_help(self, mapping) -> None:  # noqa: ARG002
        prefix = self.context.clean_prefix
        await self.get_destination().send(embed=build_landing_embed(prefix))

    async def send_command_help(self, command: commands.Command) -> None:
        prefix = self.context.clean_prefix
        extra_fields = await self._dynamic_fields(command.name)
        await self.get_destination().send(
            embed=build_command_embed(
                prefix,
                command.name,
                aliases=list(command.aliases),
                full_help=command.help,
                extra_fields=extra_fields,
            )
        )

    async def send_cog_help(self, cog: commands.Cog) -> None:
        prefix = self.context.clean_prefix
        names = [c.name for c in cog.get_commands()]
        heading = cog.qualified_name
        await self.get_destination().send(
            embed=build_cog_embed(prefix, heading, names)
        )

    async def send_group_help(self, group) -> None:  # pragma: no cover - no groups
        await self.send_command_help(group)

    async def send_error_message(self, error: str) -> None:
        await self.get_destination().send(f"⚠️ {error}")

    async def command_not_found(self, string: str) -> str:
        prefix = self.context.clean_prefix
        return (
            f"No command called **{string}**. Type `{prefix}help` to see "
            f"everything you can do."
        )

    async def _dynamic_fields(self, name: str) -> list[tuple[str, str]]:
        """Live, server-specific fields. Only ``$found`` needs them: the real
        industry set and the actual fee ladder for this guild + caller."""
        if name != "found":
            return []
        fields: list[tuple[str, str]] = [("Industries", industries_field())]

        ctx = self.context
        if ctx.guild is None:
            return fields  # DMs have no economy; show the static industry list only

        async with ctx.bot.db.session() as session:
            state = (await session.execute(
                select(ServerState).where(ServerState.guild_id == ctx.guild.id)
            )).scalars().first()
            if state is None:
                return fields
            owned = int(await session.scalar(
                select(func.count())
                .select_from(Company)
                .where(
                    Company.guild_id == ctx.guild.id,
                    Company.owner_id == ctx.author.id,
                    Company.is_state == False,  # noqa: E712
                    Company.active == True,  # noqa: E712
                )
            ) or 0)
            base = int(state.base_founding_fee)
            mult = int(state.fee_multiplier)

        ladder = fee_ladder(base, mult)
        ladder_str = " · ".join(f"{ord_}: {fmt(fee)}" for ord_, fee in ladder)
        next_fee = int(base * (mult ** owned))
        fee_value = (
            f"Scales **{mult}×** per company you already own:\n{ladder_str} nug\n"
            f"You currently own **{owned}**, so your next company costs "
            f"**{fmt(next_fee)} nug** (paid to the pool). You'll mint "
            f"**{fmt(config.SHARES_AT_FOUNDING)}** shares and own 100%."
        )
        fields.append(("Founding fee", fee_value))
        return fields
