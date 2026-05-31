"""Valuation and net worth (§13). Everything here is computed on read; nothing
is stored.

    avg_daily_revenue = mean realized daily revenue over last 10 business days
    annual_revenue    = avg_daily_revenue * 250
    nominal_value     = treasury + annual_revenue * revenue_multiple * sentiment
    real_value        = nominal_value / inflation_index
    share_price       = real_value / total_shares
    net_worth         = wallet + Σ(shares_held * share_price)            (all real)

``sentiment`` is the company's industry multiplier for the day (1.0 normally,
the event multiplier on an event day). "Δ today" isolates the day's sentiment
move — today's price vs. the same company priced at the previous business day's
sentiment — which is what makes events the day-trader's signal. Prices freeze on
weekends (``frozen`` true → sentiment 1.0, delta 0).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import events as events_mod
from . import gameday
from . import money
from .config import AVG_REVENUE_WINDOW_DAYS, BUSINESS_DAYS_PER_YEAR, REVENUE_MULTIPLE
from .models import Company, Holding, ServerState, User


@dataclass
class CompanyValuation:
    company_id: int
    ticker: str
    name: str
    industry: str
    treasury: int
    total_shares: int
    avg_daily_revenue: float
    annual_revenue: float
    sentiment: float
    nominal_value: float
    real_value: float
    share_price: float  # real, per share
    delta_today: float  # fraction, e.g. 0.148 == +14.8%
    frozen: bool


@dataclass
class HoldingValue:
    company_id: int
    ticker: str
    name: str
    shares: int
    share_price: float  # real
    value: float  # real, shares * share_price


@dataclass
class NetWorth:
    user_id: int
    wallet_real: float
    holdings: list[HoldingValue] = field(default_factory=list)
    holdings_value: float = 0.0
    total: float = 0.0
    owned_tickers: list[str] = field(default_factory=list)


@dataclass
class LeaderboardEntry:
    rank: int
    user_id: int
    net_worth: float
    company_labels: list[str]


def _value_at(treasury: int, annual_revenue: float, sentiment: float) -> float:
    """Nominal value of a company at a given sentiment."""
    return treasury + annual_revenue * REVENUE_MULTIPLE * sentiment


async def company_valuation(
    session: AsyncSession,
    state: ServerState,
    company: Company,
    *,
    sentiment: float | None = None,
    prev_sentiment: float | None = None,
) -> CompanyValuation:
    """Value one company. ``sentiment``/``prev_sentiment`` may be supplied to
    avoid repeated event lookups in batch callers; otherwise they're queried."""
    frozen = not gameday.is_business_day(state.game_day)

    if sentiment is None:
        sentiment = (
            1.0
            if frozen
            else await events_mod.active_multiplier(
                session, state.guild_id, state.game_day, company.industry
            )
        )
    if frozen:
        sentiment = 1.0

    if prev_sentiment is None:
        prev_day = gameday.business_days_before(state.game_day, 1)
        prev_sentiment = await events_mod.active_multiplier(
            session, state.guild_id, prev_day, company.industry
        )

    avg_rev = await money.avg_daily_revenue(
        session, company.id, state.game_day, AVG_REVENUE_WINDOW_DAYS
    )
    annual_rev = avg_rev * BUSINESS_DAYS_PER_YEAR

    nominal = _value_at(company.treasury, annual_rev, sentiment)
    real_value = nominal / state.inflation_index if state.inflation_index else nominal
    share_price = real_value / company.total_shares if company.total_shares > 0 else 0.0

    if frozen:
        delta = 0.0
    else:
        prev_val = _value_at(company.treasury, annual_rev, prev_sentiment)
        delta = (nominal / prev_val - 1.0) if prev_val > 0 else 0.0

    return CompanyValuation(
        company_id=company.id,
        ticker=company.ticker,
        name=company.name,
        industry=company.industry,
        treasury=company.treasury,
        total_shares=company.total_shares,
        avg_daily_revenue=avg_rev,
        annual_revenue=annual_rev,
        sentiment=sentiment,
        nominal_value=nominal,
        real_value=real_value,
        share_price=share_price,
        delta_today=delta,
        frozen=frozen,
    )


async def _active_private_companies(
    session: AsyncSession, guild_id: int
) -> list[Company]:
    return (
        await session.execute(
            select(Company).where(
                Company.guild_id == guild_id,
                Company.active == True,  # noqa: E712
                Company.is_state == False,  # noqa: E712
            )
        )
    ).scalars().all()


async def valuation_map(
    session: AsyncSession, state: ServerState
) -> dict[int, CompanyValuation]:
    """Value every active private company once (shared by market, leaderboard,
    and net worth so events are queried a minimal number of times)."""
    frozen = not gameday.is_business_day(state.game_day)
    today_mults = (
        {}
        if frozen
        else await events_mod.active_multipliers(session, state.guild_id, state.game_day)
    )
    prev_day = gameday.business_days_before(state.game_day, 1)
    prev_mults = await events_mod.active_multipliers(session, state.guild_id, prev_day)

    result: dict[int, CompanyValuation] = {}
    for company in await _active_private_companies(session, state.guild_id):
        sentiment = 1.0 if frozen else today_mults.get(company.industry, 1.0)
        prev_sentiment = prev_mults.get(company.industry, 1.0)
        result[company.id] = await company_valuation(
            session,
            state,
            company,
            sentiment=sentiment,
            prev_sentiment=prev_sentiment,
        )
    return result


async def market_table(
    session: AsyncSession, state: ServerState
) -> list[CompanyValuation]:
    """All active private companies, sorted by real market value (desc)."""
    vals = list((await valuation_map(session, state)).values())
    vals.sort(key=lambda v: v.real_value, reverse=True)
    return vals


async def net_worth(
    session: AsyncSession,
    state: ServerState,
    user_id: int,
    *,
    price_map: dict[int, CompanyValuation] | None = None,
) -> NetWorth:
    """Real net worth for one player: real wallet + Σ holdings·share_price."""
    if price_map is None:
        price_map = await valuation_map(session, state)

    user = await session.get(User, (state.guild_id, user_id))
    wallet = user.wallet if user else 0
    wallet_real = wallet / state.inflation_index if state.inflation_index else float(wallet)

    rows = (
        await session.execute(
            select(Holding.company_id, Holding.shares).where(
                Holding.user_id == user_id, Holding.shares > 0
            )
        )
    ).all()

    holdings: list[HoldingValue] = []
    holdings_value = 0.0
    for cid, shares in rows:
        val = price_map.get(cid)
        if val is None:
            continue  # inactive/state company: shares carry no market value
        worth = shares * val.share_price
        holdings_value += worth
        holdings.append(
            HoldingValue(
                company_id=cid,
                ticker=val.ticker,
                name=val.name,
                shares=int(shares),
                share_price=val.share_price,
                value=worth,
            )
        )
    holdings.sort(key=lambda h: h.value, reverse=True)

    owned = (
        await session.execute(
            select(Company.ticker).where(
                Company.guild_id == state.guild_id,
                Company.owner_id == user_id,
                Company.active == True,  # noqa: E712
            )
        )
    ).scalars().all()

    return NetWorth(
        user_id=user_id,
        wallet_real=wallet_real,
        holdings=holdings,
        holdings_value=holdings_value,
        total=wallet_real + holdings_value,
        owned_tickers=list(owned),
    )


async def leaderboard(
    session: AsyncSession, state: ServerState, *, limit: int = 10
) -> list[LeaderboardEntry]:
    """Players ranked by real net worth, with the companies they hold listed."""
    price_map = await valuation_map(session, state)

    user_ids = (
        await session.execute(
            select(User.user_id).where(User.guild_id == state.guild_id)
        )
    ).scalars().all()

    scored: list[tuple[float, int, list[str]]] = []
    for uid in user_ids:
        nw = await net_worth(session, state, uid, price_map=price_map)
        labels = [f"{h.name} ({h.ticker})" for h in nw.holdings]
        scored.append((nw.total, uid, labels))

    scored.sort(key=lambda x: x[0], reverse=True)
    entries: list[LeaderboardEntry] = []
    for rank, (total, uid, labels) in enumerate(scored[:limit], start=1):
        entries.append(
            LeaderboardEntry(
                rank=rank, user_id=uid, net_worth=total, company_labels=labels
            )
        )
    return entries
