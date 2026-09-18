"""Command-surface UX regressions — the Discord-layer papercuts.

These are all things a player hits in the first ten minutes and the engine
suite can never see: percent arguments that used to be rejected, an interactive
flow that threw away your work on one typo, a `$balance` that didn't say when
you get paid, and replies that could have pinged @everyone.

Driven through the no-token :mod:`cog_harness`, like ``test_cogs_e2e``.
"""

from __future__ import annotations

import discord
import pytest

from tendies import discordutil, lookups
from cog_harness import GUILD_ID, Harness, forbidden

# asyncio_mode = "auto" (pyproject) runs the async tests below without a mark.


# ---------------------------------------------------------------------------
# 1. Percent arguments: `10%`, `10`, `0.5`, `1` all reach the service.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('raw', 'shown'), [
    ("10%", "10%"),
    ("10", "10%"),
    ("0.5", "0.5%"),
    ("1", "1%"),
])
async def test_raise_accepts_percent_forms(raw, shown):
    h = await Harness.create()
    try:
        owner = 7000 + int(float(raw.rstrip('%')) * 10)
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args=f'P{int(float(raw.rstrip("%")) * 10):03d} "Pct Co" tech')
        ticker = f'P{int(float(raw.rstrip("%")) * 10):03d}'
        ctx = await h.invoke(h.ctx(owner), "raise", ticker, "500K", raw)
        text = ctx.last_text()
        assert "funding round open" in text
        assert shown in text
    finally:
        await h.close()


async def test_raise_rejects_a_non_percent():
    h = await Harness.create()
    try:
        owner = 7100
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='PBAD "Bad Pct" tech')
        # BadInput is a GameError, so the harness renders it like the bot does.
        ctx = await h.invoke(h.ctx(owner), "raise", "PBAD", "500K", "lots")
        assert "⚠️" in ctx.last_text()
        assert "equity percentage" in ctx.last_text()
    finally:
        await h.close()


@pytest.mark.parametrize('raw', ["50%", "50"])
async def test_promote_accepts_percent_forms(raw):
    h = await Harness.create()
    try:
        owner, worker = 7200 + len(raw), 7300 + len(raw)
        await h.fund_wallet(owner, 1_000_000)
        ticker = f"PR{len(raw)}"
        await h.invoke(h.ctx(owner), "found", args=f'{ticker} "Promo Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("Clerk | 1000 | 0 | 0", "Clerking.")
        await h.invoke(octx, "postjob", ticker)
        job_id = await h.open_job_id(ticker)
        await h.invoke(h.ctx(worker), "apply", job_id)
        await h.invoke(h.ctx(owner), "hire", ticker, "a")

        ctx = await h.invoke(h.ctx(owner), "promote", f"<@{worker}>", raw)
        text = ctx.last_text()
        assert "Raise granted" in text
        assert "1,500" in text  # 1000 -> +50%
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# 2. $postjob: K/M/B/T wages, `cancel`, and one free retry.
# ---------------------------------------------------------------------------

async def test_postjob_wage_accepts_k_suffix():
    h = await Harness.create()
    try:
        owner = 7400
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='SUFX "Suffix Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("Engineer | 5K | 0 | 0", "Builds things.")
        await h.invoke(octx, "postjob", "SUFX")
        text = octx.all_text()
        assert "Posted" in text
        assert "5,000 nug/day" in text
    finally:
        await h.close()


@pytest.mark.parametrize('stage', ["fields", "description"])
async def test_postjob_cancel_at_any_prompt(stage):
    h = await Harness.create()
    try:
        owner = 7500 + len(stage)
        ticker = f"CN{len(stage)}"
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args=f'{ticker} "Cancel Co" tech')
        octx = h.ctx(owner)
        if stage == "fields":
            h.bot.queue("cancel")
        else:
            h.bot.queue("Engineer | 5K | 0 | 0", "CANCEL")
        await h.invoke(octx, "postjob", ticker)
        assert "Cancelled" in octx.all_text()

        async with h.db.session() as session:
            co = await lookups.get_company(session, GUILD_ID, ticker)
            jobs = await h.registry["company"][0]._open_jobs(session, co.id)
        assert jobs == []
    finally:
        await h.close()


async def test_postjob_reprompts_once_after_a_bad_field_line():
    h = await Harness.create()
    try:
        owner = 7600
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='RTRY "Retry Co" tech')
        octx = h.ctx(owner)
        # First reply is malformed; the second one is accepted.
        h.bot.queue("Engineer | not-a-wage | 0 | 0",
                    "Engineer | 5K | 0 | 0",
                    "Builds things.")
        await h.invoke(octx, "postjob", "RTRY")
        text = octx.all_text()
        assert "Try again" in text  # the re-prompt, not an abort
        assert "Posted" in text
        assert "5,000 nug/day" in text
    finally:
        await h.close()


async def test_prompt_text_ignores_a_command_typed_mid_flow():
    h = await Harness.create()
    try:
        owner = 7650
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='SKIP "Skip Co" tech')
        octx = h.ctx(owner)
        # "$help" is someone asking for help, not a job title — keep waiting.
        h.bot.queue("$help", "Engineer | 5K | 0 | 0", "Builds things.")
        await h.invoke(octx, "postjob", "SKIP")
        text = octx.all_text()
        assert "Posted" in text
        assert "Engineer" in text
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# 3. $company: job descriptions and inbound acquisition offers.
# ---------------------------------------------------------------------------

async def test_company_card_shows_job_description_and_inbound_offer():
    h = await Harness.create()
    try:
        target_owner, buyer_owner = 7700, 7701
        await h.fund_wallet(target_owner, 1_000_000)
        await h.fund_wallet(buyer_owner, 1_000_000)
        await h.invoke(h.ctx(target_owner), "found", args='TGT "Target Co" tech')
        await h.invoke(h.ctx(buyer_owner), "found", args='BUY "Buyer Co" tech')
        await h.fund_treasury("BUY", 5_000_000)

        octx = h.ctx(target_owner)
        h.bot.queue("Engineer | 5K | 0 | 0", "Writes the firmware that runs the fryer.")
        await h.invoke(octx, "postjob", "TGT")

        await h.invoke(h.ctx(buyer_owner), "acquire", "BUY", "TGT", "1M")

        # The target's owner sees both the role blurb and the pending offer.
        ctx = await h.invoke(h.ctx(target_owner), "company", "TGT")
        text = ctx.last_text()
        assert "Open jobs" in text
        assert "Writes the firmware" in text
        assert "Pending acquisition offers" in text
        assert "BUY" in text and "1,000,000" in text

        # A stranger sees the jobs but not the offer.
        other = await h.invoke(h.ctx(7702), "company", "TGT")
        assert "Writes the firmware" in other.last_text()
        assert "Pending acquisition offers" not in other.last_text()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# 4. $balance: the onboarding facts.
# ---------------------------------------------------------------------------

async def test_balance_shows_wage_clockin_streak_and_next_close():
    h = await Harness.create(weekday="monday")
    try:
        worker = 7800
        # Unemployed: still told where to look, and when the close is.
        ctx = await h.invoke(h.ctx(worker), "balance")
        text = ctx.last_text()
        assert "Not employed yet" in text
        assert "Clock-in streak: **0**" in text
        assert "Next close: <t:" in text

        job_id = await h.first_state_job_id()
        await h.invoke(h.ctx(worker), "apply", job_id)

        ctx = await h.invoke(h.ctx(worker), "balance")
        text = ctx.last_text()
        assert "Daily wage:" in text
        assert "Not clocked in today" in text

        h.bot.queue(False)  # decline the first-clock-in reminder prompt
        await h.invoke(h.ctx(worker), "clockin")
        ctx = await h.invoke(h.ctx(worker), "balance")
        text = ctx.last_text()
        assert "Clocked in today" in text
        assert "Clock-in streak: **1**" in text
    finally:
        await h.close()


async def test_balance_reports_earnings_from_the_last_close():
    h = await Harness.create(weekday="monday")
    try:
        worker = 7810
        job_id = await h.first_state_job_id()
        await h.invoke(h.ctx(worker), "apply", job_id)
        h.bot.queue(False)
        await h.invoke(h.ctx(worker), "clockin")

        # Before a close there is nothing to report.
        assert "Earned at last close" not in (
            await h.invoke(h.ctx(worker), "balance")
        ).last_text()

        await h.invoke(h.ctx(1, manager=True), "forcetick")

        text = (await h.invoke(h.ctx(worker), "balance")).last_text()
        assert "Earned at last close" in text
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# 5. $jobs: the "(applied)" marker.
# ---------------------------------------------------------------------------

async def test_jobs_marks_positions_you_already_applied_to():
    h = await Harness.create()
    try:
        owner, worker = 7900, 7901
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='APPL "Applied Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("Engineer | 5K | 0 | 0", "Builds things.")
        await h.invoke(octx, "postjob", "APPL")
        job_id = await h.open_job_id("APPL")

        before = await h.invoke(h.ctx(worker), "jobs")
        assert "(applied)" not in before.last_text()

        await h.invoke(h.ctx(worker), "apply", job_id)
        after = await h.invoke(h.ctx(worker), "jobs")
        assert "(applied)" in after.last_text()

        # Only for the applicant — someone else still sees a plain listing.
        assert "(applied)" not in (await h.invoke(h.ctx(7902), "jobs")).last_text()
    finally:
        await h.close()


async def test_apply_pings_the_owner_with_a_scoped_allowed_mentions():
    h = await Harness.create()
    try:
        owner, worker = 7950, 7951
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='PING "Ping Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("Engineer | 5K | 0 | 0", "Builds things.")
        await h.invoke(octx, "postjob", "PING")
        job_id = await h.open_job_id("PING")

        wctx = await h.invoke(h.ctx(worker), "apply", job_id)
        msg = wctx.last
        assert f"<@{owner}>" in (msg.content or "")
        allowed = msg.kwargs.get("allowed_mentions")
        assert allowed is not None
        assert [u.id for u in allowed.users] == [owner]
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# 6. $market: the "Open rounds" section.
# ---------------------------------------------------------------------------

async def test_market_lists_open_funding_rounds():
    h = await Harness.create()
    try:
        owner = 8000
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='ROND "Round Co" tech')

        before = await h.invoke(h.ctx(owner), "market")
        assert "Open rounds" not in before.last_text()

        await h.invoke(h.ctx(owner), "raise", "ROND", "500K", "10%")

        ctx = await h.invoke(h.ctx(owner), "market")
        text = ctx.last_text()
        assert "Open rounds" in text
        assert "ROND" in text
        assert "500,000" in text  # target and remaining
        assert "10%" in text
        assert "$invest ROND" in text
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# 7. Missing-argument usage hints.
# ---------------------------------------------------------------------------

async def test_missing_argument_gets_a_usage_hint():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(8100), "company")
        text = ctx.last_text()
        assert "Usage: `$company <ticker>`" in text
        assert "$help company" in text
    finally:
        await h.close()


def test_usage_hint_covers_every_catalogued_command():
    """`$help` and the error handler read the same table, so a command can
    never have a detail page but no usage hint."""
    from tendies import help_menu

    for name in help_menu.SUMMARY:
        assert name in help_menu.USAGE


# ---------------------------------------------------------------------------
# 8. Mention safety + permission fallbacks.
# ---------------------------------------------------------------------------

def test_bot_suppresses_mentions_globally():
    """`$company @everyone` echoes the input; it must never ping."""
    from tendies.bot import TendiesBot
    from tendies.config import Settings

    settings = Settings(
        discord_token="x",
        database_url="sqlite+aiosqlite:///:memory:",
        command_prefix="$",
        manager_role="Tendies Manager",
        tick_interval_seconds=86400,
    )
    bot = TendiesBot(settings=settings)
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False


async def test_confirm_falls_back_to_typed_yes_without_add_reactions():
    h = await Harness.create()
    try:
        ctx = h.ctx(8200, manager=True, can_react=False)
        h.bot.queue("yes")
        await h.invoke(ctx, "print", "1000")
        text = ctx.all_text()
        assert "Add Reactions" in text
        assert "Money printed" in text
    finally:
        await h.close()


async def test_confirm_typed_no_cancels():
    h = await Harness.create()
    try:
        ctx = h.ctx(8201, manager=True, can_react=False)
        h.bot.queue("no")
        await h.invoke(ctx, "print", "1000")
        assert "Print cancelled" in ctx.all_text()
    finally:
        await h.close()


def test_forbidden_helper_builds_a_real_discord_error():
    """Guards the harness fake: it must be the exception the cogs catch."""
    assert isinstance(forbidden(), discord.Forbidden)


def test_manager_role_match_is_case_and_space_insensitive():
    class _Bot:
        settings = type("S", (), {"manager_role": "Tendies Manager"})()

    class _Role:
        def __init__(self, name):
            self.name = name

    class _Author:
        guild_permissions = None

        def __init__(self, role_name):
            self.roles = [_Role(role_name)]

    class _Ctx:
        def __init__(self, role_name):
            self.bot = _Bot()
            self.author = _Author(role_name)

    assert discordutil.is_manager(_Ctx("  tendies manager "))
    assert discordutil.is_manager(_Ctx("TENDIES MANAGER"))
    assert not discordutil.is_manager(_Ctx("Tendies Managers"))


# ---------------------------------------------------------------------------
# 8. The clock-in reminder ping must survive the bot-wide mention suppression.
# ---------------------------------------------------------------------------

async def test_clockin_reminder_opts_back_in_to_pinging_its_targets():
    """``bot.allowed_mentions = none()`` silences every send by default; the
    opt-in reminder is worthless if its ``<@uid>`` mentions don't ping."""
    from types import SimpleNamespace

    from tendies.scheduler import TickScheduler

    sent: list[tuple[str, dict]] = []

    class _Channel:
        def permissions_for(self, _me):
            return SimpleNamespace(send_messages=True)

        async def send(self, content=None, **kwargs):
            sent.append((content, kwargs))

    channel = _Channel()
    guild = SimpleNamespace(me=object(), system_channel=channel, text_channels=[channel])
    settings = SimpleNamespace(
        calendar_timezone=None, accelerated_mode=False,
        tick_interval_seconds=5, command_prefix="$",
    )
    bot = SimpleNamespace(db=None, settings=settings, get_guild=lambda _gid: guild)
    scheduler = TickScheduler(bot)

    user_ids = [4001, 4002]
    await scheduler._ping_forgetful(GUILD_ID, user_ids)

    assert len(sent) == 1
    content, kwargs = sent[0]
    assert "<@4001>" in content and "<@4002>" in content
    allowed = kwargs.get("allowed_mentions")
    assert allowed is not None, "the reminder must opt back in to mentions"
    assert [u.id for u in allowed.users] == user_ids
    assert allowed.everyone in (False, None)


# ---------------------------------------------------------------------------
# 9. prompt_text: skip real commands, not every reply that starts with `$`.
# ---------------------------------------------------------------------------

async def test_prompt_text_accepts_a_reply_that_starts_with_the_prefix():
    """``$5K/day plus equity`` is a job description, not a command. Skipping it
    on the prefix alone hung the flow until the 120s timeout."""
    h = await Harness.create()
    try:
        owner = 7660
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='DOLR "Dollar Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("Engineer | 5K | 0 | 0", "$5K/day plus equity")
        await h.invoke(octx, "postjob", "DOLR")
        text = octx.all_text()
        assert "Posted" in text
        assert "still waiting for your reply" not in text
    finally:
        await h.close()


async def test_prompt_text_says_it_is_still_waiting_when_it_skips_a_command():
    h = await Harness.create()
    try:
        owner = 7670
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='WAIT "Wait Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("$balance", "Engineer | 5K | 0 | 0", "Builds things.")
        await h.invoke(octx, "postjob", "WAIT")
        text = octx.all_text()
        assert "still waiting for your reply" in text
        assert "Posted" in text
    finally:
        await h.close()


async def test_balance_ignores_a_bonus_paid_on_the_still_open_day():
    """A streak milestone is credited at clock-in, on the *current* (unsettled)
    date. The card must keep reporting the last day that actually closed."""
    import datetime as dt

    from sqlalchemy import select

    from tendies.models import PlayerProfile, ServerState, Transaction

    h = await Harness.create(weekday="monday")
    try:
        worker = 7820
        job_id = await h.first_state_job_id()
        await h.invoke(h.ctx(worker), "apply", job_id)
        h.bot.queue(False)
        await h.invoke(h.ctx(worker), "clockin")
        await h.invoke(h.ctx(1, manager=True), "forcetick")  # settles Monday

        async with h.db.session() as session:
            state = (await session.execute(
                select(ServerState).where(ServerState.guild_id == GUILD_ID)
            )).scalars().one()
            monday = state.game_day - dt.timedelta(days=1)
            profile = (await session.execute(
                select(PlayerProfile).where(
                    PlayerProfile.guild_id == GUILD_ID,
                    PlayerProfile.user_id == worker,
                )
            )).scalars().one()
            # One short of the 10-day milestone, as of yesterday's close.
            profile.clockin_streak = 9
            profile.last_clockin_day = monday
            profile.milestone_claimed = 0

        await h.invoke(h.ctx(worker), "clockin")  # Tuesday: pays the bonus

        async with h.db.session() as session:
            tuesday = (await session.execute(
                select(ServerState).where(ServerState.guild_id == GUILD_ID)
            )).scalars().one().game_day
            bonus_day = await session.scalar(
                select(Transaction.game_day).where(
                    Transaction.guild_id == GUILD_ID,
                    Transaction.user_id == worker,
                    Transaction.type == "streak_bonus",
                )
            )
            assert bonus_day == tuesday, "the bonus lands on the open day"

        text = (await h.invoke(h.ctx(worker), "balance")).last_text()
        assert "Earned at last close (Mon)" in text
        assert "Earned at last close (Tue)" not in text
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# 10. on_command_error: a Forbidden raised while reporting must not escape.
# ---------------------------------------------------------------------------

def _error_handler_bot():
    """A duck-typed stand-in wired to the *real* ``TendiesBot`` handlers."""
    from types import SimpleNamespace

    from tendies.bot import TendiesBot

    class _Bot:
        settings = SimpleNamespace(command_prefix="$")
        usage_hint = TendiesBot.usage_hint
        _report_forbidden = TendiesBot._report_forbidden
        on_command_error = TendiesBot.on_command_error

    return _Bot()


class _FlakyCtx:
    """``send`` raises Forbidden for the first ``fails`` calls, then records."""

    def __init__(self, fails: int):
        self._left = fails
        self.sent: list[str] = []
        self.channel = "#general"
        self.command = None

    async def send(self, content=None, **kwargs):
        if self._left > 0:
            self._left -= 1
            raise forbidden()
        self.sent.append(content)


async def test_game_error_reply_blocked_by_permissions_falls_back_to_the_hint():
    from tendies.errors import GameError

    bot = _error_handler_bot()
    ctx = _FlakyCtx(fails=1)
    await bot.on_command_error(ctx, GameError("you can't do that"))

    assert len(ctx.sent) == 1
    assert "Send Messages" in ctx.sent[0]
    assert "Embed Links" in ctx.sent[0]
    assert "Add Reactions" in ctx.sent[0]


async def test_a_totally_muted_channel_stays_silent_without_recursing():
    from tendies.errors import GameError

    bot = _error_handler_bot()
    ctx = _FlakyCtx(fails=99)
    await bot.on_command_error(ctx, GameError("you can't do that"))
    assert ctx.sent == []
