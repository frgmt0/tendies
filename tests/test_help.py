"""Tests for the custom ``$help`` menu — both the pure builders and the live,
server-aware ``$help found`` fee ladder driven through the real ``TendiesHelp``.

The point of the help menu is newcomer onboarding, so these assert the things a
first-time player relies on: that every command is listed, that the numbers are
derived (not stale), and that ``$help found`` explains industries and the
*actual* escalating cost for the asking player.
"""

from __future__ import annotations

from tendies import config, emojis, help_menu

from cog_harness import Harness

# asyncio_mode = "auto" (pyproject) runs the async tests below without a mark.


# --------------------------------------------------------------------------
# Pure builders
# --------------------------------------------------------------------------

def test_fee_ladder_matches_founding_formula():
    ladder = help_menu.fee_ladder(50_000, 4, rungs=5)
    assert ladder == [
        ("1st", 50_000), ("2nd", 200_000), ("3rd", 800_000),
        ("4th", 3_200_000), ("5th", 12_800_000),
    ]
    # The ladder must use the exact service formula: base * mult**owned.
    for i, (_ord, fee) in enumerate(ladder):
        assert fee == int(50_000 * (4 ** i))


def test_fee_ladder_tracks_config_defaults():
    """If the knobs change, the ladder follows — no hardcoded numbers."""
    ladder = help_menu.fee_ladder(
        config.BASE_FOUNDING_FEE, config.FEE_MULTIPLIER, rungs=3
    )
    assert ladder[0][1] == config.BASE_FOUNDING_FEE
    assert ladder[1][1] == config.BASE_FOUNDING_FEE * config.FEE_MULTIPLIER


def test_industries_field_lists_every_industry_with_emoji():
    field = help_menu.industries_field()
    for ind in config.INDUSTRIES:
        assert ind.capitalize() in field
        assert emojis.industry(ind) in field
    assert "aliases" in field.lower()


def test_landing_embed_lists_all_commands():
    embed = help_menu.build_landing_embed("$")
    text = embed.description + "\n" + "\n".join(f.value for f in embed.fields)
    # Every catalogued command appears in the menu.
    for name in help_menu.SUMMARY:
        assert f"$" + name in text
    # The quick-start path is present.
    assert "New here" in embed.description
    assert "$jobs" in embed.description and "$found" in embed.description
    # All five categories rendered.
    assert len(embed.fields) == len(help_menu.CATEGORIES)


def test_command_embed_has_usage_aliases_and_context():
    embed = help_menu.build_command_embed(
        "$", "balance", aliases=["bal"],
        extra_fields=[("Extra", "x")],
    )
    names = [f.name for f in embed.fields]
    assert "Usage" in names and "Aliases" in names and "Extra" in names
    usage = next(f.value for f in embed.fields if f.name == "Usage")
    assert "$balance" in usage
    aliases = next(f.value for f in embed.fields if f.name == "Aliases")
    assert "$bal" in aliases


def test_invest_help_states_accredited_threshold_from_config():
    note = help_menu.EXTRAS["invest"]
    from tendies.formatting import fmt
    assert fmt(config.ACCREDITED_THRESHOLD) in note
    assert str(config.INCOME_WINDOW_DAYS) in note


# --------------------------------------------------------------------------
# Live, server-aware help (through the real TendiesHelp)
# --------------------------------------------------------------------------

async def test_help_landing_renders_through_real_help_command():
    h = await Harness.create()
    try:
        ctx = await h.invoke_help(h.ctx(1))
        text = ctx.last_text()
        assert "Tendies — how to play" in text
        assert "$found" in text
    finally:
        await h.close()


async def test_help_found_shows_personalized_fee_ladder():
    h = await Harness.create()
    try:
        founder = 800
        await h.fund_wallet(founder, 10_000_000)

        # Before founding anything: next company is the 1st (base fee).
        ctx = await h.invoke_help(h.ctx(founder), "found")
        text = ctx.last_text()
        assert "Industries" in text
        assert emojis.INDUSTRY_TECH in text  # an industry glyph rendered
        from tendies.formatting import fmt
        assert "own **0**" in text
        assert fmt(config.BASE_FOUNDING_FEE) in text  # next costs the base fee

        # After founding one, the personalized "next" cost scales up.
        await h.invoke(h.ctx(founder), "found", args='ONE "One Co" tech')
        ctx = await h.invoke_help(h.ctx(founder), "found")
        text = ctx.last_text()
        assert "own **1**" in text
        next_fee = config.BASE_FOUNDING_FEE * config.FEE_MULTIPLIER
        assert fmt(next_fee) in text
    finally:
        await h.close()


async def test_help_unknown_command_is_friendly():
    h = await Harness.create()
    try:
        ctx = await h.invoke_help(h.ctx(1), "wat")
        text = ctx.last_text()
        assert "⚠️" in text
        assert "No command called" in text
    finally:
        await h.close()
