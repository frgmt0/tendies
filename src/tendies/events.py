"""Market events (§5).

Events are the day-trading hook and are rare on purpose. The events table *is*
the state: production (tick step 4) and valuation sentiment (§13) both read the
multiplier for a given (industry, game_day) straight from it — there's no
separate mutable "active multiplier" to keep in sync.

Two sources: ``random`` (rolled 0–``MAX_EVENTS_PER_WEEK`` at each Monday tick) and
``admin`` (a Manager firing one for the current day). Event rolling takes an
optional ``rng`` so the tick is deterministically testable.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import emojis, gameday
from .config import INDUSTRIES, MAX_EVENTS_PER_WEEK
from .models import Event, ServerState

#: industry value used for a server-wide event (hits every sector).
MARKET_WIDE = "all"


@dataclass(frozen=True)
class EventTemplate:
    industry: str  # canonical industry or MARKET_WIDE
    multiplier: float
    emoji: str
    blurb: str


#: Curated flavor pool for random rolls. Multipliers run both directions; the
#: market-wide crash is rare and brutal. Each event is tagged with its industry's
#: custom emoji (the market-wide crash with the crash glyph).
EVENT_TEMPLATES: tuple[EventTemplate, ...] = (
    EventTemplate("medicine", 2.5, emojis.INDUSTRY_MEDICINE, "Medicine breakthrough — a blockbuster drug clears trials."),
    EventTemplate("medicine", 0.6, emojis.INDUSTRY_MEDICINE, "Recall scandal rocks the pharma sector."),
    EventTemplate("energy", 2.2, emojis.INDUSTRY_ENERGY, "Energy crunch — prices spike at the pump."),
    EventTemplate("energy", 0.4, emojis.INDUSTRY_ENERGY, "Oil glut floods the market. Frackers, condolences."),
    EventTemplate("tech", 2.4, emojis.INDUSTRY_TECH, "Tech mania — a viral launch lights up the sector."),
    EventTemplate("tech", 0.5, emojis.INDUSTRY_TECH, "Tech bubble jitters send valuations tumbling."),
    EventTemplate("materials", 2.0, emojis.INDUSTRY_MATERIALS, "Commodities supercycle — raw materials in hot demand."),
    EventTemplate("materials", 0.6, emojis.INDUSTRY_MATERIALS, "Mining glut craters materials prices."),
    EventTemplate("food", 1.8, emojis.INDUSTRY_FOOD, "Viral food trend sends orders through the roof."),
    EventTemplate("food", 0.6, emojis.INDUSTRY_FOOD, "Foodborne outbreak spooks the food sector."),
    EventTemplate("logistics", 2.0, emojis.industry("logistics"), "Shipping squeeze — freight rates surge."),
    EventTemplate("logistics", 0.6, emojis.industry("logistics"), "Port backlog snarls the logistics sector."),
    EventTemplate("infrastructure", 1.9, emojis.industry("infrastructure"), "Infrastructure stimulus package passes."),
    EventTemplate("infrastructure", 0.6, emojis.industry("infrastructure"), "Budget freeze halts public projects."),
    EventTemplate("entertainment", 2.3, emojis.industry("entertainment"), "Blockbuster season — entertainment is booming."),
    EventTemplate("entertainment", 0.6, emojis.industry("entertainment"), "Streaming wars bruise the entertainment sector."),
    EventTemplate("finance", 2.1, emojis.INDUSTRY_FINANCE, "Bull run — finance is printing."),
    EventTemplate("finance", 0.5, emojis.INDUSTRY_FINANCE, "Credit crunch hammers the finance sector."),
    EventTemplate("defense", 2.2, emojis.INDUSTRY_DEFENSE, "Defense contracts surge — the generals are buying."),
    EventTemplate("defense", 0.6, emojis.INDUSTRY_DEFENSE, "Peace breaks out; defense budgets get slashed."),
    EventTemplate("consumer", 1.9, emojis.INDUSTRY_CONSUMER, "Consumer spending spree — shelves empty out."),
    EventTemplate("consumer", 0.6, emojis.INDUSTRY_CONSUMER, "Consumer confidence craters; wallets snap shut."),
    EventTemplate(MARKET_WIDE, 0.5, emojis.STOCK_DOWN, "Market-wide crash — everything is on fire."),
)


def _business_days_of_week(monday: dt.date) -> list[dt.date]:
    """Mon–Fri dates for the week starting at ``monday``."""
    return [monday + dt.timedelta(days=i) for i in range(5)]


async def roll_weekly_events(
    session: AsyncSession,
    state: ServerState,
    week_monday: dt.date,
    *,
    rng: random.Random | None = None,
) -> list[Event]:
    """Roll 0–``MAX_EVENTS_PER_WEEK`` events for the business week starting at
    ``week_monday``, persisting them. Returns the created events.

    Each event gets a random template and a random business day, avoiding two
    events on the same (day, industry). Pass ``rng`` to make rolls deterministic.
    """
    rng = rng or random.Random()
    count = rng.randint(0, MAX_EVENTS_PER_WEEK)
    days = _business_days_of_week(week_monday)
    created: list[Event] = []
    used: set[tuple[dt.date, str]] = set()
    attempts = 0
    while len(created) < count and attempts < 50:
        attempts += 1
        template = rng.choice(EVENT_TEMPLATES)
        day = rng.choice(days)
        key = (day, template.industry)
        if key in used:
            continue
        used.add(key)
        event = Event(
            guild_id=state.guild_id,
            game_day=day,
            industry=template.industry,
            multiplier=template.multiplier,
            source="random",
            blurb=f"{template.emoji} {template.blurb}",
        )
        session.add(event)
        created.append(event)
    if created:
        await session.flush()
    return created


async def create_admin_event(
    session: AsyncSession,
    state: ServerState,
    industry: str,
    multiplier: float,
    blurb: str,
) -> Event:
    """Fire an admin event for the current game day. ``industry`` should be a
    canonical industry or ``MARKET_WIDE``; the cog validates input first."""
    event = Event(
        guild_id=state.guild_id,
        game_day=state.game_day,
        industry=industry,
        multiplier=multiplier,
        source="admin",
        blurb=blurb,
    )
    session.add(event)
    await session.flush()
    return event


async def _events_on(
    session: AsyncSession, guild_id: int, game_day: dt.date
) -> list[Event]:
    return (
        await session.execute(
            select(Event).where(
                Event.guild_id == guild_id, Event.game_day == game_day
            )
        )
    ).scalars().all()


async def active_multiplier(
    session: AsyncSession, guild_id: int, game_day: dt.date, industry: str
) -> float:
    """The sentiment multiplier for ``industry`` on ``game_day`` (1.0 normally).

    The product of every event that touches the industry that day — its own
    sector events and any market-wide event. Weekends carry no events, so this
    returns 1.0 there.
    """
    mult = 1.0
    for event in await _events_on(session, guild_id, game_day):
        if event.industry == industry or event.industry == MARKET_WIDE:
            mult *= event.multiplier
    return mult


async def active_multipliers(
    session: AsyncSession, guild_id: int, game_day: dt.date
) -> dict[str, float]:
    """Multiplier for every industry on ``game_day`` (one query)."""
    events = await _events_on(session, guild_id, game_day)
    result = {ind: 1.0 for ind in INDUSTRIES}
    for event in events:
        if event.industry == MARKET_WIDE:
            for ind in result:
                result[ind] *= event.multiplier
        elif event.industry in result:
            result[event.industry] *= event.multiplier
    return result


async def events_for_week(
    session: AsyncSession, guild_id: int, week_monday: dt.date
) -> list[Event]:
    """All events scheduled for the business week (for the Monday announcement)."""
    days = _business_days_of_week(week_monday)
    return (
        await session.execute(
            select(Event)
            .where(Event.guild_id == guild_id, Event.game_day.in_(days))
            .order_by(Event.game_day)
        )
    ).scalars().all()
