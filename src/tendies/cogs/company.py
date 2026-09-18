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
DISPLAY_ROWS = 20


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


#: Replies that abort an interactive flow at any prompt.
_CANCEL_WORDS = ("cancel", "abort", "stop", "quit", "nevermind", "never mind")

#: How much of a job description fits on one line of the company card.
DESCRIPTION_PREVIEW = 80


def _is_cancel(text: str | None) -> bool:
    return (text or "").strip().casefold() in _CANCEL_WORDS


def _preview(text: str | None, limit: int = DESCRIPTION_PREVIEW) -> str:
    """One-line, length-capped version of a job description."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _job_lines(jobs) -> list[str]:
    """Render the open-jobs block, description included (truncated)."""
    lines: list[str] = []
    for job in jobs[:DISPLAY_ROWS]:
        lines.append(f"  • {job.title} — {fmt(job.daily_wage)} nug/day")
        blurb = _preview(job.description)
        if blurb:
            lines.append(f"    _{blurb}_")
    if len(jobs) > DISPLAY_ROWS:
        lines.append(f"  … and {len(jobs) - DISPLAY_ROWS} more open roles")
    return lines


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
                lines.append("")
                if jobs:
                    lines.append("**Open jobs:**")
                    lines.extend(_job_lines(jobs))
                else:
                    lines.append("No open jobs right now.")
                title = (
                    f"{emojis.TREASURY_POOL} {co.name} ({co.ticker}) — "
                    f"{emojis.industry(co.industry)} {label} · state-owned"
                )
            else:
                lines.append(f"Owner: {mention(co.owner_id)}")
                lines.append(f"Treasury: {fmt(co.treasury)} nug")
                lines.append(f"Shares: {fmt(co.total_shares)} total")
                for uid, shares in detail.cap_table[:DISPLAY_ROWS]:
                    lines.append(
                        f"  {mention(uid)} ... {fmt(shares)} ({shares_pct(shares, co.total_shares)})"
                    )
                if len(detail.cap_table) > DISPLAY_ROWS:
                    lines.append(
                        f"  … and {len(detail.cap_table) - DISPLAY_ROWS} more shareholders"
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

                jobs = await self._open_jobs(session, co.id)
                lines.append("")
                if jobs:
                    lines.append("**Open jobs:**")
                    lines.extend(_job_lines(jobs))
                    lines.append(f"Apply with `{ctx.prefix}jobs` to get the job id.")
                else:
                    lines.append(
                        f"No open jobs right now — the owner can post one with "
                        f"`{ctx.prefix}postjob {co.ticker}`."
                    )

                # The owner is the only person who can act on an inbound offer,
                # and nothing else surfaces one, so show it right here.
                if co.owner_id == ctx.author.id:
                    offers = await self._inbound_offers(session, co.id)
                    if offers:
                        lines.append("")
                        lines.append("**Pending acquisition offers (you decide):**")
                        for acquirer_ticker, amount in offers[:DISPLAY_ROWS]:
                            lines.append(
                                f"  • **{acquirer_ticker}** offers {fmt(amount)} nug "
                                f"— `{ctx.prefix}accept {acquirer_ticker}` / "
                                f"`{ctx.prefix}decline {acquirer_ticker}`"
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

    async def _inbound_offers(self, session, company_id: int) -> list[tuple[str, int]]:
        """Open acquisition offers *for* this company, as (acquirer ticker, amount).

        Read straight off the models: the acquisitions service exposes no
        "offers against me" query and its signatures are owned elsewhere.
        """
        from sqlalchemy import select

        from ..models import Company, Offer

        rows = (
            await session.execute(
                select(Offer.amount, Company.ticker)
                .join(Company, Offer.acquirer_id == Company.id)
                .where(Offer.target_id == company_id, Offer.status == "open")
                .order_by(Offer.id.asc())
            )
        ).all()
        return [(ticker, int(amount)) for amount, ticker in rows]

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
            "The wage takes K/M/B/T — e.g. `Barista | 5K | 0 | 0`. "
            "Reply `cancel` to stop.\n"
            "Then paste the description in your next message.",
        )
        if fields is None:
            await ctx.send("⌛ Timed out waiting for the job details. Try `$postjob` again.")
            return
        if _is_cancel(fields):
            await ctx.send("Cancelled — no job posted.")
            return

        # One mistyped field shouldn't throw away the whole flow: re-ask once,
        # showing what was wrong. A second failure falls through to the
        # centralized error renderer.
        try:
            title, daily_wage, equity_shares, vest_days = _parse_job_fields(fields)
        except BadInput as err:
            retry = await discordutil.prompt_text(
                ctx,
                f"⚠️ {err}\nTry again: "
                "`<title> | <daily wage> | <equity sh, or 0> | <vest days, or 0>` "
                "(or `cancel`).",
            )
            if retry is None:
                await ctx.send("⌛ Timed out waiting for the job details. Try `$postjob` again.")
                return
            if _is_cancel(retry):
                await ctx.send("Cancelled — no job posted.")
                return
            title, daily_wage, equity_shares, vest_days = _parse_job_fields(retry)

        description = await discordutil.prompt_text(
            ctx, "Now paste the job description (or `cancel`)."
        )
        if description is None:
            await ctx.send("⌛ Timed out waiting for the description. Try `$postjob` again.")
            return
        if _is_cancel(description):
            await ctx.send("Cancelled — no job posted.")
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
                f"Job ID {job_id}. Applicants will show in `$applicants {display_ticker}`. "
                "This role stays open and can be used for multiple hires.",
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
        visible = applicants[:DISPLAY_ROWS]
        for i, app in enumerate(visible):
            letter = _LETTERS[i] if i < len(_LETTERS) else str(i + 1)
            lines.append(
                f"{letter}) {mention(app.user_id)} — "
                f"net worth {abbr(app.net_worth)}, "
                f"currently {app.current_label}  "
                f"_(applied for {app.job_title})_"
            )
        first = _LETTERS[0]
        last = (
            _LETTERS[len(visible) - 1]
            if len(visible) <= len(_LETTERS)
            else str(len(visible))
        )
        if len(applicants) > DISPLAY_ROWS:
            lines.append(
                f"… showing {DISPLAY_ROWS} of {len(applicants)} pending applicants; "
                "hire anyone omitted by @mention."
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

    # ---------------------------------------------------------------- promote
    @commands.command(name="promote")
    async def promote(self, ctx: commands.Context, target: str, pct: str) -> None:
        """$promote <@employee> <% raise> — owner-only, raise an employee's wage."""
        target_id = _parse_user_id(target)
        if target_id is None:
            raise BadInput("Mention the employee, e.g. `$promote @user 10`.")
        # Shared parser: `10`, `10%`, and `2.5` all mean the same raise.
        raise_pct = discordutil.parse_percent(pct, label="raise percentage")
        async with self.bot.db.session() as session:
            state = await lookups.get_state(session, ctx.guild.id)
            result = await companies.promote(
                session, state, ctx.author.id, target_id, raise_pct
            )
        await ctx.send(
            embed=discordutil.embed(
                f"{emojis.STOCK_UP} Raise granted",
                f"{mention(result.user_id)} at **{result.ticker}**: "
                f"{fmt(result.old_wage)} → **{fmt(result.new_wage)} nug/day** "
                f"(+{result.pct:g}%). Takes effect at the next tick.",
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
    # The wage goes through the shared money parser, so `5K`/`1.5M` work here
    # exactly as they do in `$print`, `$invest`, and `$dividend`.
    daily_wage = discordutil.parse_amount(parts[1])
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
