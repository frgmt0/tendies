"""End-to-end cog tests — the Discord command surface, driven by a dummy user.

These run the real cogs through the no-token :mod:`cog_harness`, covering the
~half of the codebase the engine suite never touches: argument parsing, service
wiring, embed rendering, the confirm/prompt flows, Manager gating, and the
centralized ``GameError`` → ``"⚠️ …"`` rendering. Money conservation is
re-checked at the command layer (the supply must equal the starting pool through
a full playthrough, since no command but ``$print`` mints).

``conftest`` is not used here (it forbids importing ``discord``); the harness
owns its own DB.
"""

from __future__ import annotations

import pytest

from tendies import config, emojis
from tendies.config import STARTING_POOL

from cog_harness import GUILD_ID, Harness

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Info commands
# ---------------------------------------------------------------------------

async def test_pool_and_today_render():
    h = await Harness.create(weekday="monday")
    try:
        ctx = await h.invoke(h.ctx(1), "pool")
        text = ctx.last_text()
        assert emojis.TREASURY_POOL in text
        assert "Server Treasury" in text
        # Freshly bootstrapped: pool == supply == starting pool.
        assert f"{STARTING_POOL:,}" in text.replace("nug", "").strip() or "1,000,000,000,000" in text
        assert await h.money_supply() == STARTING_POOL

        ctx = await h.invoke(h.ctx(1), "today")
        assert "OPEN" in ctx.last_text()
    finally:
        await h.close()


async def test_today_closed_on_weekend():
    h = await Harness.create(weekday="saturday")
    try:
        ctx = await h.invoke(h.ctx(1), "today")
        assert "CLOSED" in ctx.last_text()
    finally:
        await h.close()


async def test_market_empty_then_open_header():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(1), "market")
        text = ctx.last_text()
        assert emojis.STOCK_UP in text  # exchange header
        assert "No private companies yet" in text
    finally:
        await h.close()


async def test_leaderboard_empty():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(1), "leaderboard")
        text = ctx.last_text()
        assert emojis.LEADERBOARD in text
        assert "Nobody's on the board" in text
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# Player lifecycle: broke -> state job -> clock in -> tick -> paid
# ---------------------------------------------------------------------------

async def test_balance_broke_then_state_job_paid_at_tick():
    h = await Harness.create()
    try:
        uid = 100
        ctx = await h.invoke(h.ctx(uid), "balance")
        assert "0 nug" in ctx.last_text()
        assert "No holdings" in ctx.last_text()

        # State jobs are listed.
        ctx = await h.invoke(h.ctx(uid), "jobs")
        assert emojis.HIRING in ctx.last_text()
        assert "STATE" in ctx.last_text()

        # Apply to a state job -> auto-accepted.
        job_id = await h.first_state_job_id()
        ctx = await h.invoke(h.ctx(uid), "apply", job_id)
        assert "Hired" in ctx.last_text()
        assert emojis.HIRING in ctx.last_text()

        # Clock in (first time also offers the reminder opt-in), then tick.
        ctx = h.ctx(uid)
        h.bot.queue(True)  # accept the one-time reminder prompt
        await h.invoke(ctx, "clockin")
        assert emojis.CLOCK_IN in ctx.all_text()
        assert "Clocked in" in ctx.all_text()

        await h.invoke(h.ctx(9, manager=True), "forcetick")

        # Wage was paid (net of tax) and conservation still holds.
        ctx = await h.invoke(h.ctx(uid), "balance")
        bal_text = ctx.last_text()
        assert "0 nug (real)" not in bal_text.splitlines()[1]  # wallet line is no longer zero
        assert await h.money_supply() == STARTING_POOL
    finally:
        await h.close()


async def test_clockin_first_time_offers_reminder_and_shows_streak():
    h = await Harness.create()
    try:
        uid = 720
        jid = await h.first_state_job_id()
        await h.invoke(h.ctx(uid), "apply", jid)

        ctx = h.ctx(uid)
        h.bot.queue(True)  # react ✅ to the opt-in question
        await h.invoke(ctx, "clockin")
        text = ctx.all_text()
        assert "Clocked in" in text
        assert "streak" in text.lower()
        assert "ping you" in text  # the opt-in question was asked
        assert "You're in" in text  # opted in

        # Opt-in persisted on the player's profile.
        from tendies.models import PlayerProfile
        async with h.db.session() as s:
            p = await s.get(PlayerProfile, (GUILD_ID, uid))
            assert p.reminder_opt_in is True
            assert p.reminder_prompted is True
            assert p.clockin_streak == 1
    finally:
        await h.close()


async def test_clockin_reminder_decline_is_honored():
    h = await Harness.create()
    try:
        uid = 721
        jid = await h.first_state_job_id()
        await h.invoke(h.ctx(uid), "apply", jid)
        ctx = h.ctx(uid)
        h.bot.queue(False)  # ignore the ✅ (timeout)
        await h.invoke(ctx, "clockin")
        assert "No pings" in ctx.all_text()
        from tendies.models import PlayerProfile
        async with h.db.session() as s:
            p = await s.get(PlayerProfile, (GUILD_ID, uid))
            assert p.reminder_opt_in is False
            assert p.reminder_prompted is True  # won't be asked again
    finally:
        await h.close()


async def test_reminders_toggle_command():
    h = await Harness.create()
    try:
        uid = 722
        ctx = await h.invoke(h.ctx(uid), "reminders", "on")
        assert "ON" in ctx.last_text()
        ctx = await h.invoke(h.ctx(uid), "reminders", "off")
        assert "OFF" in ctx.last_text()
        ctx = await h.invoke(h.ctx(uid), "reminders", "wat")
        assert "reminders on" in ctx.last_text()
    finally:
        await h.close()


async def test_promote_raises_employee_wage():
    h = await Harness.create()
    try:
        owner, worker = 730, 731
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='PROM "Promo Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("Worker | 1000 | 0 | 0", "Do work.")
        await h.invoke(octx, "postjob", "PROM")
        job_id = await h.open_job_id("PROM")
        await h.invoke(h.ctx(worker), "apply", job_id)
        apps_ctx = await h.invoke(h.ctx(owner), "applicants", "PROM")  # noqa: F841
        await h.invoke(h.ctx(owner), "hire", "PROM", "a")

        ctx = await h.invoke(h.ctx(owner), "promote", f"<@{worker}>", 50.0)
        text = ctx.last_text()
        assert emojis.STOCK_UP in text
        assert "Raise granted" in text
        assert "1,500" in text  # 1000 +50%
    finally:
        await h.close()


async def test_promote_rejected_for_non_owner():
    h = await Harness.create()
    try:
        owner, worker, stranger = 740, 741, 742
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='OWNS "Owns Co" tech')
        octx = h.ctx(owner)
        h.bot.queue("Worker | 1000 | 0 | 0", "Do work.")
        await h.invoke(octx, "postjob", "OWNS")
        job_id = await h.open_job_id("OWNS")
        await h.invoke(h.ctx(worker), "apply", job_id)
        await h.invoke(h.ctx(owner), "hire", "OWNS", "a")

        ctx = await h.invoke(h.ctx(stranger), "promote", f"<@{worker}>", 50.0)
        assert "⚠️" in ctx.last_text()
    finally:
        await h.close()


async def test_clockin_twice_is_idempotent():
    h = await Harness.create()
    try:
        uid = 101
        job_id = await h.first_state_job_id()
        await h.invoke(h.ctx(uid), "apply", job_id)
        await h.invoke(h.ctx(uid), "clockin")
        ctx = await h.invoke(h.ctx(uid), "clockin")
        assert "Already clocked in" in ctx.last_text()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# Founding & company detail
# ---------------------------------------------------------------------------

async def test_found_company_charges_fee_and_renders():
    h = await Harness.create()
    try:
        founder = 200
        await h.fund_wallet(founder, 1_000_000)
        pool_before = await h.pool_balance()

        ctx = await h.invoke(
            h.ctx(founder), "found", args='MOON "Moon Mining Inc" materials'
        )
        text = ctx.last_text()
        assert emojis.FACTORY in text
        assert emojis.INDUSTRY_MATERIALS in text  # routed industry glyph
        assert "Moon Mining Inc" in text and "MOON" in text
        assert "100%" in text

        # Fee went to the pool.
        assert await h.pool_balance() == pool_before + config.BASE_FOUNDING_FEE
        assert await h.money_supply() == STARTING_POOL

        # $company shows the cap table at 100%.
        ctx = await h.invoke(h.ctx(founder), "company", "MOON")
        detail = ctx.last_text()
        assert "Moon Mining Inc" in detail
        assert "Owner:" in detail
        assert emojis.INDUSTRY_MATERIALS in detail
    finally:
        await h.close()


async def test_found_bad_args_renders_usage():
    h = await Harness.create()
    try:
        await h.fund_wallet(201, 1_000_000)
        ctx = await h.invoke(h.ctx(201), "found", args="JUSTONE")
        assert "⚠️" in ctx.last_text()
        assert "Usage" in ctx.last_text()
    finally:
        await h.close()


async def test_company_unknown_ticker_errors():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(1), "company", "NOPE")
        assert "⚠️" in ctx.last_text()
    finally:
        await h.close()


async def test_found_defense_and_consumer_industries():
    """The two industries added for the custom emojis resolve and render."""
    h = await Harness.create()
    try:
        await h.fund_wallet(210, 5_000_000)
        ctx = await h.invoke(h.ctx(210), "found", args='ARMS "Arms Co" defense')
        assert emojis.INDUSTRY_DEFENSE in ctx.last_text()

        await h.fund_wallet(211, 5_000_000)
        ctx = await h.invoke(h.ctx(211), "found", args='SHOP "Shop Co" retail')
        # 'retail' is an alias -> consumer
        assert emojis.INDUSTRY_CONSUMER in ctx.last_text()
        assert "Consumer" in ctx.last_text()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# Hiring flow: postjob (interactive) -> apply -> applicants -> hire -> fire
# ---------------------------------------------------------------------------

async def test_postjob_hire_and_fire_flow():
    h = await Harness.create()
    try:
        owner, worker = 300, 301
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='TEND "Tendies Inc" food')

        # $postjob is interactive: two prompt_text replies.
        octx = h.ctx(owner)
        h.bot.queue("Fry Cook | 500 | 0 | 0", "Make the tendies.")
        await h.invoke(octx, "postjob", "TEND")
        assert emojis.HIRING in octx.all_text()
        assert "Fry Cook" in octx.all_text()

        # Worker applies to the private job (queued, not auto-accepted).
        job_id = await h.open_job_id("TEND")
        wctx = await h.invoke(h.ctx(worker), "apply", job_id)
        assert "Application filed" in wctx.last_text()

        # Owner sees the applicant and hires by letter.
        octx = await h.invoke(h.ctx(owner), "applicants", "TEND")
        assert "a)" in octx.last_text()
        octx = await h.invoke(h.ctx(owner), "hire", "TEND", "a")
        assert "Hired" in octx.last_text()
        assert emojis.HIRING in octx.last_text()

        # Owner fires the worker by mention.
        octx = await h.invoke(h.ctx(owner), "fire", "TEND", f"<@{worker}>")
        assert emojis.FIRED in octx.last_text()
        assert "no longer works" in octx.last_text()

        assert await h.money_supply() == STARTING_POOL
    finally:
        await h.close()


async def test_postjob_bad_field_count_errors():
    h = await Harness.create()
    try:
        owner = 310
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='BAD "Bad Co" food')
        octx = h.ctx(owner)
        h.bot.queue("only two | fields")  # not 4 fields -> BadInput
        await h.invoke(octx, "postjob", "BAD")
        assert "⚠️" in octx.all_text()
        assert "4 fields" in octx.all_text()
    finally:
        await h.close()


async def test_postjob_prompt_timeout():
    h = await Harness.create()
    try:
        owner = 311
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='TIME "Time Co" food')
        octx = h.ctx(owner)
        h.bot.queue(None)  # prompt_text times out
        await h.invoke(octx, "postjob", "TIME")
        assert "Timed out" in octx.all_text()
    finally:
        await h.close()


async def test_postjob_non_owner_rejected():
    h = await Harness.create()
    try:
        owner, stranger = 320, 321
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='OWN "Owned Co" food')
        sctx = await h.invoke(h.ctx(stranger), "postjob", "OWN")
        assert "⚠️" in sctx.all_text()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# Capital markets: raise / invest gate / dividend / acquisition
# ---------------------------------------------------------------------------

async def test_raise_round_renders():
    h = await Harness.create()
    try:
        owner = 400
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='RAYZ "Raise Co" tech')
        ctx = await h.invoke(h.ctx(owner), "raise", "RAYZ", "500K", 10.0)
        text = ctx.last_text()
        assert emojis.STOCK_UP in text
        assert "funding round open" in text
        assert "500,000" in text
    finally:
        await h.close()


async def test_invest_blocked_by_accredited_gate():
    h = await Harness.create()
    try:
        owner, investor = 410, 411
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='GATE "Gate Co" tech')
        await h.invoke(h.ctx(owner), "raise", "GATE", "500K", 10.0)

        # A funded-but-not-earning investor is NOT accredited.
        await h.fund_wallet(investor, 1_000_000)
        ctx = await h.invoke(h.ctx(investor), "invest", "GATE", "100K")
        text = ctx.last_text()
        assert "⚠️" in text
        assert "Accredited investors only" in text
    finally:
        await h.close()


async def test_dividend_pays_pro_rata():
    h = await Harness.create()
    try:
        owner = 420
        await h.fund_wallet(owner, 1_000_000)
        await h.invoke(h.ctx(owner), "found", args='DIVY "Div Co" finance')
        await h.fund_treasury("DIVY", 1_000_000)  # give the treasury something to pay

        ctx = await h.invoke(h.ctx(owner), "dividend", "DIVY", "100K")
        text = ctx.last_text()
        assert emojis.DIVIDEND in text
        assert "Dividend paid" in text
        assert "Tax → pool" in text
        assert await h.money_supply() == STARTING_POOL
    finally:
        await h.close()


async def test_acquisition_offer_accept_flow():
    h = await Harness.create()
    try:
        buyer_owner, target_owner = 430, 431
        await h.fund_wallet(buyer_owner, 2_000_000)
        await h.fund_wallet(target_owner, 2_000_000)
        await h.invoke(h.ctx(buyer_owner), "found", args='BUYR "Buyer Co" tech')
        await h.invoke(h.ctx(target_owner), "found", args='TARG "Target Co" tech')
        await h.fund_treasury("BUYR", 5_000_000)  # buyer pays from treasury

        # Offer from buyer's owner.
        octx = await h.invoke(
            h.ctx(buyer_owner), "acquire", "BUYR", "TARG", "1M"
        )
        assert emojis.ACQUISITION in octx.last_text()
        assert "Acquisition offer" in octx.last_text()

        # Target owner accepts (keyed by acquirer ticker).
        actx = await h.invoke(h.ctx(target_owner), "accept", "BUYR")
        text = actx.last_text()
        assert "Deal closed" in text
        assert emojis.ACQUISITION in text

        assert await h.money_supply() == STARTING_POOL
    finally:
        await h.close()


async def test_acquisition_decline():
    h = await Harness.create()
    try:
        a, b = 440, 441
        await h.fund_wallet(a, 2_000_000)
        await h.fund_wallet(b, 2_000_000)
        await h.invoke(h.ctx(a), "found", args='AAA "A Co" tech')
        await h.invoke(h.ctx(b), "found", args='BBB "B Co" tech')
        await h.fund_treasury("AAA", 5_000_000)
        await h.invoke(h.ctx(a), "acquire", "AAA", "BBB", "1M")
        ctx = await h.invoke(h.ctx(b), "decline", "AAA")
        assert "declined" in ctx.last_text().lower()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# Manager / admin commands + gating
# ---------------------------------------------------------------------------

async def test_print_requires_manager():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(500), "print", "1B")  # not a manager
        assert "Manager only" in ctx.last_text()
        # Nothing minted.
        assert await h.money_supply() == STARTING_POOL
    finally:
        await h.close()


async def test_print_confirm_yes_mints_and_inflates():
    h = await Harness.create()
    try:
        ctx = h.ctx(9, manager=True)
        h.bot.queue(True)  # confirm ✅
        await h.invoke(ctx, "print", "1B")
        assert "Money printed" in ctx.all_text()
        assert emojis.MONEY_PRINTER in ctx.all_text()
        assert await h.money_supply() == STARTING_POOL + 1_000_000_000
    finally:
        await h.close()


async def test_print_confirm_no_cancels():
    h = await Harness.create()
    try:
        ctx = h.ctx(9, manager=True)
        h.bot.queue(False)  # timeout / no reaction
        await h.invoke(ctx, "print", "1B")
        assert "cancelled" in ctx.all_text().lower()
        assert await h.money_supply() == STARTING_POOL
    finally:
        await h.close()


async def test_stats_requires_manager():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(600), "stats")  # not a manager
        assert "Manager only" in ctx.last_text()
    finally:
        await h.close()


async def test_stats_dashboard_reflects_real_activity():
    h = await Harness.create()
    try:
        # One state worker clocks in, one founder builds a company, then tick.
        jid = await h.first_state_job_id()
        await h.invoke(h.ctx(610), "apply", jid)
        await h.invoke(h.ctx(610), "clockin")
        await h.fund_wallet(611, 2_000_000)
        await h.invoke(h.ctx(611), "found", args='STAT "Stat Co" tech')
        # Manager prints, then advances a day so flows are non-zero.
        pctx = h.ctx(9, manager=True)
        h.bot.queue(True)
        await h.invoke(pctx, "print", "1B")
        await h.invoke(h.ctx(9, manager=True), "forcetick")

        ctx = await h.invoke(h.ctx(9, manager=True), "stats")
        text = ctx.last_text()
        assert "Macro dashboard" in text
        assert "Money supply" in text
        assert "Gini" in text
        assert "Recession cap" in text
        # The founded company shows in the industry mix (tech glyph).
        assert emojis.INDUSTRY_TECH in text
        # Minting is reflected in money supply.
        assert await h.money_supply() == STARTING_POOL + 1_000_000_000
    finally:
        await h.close()


async def test_taxrate_set():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(9, manager=True), "taxrate", "25%")
        assert emojis.TREASURY_POOL in ctx.last_text()
        assert "25.0%" in ctx.last_text()
    finally:
        await h.close()


async def test_event_industry_and_market_wide():
    h = await Harness.create()
    try:
        # Industry event.
        ctx = await h.invoke(
            h.ctx(9, manager=True), "event", raw='energy 0.4 "Pipeline rupture"'
        )
        text = ctx.last_text()
        assert emojis.BREAKING_NEWS in text
        assert emojis.INDUSTRY_ENERGY in text
        assert "×0.4" in text

        # Market-wide event uses the crash glyph.
        ctx = await h.invoke(
            h.ctx(9, manager=True), "event", raw='all 0.5 "Black Monday"'
        )
        assert emojis.STOCK_DOWN in ctx.last_text()
        assert "every industry" in ctx.last_text()
    finally:
        await h.close()


async def test_event_multiplier_clamped_to_sane_range():
    h = await Harness.create()
    try:
        ctx = await h.invoke(
            h.ctx(9, manager=True), "event", raw='tech 100000 "Singularity"'
        )
        assert "⚠️" in ctx.last_text()
        assert "unrealistically large" in ctx.last_text()
    finally:
        await h.close()


async def test_event_bad_industry_errors():
    h = await Harness.create()
    try:
        ctx = await h.invoke(
            h.ctx(9, manager=True), "event", raw='pottery 2 "Kiln boom"'
        )
        assert "⚠️" in ctx.last_text()
        assert "Unknown industry" in ctx.last_text()
    finally:
        await h.close()


async def test_setday_corrects_calendar():
    h = await Harness.create()
    try:
        ctx = await h.invoke(h.ctx(9, manager=True), "setday", "friday")
        assert "Friday" in ctx.last_text()
    finally:
        await h.close()


async def test_forcetick_closed_on_weekend():
    h = await Harness.create(weekday="friday")
    try:
        # Friday settles first; the following Saturday close is empty.
        friday = await h.invoke(h.ctx(9, manager=True), "forcetick")
        assert "Daily close — Friday" in friday.last_text()
        ctx = await h.invoke(h.ctx(9, manager=True), "forcetick")
        assert "Closed tick" in ctx.last_text()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# Parser edge cases at the command layer
# ---------------------------------------------------------------------------

async def test_amount_parser_rejects_misgrouped_commas():
    h = await Harness.create()
    try:
        await h.fund_wallet(600, 1_000_000)
        await h.invoke(h.ctx(600), "found", args='AMT "Amt Co" tech')
        ctx = await h.invoke(h.ctx(600), "raise", "AMT", "1,5M", 10.0)
        assert "⚠️" in ctx.last_text()
        assert await h.money_supply() == STARTING_POOL
    finally:
        await h.close()


async def test_full_playthrough_conserves_money():
    """A mixed sequence of real commands must never create or destroy nuggies."""
    h = await Harness.create()
    try:
        # Two players take state jobs and clock in.
        for uid in (700, 701):
            jid = await h.first_state_job_id()
            await h.invoke(h.ctx(uid), "apply", jid)
            await h.invoke(h.ctx(uid), "clockin")
        # A founder builds a company and pays a dividend.
        await h.fund_wallet(702, 2_000_000)
        await h.invoke(h.ctx(702), "found", args='PLAY "Play Co" tech')
        await h.fund_treasury("PLAY", 500_000)
        await h.invoke(h.ctx(702), "dividend", "PLAY", "50K")
        # A manager prints and the day ticks.
        pctx = h.ctx(9, manager=True)
        h.bot.queue(True)
        await h.invoke(pctx, "print", "2B")
        await h.invoke(h.ctx(9, manager=True), "forcetick")

        # Supply == starting pool + exactly what was printed.
        assert await h.money_supply() == STARTING_POOL + 2_000_000_000
    finally:
        await h.close()
