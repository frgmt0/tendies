"""Custom server emojis, in one place.

Discord renders a custom emoji from its ``<:name:id>`` mention anywhere the bot
shares a server that has the emoji. All of Tendies' branded glyphs live here so
the cogs / scheduler / events reference *names* instead of raw IDs — change an ID
in one spot and it updates everywhere.

Generic UI affordances the server didn't draw a custom glyph for stay as standard
unicode on purpose: the ✅ confirmation reaction (custom-emoji reactions need
extra perms), ⚠️/⛔ warnings, 📅 calendar, 🌙 closed market, 🔒 lock, 🚫 decline.

NOTE: custom emojis do NOT render inside Markdown code blocks, so the aligned
``$market`` price table keeps plain-text industry names; emojis are used in embed
titles, fields, and prose instead.
"""

from __future__ import annotations

# -- Currency & macro -------------------------------------------------------
NUGGIE = "<:nuggie_coin:1510528384214499468>"
TREASURY_POOL = "<:treasury_pool:1510528389348589578>"
MONEY_PRINTER = "<:money_printer_inflation:1510528383191089172>"
RECESSION = "<:recession:1510528385804140726>"

# -- Markets ----------------------------------------------------------------
STOCK_UP = "<:invest_stock_up:1510528378195804253>"
STOCK_DOWN = "<:stock_down_crash:1510528386953383946>"
LEADERBOARD = "<:leaderboard_richest:1510528379391315968>"
DIVIDEND = "<:dividend_payout:1510528369098227772>"
ACQUISITION = "<:acquisition_handshake:1510528362517368932>"
BANKRUPTCY = "<:bankruptcy:1510528363482316810>"

# -- Companies & employment -------------------------------------------------
FACTORY = "<:factory_company:1510528371531055314>"
HIRING = "<:hiring:1510528377021403147>"
FIRED = "<:fired_layoff:1510528374840230000>"
CLOCK_IN = "<:clock_in:1510528365856292944>"

# -- Events -----------------------------------------------------------------
BREAKING_NEWS = "<:breaking_news_event:1510528364513988659>"

# -- Industries -------------------------------------------------------------
INDUSTRY_FOOD = "<:food_industry:1510528375997857862>"
INDUSTRY_MATERIALS = "<:materials_industry:1510528380800471090>"
INDUSTRY_TECH = "<:tech_industry:1510528388278784120>"
INDUSTRY_MEDICINE = "<:medicine_industry:1510528381903569016>"
INDUSTRY_ENERGY = "<:energy_industry:1510528370495197276>"
INDUSTRY_FINANCE = "<:finance_industry:1510528373175353344>"
INDUSTRY_DEFENSE = "<:defense_industry:1510528368020295700>"
INDUSTRY_CONSUMER = "<:consumer_industry:1510528366887829595>"

#: industry name -> emoji. The three industries the server hasn't drawn a glyph
#: for yet (logistics, infrastructure, entertainment) fall back to an
#: informative unicode marker; add a custom emoji here and it propagates
#: everywhere it's used.
INDUSTRY: dict[str, str] = {
    "food": INDUSTRY_FOOD,
    "materials": INDUSTRY_MATERIALS,
    "tech": INDUSTRY_TECH,
    "medicine": INDUSTRY_MEDICINE,
    "energy": INDUSTRY_ENERGY,
    "finance": INDUSTRY_FINANCE,
    "defense": INDUSTRY_DEFENSE,
    "consumer": INDUSTRY_CONSUMER,
    "logistics": "🚚",
    "infrastructure": "🏗️",
    "entertainment": "🎬",
}


def industry(name: str) -> str:
    """The emoji for an industry name (the company glyph if somehow unknown)."""
    return INDUSTRY.get(name, FACTORY)
