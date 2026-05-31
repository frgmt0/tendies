"""Company & jobs cog (SLICE B). Commands: $found, $company, $postjob,
$applicants, $hire, $fire (DESIGN §8/§9).

Cogs stay thin: parse args, open a session, fetch state, call the service, and
render. :class:`~tendies.errors.GameError` is allowed to propagate so the bot's
``on_command_error`` renders it.
"""

from __future__ import annotations

import re

from discord.ext import commands

from .. import discordutil, emojis, lookups
from ..discordutil import mention
from ..errors import BadInput, NotFound
from ..formatting import abbr, fmt, shares_pct
from ..services import companies

#: Industry → its title-cased display label for embeds.
_INDUSTRY_LABELS = {
    "food": "Food",
    "materials": "Materials",
    "tech": "Tech",
    "medicine": "Medicine",
    "energy": "Energy",
    "logistics": "Logistics",
    "infrastructure": "Infrastructure",
    "entertainment": "Entertainment",
    "finance": "Finance",
    "defense": "Defense",
    "consumer": "Consumer",
}

#: a/b/c… index letters for the applicant list.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _industry_label(industry: str) -> str:
    return _INDUSTRY_LABELS.get(industry, industry.title())


def _parse_found(raw: str) -> tuple[str, str, str]:
    """Parse ``$found`` args: first token is the ticker, last token the industry,
    everything between is the name (quotes stripped). Returns ``(ticker, name,
    industry)``. Raises :class:`BadInput` if there aren't enough tokens."""
    tokens = (raw or "").split()
    if len(tokens) < 3:
        raise BadInput(
            "Usage: `$found <ticker> <name> <industry>` "
            '(e.g. `$found MOON "Moon Mining Inc." materials`).'
        )
    ticker = tokens[0]
    industry = tokens[-1]
    name = " ".join(tokens[1:-1]).strip().strip('"').strip("'").strip()
    if not name:
        raise BadInput("A company needs a name between the ticker and the industry.")
    return ticker, name, industry


def _parse_user_id(token: str) -> int | None:
    """Resolve a mention (``<@123>`` / ``<@!123>``) or bare numeric id to an int."""
    m = re.fullmatch(r"<@!?(\d+)>", token.strip())
    if m:
        return int(m.group(1))
    t = token.strip()
    if t.isdigit():
        return int(t)
    return None


class CompanyCog(commands.Cog, name="Companies"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ found
    @commands.command(name="found")
    async def found(self, ctx: commands.Context, *, args: str = "") -> None:
        """$found <ticker> <name> <industry> — start a private company."""
        ticker, name, industry = _parse_found(args)
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await companies.found_company(
                session, state, ctx.author.id, ticker, name, industry
            )
            co = result.company
            label = _industry_label(co.industry)
            ord_label = (
                ""
                if result.companies_owned_after == 1
                else f" (your {companies._ordinal(result.companies_owned_after)} company)"
            )
            desc = (
                f"Fee: {fmt(result.fee)} nug{ord_label} → pool. "
                f"You hold {fmt(co.total_shares)} sh (100%).\n"
                f"Treasury: 0 nug. Post a job with `$postjob {co.ticker}` to start producing."
            )
            await ctx.send(
                embed=discordutil.embed(
                    f"{emojis.FACTORY} Founded {co.name} ({co.ticker}) in "
                    f"{emojis.industry(co.industry)} {label}.",
                    desc,
                )
            )

    # ---------------------------------------------------------------- company
    @commands.command(name="company")
    async def company(self, ctx: commands.Context, ticker: str) -> None:
        """$company <ticker> — show a company's full picture."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            detail = await companies.company_detail(session, state, ticker)
            co = detail.company
            label = _industry_label(co.industry)

            lines: list[str] = []
            if co.is_state:
                lines.append("Owner: **State-owned** (a public faucet)")
                lines.append(f"Treasury: {fmt(co.treasury)} nug")
                lines.append(
                    f"Employees clocked in today: {detail.clocked_in} / {detail.total_employees}"
                )
                jobs = await self._open_jobs(session, co.id)
                if jobs:
                    lines.append("")
                    lines.append("**Open jobs:**")
                    for job in jobs:
                        lines.append(f"  • {job.title} — {fmt(job.daily_wage)} nug/day")
                else:
                    lines.append("")
                    lines.append("No open jobs right now.")
                title = (
                    f"{emojis.TREASURY_POOL} {co.name} ({co.ticker}) — "
                    f"{emojis.industry(co.industry)} {label} · state-owned"
                )
            else:
                lines.append(f"Owner: {mention(co.owner_id)}")
                lines.append(f"Treasury: {fmt(co.treasury)} nug")
                lines.append(f"Shares: {fmt(co.total_shares)} total")
                for uid, shares in detail.cap_table:
                    lines.append(
                        f"  {mention(uid)} ... {fmt(shares)} ({shares_pct(shares, co.total_shares)})"
                    )
                lines.append(
                    f"Employees clocked in today: {detail.clocked_in} / {detail.total_employees}"
                )
                val = detail.valuation
                lines.append(f"Avg daily revenue (10d): {fmt(val.avg_daily_revenue)} nug")
                lines.append(
                    f"Valuation (real): {fmt(val.real_value)} nug "
                    f"→ share price {val.share_price:,.2f} nug"
                )
                title = (
                    f"{emojis.FACTORY} {co.name} ({co.ticker}) — "
                    f"{emojis.industry(co.industry)} {label}"
                )

            await ctx.send(embed=discordutil.embed(title, "\n".join(lines)))

    async def _open_jobs(self, session, company_id: int):
        from sqlalchemy import select

        from ..models import Job

        return (
            await session.execute(
                select(Job)
                .where(Job.company_id == company_id, Job.open == True)  # noqa: E712
                .order_by(Job.id.asc())
            )
        ).scalars().all()

    # ---------------------------------------------------------------- postjob
    @commands.command(name="postjob")
    async def postjob(self, ctx: commands.Context, ticker: str) -> None:
        """$postjob <ticker> — owner-only interactive job posting."""
        # Validate ownership up front so we don't prompt non-owners.
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            company = await lookups.get_company(
                session, state.guild_id, ticker, private_only=True
            )
            lookups.require_owner(company, ctx.author.id)
            display_ticker = company.ticker

        fields = await discordutil.prompt_text(
            ctx,
            "Reply with: `<title> | <daily wage> | <equity sh, or 0> | <vest days, or 0>`\n"
            "Then paste the description in your next message.",
        )
        if fields is None:
            await ctx.send("⌛ Timed out waiting for the job details. Try `$postjob` again.")
            return

        title, daily_wage, equity_shares, vest_days = _parse_job_fields(fields)

        description = await discordutil.prompt_text(
            ctx, "Now paste the job description."
        )
        if description is None:
            await ctx.send("⌛ Timed out waiting for the description. Try `$postjob` again.")
            return

        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            job = await companies.post_job(
                session,
                state,
                ctx.author.id,
                display_ticker,
                title,
                description,
                daily_wage,
                equity_shares,
                vest_days,
            )
            job_id = job.id
            job_title = job.title

        if equity_shares > 0:
            equity_clause = f" + {fmt(equity_shares)} sh vesting over {vest_days}d"
        else:
            equity_clause = ""
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.HIRING} Posted: {display_ticker} — {job_title} — "
                f"{fmt(daily_wage)} nug/day{equity_clause}.",
                f"Job ID {job_id}. Applicants will show in `$applicants {display_ticker}`.",
            )
        )

    # ------------------------------------------------------------- applicants
    @commands.command(name="applicants")
    async def applicants(self, ctx: commands.Context, ticker: str) -> None:
        """$applicants <ticker> — owner-only list of pending applicants."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            company = await lookups.get_company(
                session, state.guild_id, ticker, private_only=True
            )
            lookups.require_owner(company, ctx.author.id)
            display_ticker = company.ticker
            applicants = await companies.list_applicants(
                session, state, ctx.author.id, ticker
            )

        if not applicants:
            await ctx.send(
                embed=discordutil.embed(
                    f"{emojis.HIRING} {display_ticker} — applicants",
                    "No pending applicants yet.",
                )
            )
            return

        lines: list[str] = []
        for i, app in enumerate(applicants):
            letter = _LETTERS[i] if i < len(_LETTERS) else str(i + 1)
            lines.append(
                f"{letter}) {mention(app.user_id)} — "
                f"net worth {abbr(app.net_worth)}, "
                f"currently {app.current_label}  "
                f"_(applied for {app.job_title})_"
            )
        first = _LETTERS[0]
        last = (
            _LETTERS[len(applicants) - 1]
            if len(applicants) <= len(_LETTERS)
            else str(len(applicants))
        )
        lines.append("")
        lines.append(
            f"`$hire {display_ticker} {first}`"
            + (f"   |   `$hire {display_ticker} {last}`" if len(applicants) > 1 else "")
            + f"   |   `$fire {display_ticker} @user`"
        )
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.HIRING} {display_ticker} — applicants", "\n".join(lines)
            )
        )

    # ------------------------------------------------------------------- hire
    @commands.command(name="hire")
    async def hire(self, ctx: commands.Context, ticker: str, selector: str) -> None:
        """$hire <ticker> <letter|@user|id> — owner-only, hire an applicant."""
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            company = await lookups.get_company(
                session, state.guild_id, ticker, private_only=True
            )
            lookups.require_owner(company, ctx.author.id)
            display_ticker = company.ticker

            applicants = await companies.list_applicants(
                session, state, ctx.author.id, ticker
            )
            application_id = _resolve_selector(selector, applicants)

            result = await companies.hire(
                session, state, ctx.author.id, ticker, application_id
            )

        if result.equity_shares > 0:
            equity_clause = (
                f", {fmt(result.equity_shares)} sh vesting over "
                f"{result.vest_days} business days"
            )
        else:
            equity_clause = ""
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.HIRING} Hired!",
                f"{mention(result.user_id)} hired at {display_ticker}, "
                f"{fmt(result.daily_wage)} nug/day{equity_clause}.",
            )
        )

    # ------------------------------------------------------------------- fire
    @commands.command(name="fire")
    async def fire(self, ctx: commands.Context, ticker: str, target: str) -> None:
        """$fire <ticker> <@user> — owner-only, end someone's employment."""
        target_id = _parse_user_id(target)
        if target_id is None:
            raise BadInput("Mention the user to fire, e.g. `$fire MOON @someone`.")
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            company = await lookups.get_company(
                session, state.guild_id, ticker, private_only=True
            )
            display_ticker = company.ticker
            await companies.fire(session, state, ctx.author.id, ticker, target_id)
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.FIRED} Fired.",
                f"{mention(target_id)} no longer works at {display_ticker}. "
                f"Unvested equity is forfeit; vested shares are theirs to keep.",
            )
        )


def _parse_job_fields(raw: str) -> tuple[str, int, int, int]:
    """Parse the 4 pipe-separated job fields into
    ``(title, daily_wage, equity_shares, vest_days)``."""
    parts = [p.strip() for p in (raw or "").split("|")]
    if len(parts) != 4:
        raise BadInput(
            "Bad format. Reply with exactly 4 fields separated by `|`: "
            "`<title> | <daily wage> | <equity sh, or 0> | <vest days, or 0>`."
        )
    title = parts[0]
    if not title:
        raise BadInput("The job needs a title (the first field).")
    daily_wage = _parse_int(parts[1], "daily wage")
    equity_shares = _parse_int(parts[2], "equity shares")
    vest_days = _parse_int(parts[3], "vest days")
    return title, daily_wage, equity_shares, vest_days


def _parse_int(token: str, field: str) -> int:
    """Parse a whole number, tolerating thousands separators."""
    cleaned = token.replace(",", "").replace("_", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        raise BadInput(f"The {field} must be a whole number, got `{token}`.")


def _resolve_selector(
    selector: str, applicants: list["companies.ApplicantInfo"]
) -> int:
    """Turn a hire selector (letter, mention, or id) into an application_id."""
    sel = selector.strip()

    # Letter selector (a, b, c, ...).
    if len(sel) == 1 and sel.lower() in _LETTERS:
        idx = _LETTERS.index(sel.lower())
        if idx >= len(applicants):
            raise NotFound(f"No applicant `{sel}` — only {len(applicants)} pending.")
        return applicants[idx].application_id

    # Mention / id selector.
    uid = _parse_user_id(sel)
    if uid is not None:
        matches = [a for a in applicants if a.user_id == uid]
        if not matches:
            raise NotFound("That user isn't a pending applicant here.")
        return matches[0].application_id

    raise BadInput(
        "Pick an applicant by letter (e.g. `a`) or by mention (e.g. `@user`)."
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CompanyCog(bot))
