"""Capital-markets services: funding rounds, investing, and dividends (§11, §12).

These implement the equity side of the economy — raising money against newly
minted shares, the accredited-investor income gate, and pro-rata dividend
payouts. Like every service they take an already-fetched ``ServerState``, mutate
ORM objects and route all money through :mod:`tendies.money`, and never commit
(the caller's session context does) nor import discord. User errors surface as
:class:`~tendies.errors.GameError` subclasses with player-facing messages.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import gameday, lifecycle, lookups, money
from ..config import (
    ACCREDITED_THRESHOLD,
    BUSINESS_DAYS_PER_YEAR,
    INCOME_WINDOW_DAYS,
)
from ..errors import BadInput, InsufficientFunds, NotAllowed, NotFound
from ..formatting import fmt
from ..models import FundingRound, Holding, ServerState


def _require_business_day(state: ServerState) -> None:
    if not gameday.is_business_day(state.game_day):
        raise NotAllowed(
            "Financial operations are closed for the weekend. Try again Monday."
        )


def _require_positive_int64(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BadInput(f"{label} must be a positive whole number.")
    if value > money.MAX_INT64:
        raise BadInput(f"{label} is too large to store safely.")


# ---------------------------------------------------------------------------
# $raise — open a funding round
# ---------------------------------------------------------------------------

@dataclass
class RaiseResult:
    ticker: str
    amount: int
    equity_pct: float
    new_shares: int
    implied_valuation: float


async def open_round(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    ticker: str,
    amount: int,
    equity_pct: float,
) -> RaiseResult:
    """Open a funding round for the owner's private company (§11).

    ``new_shares`` is the dilution-correct mint that yields exactly
    ``equity_pct`` of the *post-money* cap table:

        new_shares = round(total_shares * equity_pct / (100 - equity_pct))

    (DESIGN §11's literal "1,000,000 new sh" example is an arithmetic typo;
    this formula is implemented instead.) ``implied_valuation`` is
    ``amount / (equity_pct / 100)``.
    """
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    lookups.require_owner(company, owner_id)

    if not math.isfinite(equity_pct) or not (0 < equity_pct < 100):
        raise BadInput("Equity percentage must be between 0 and 100 (exclusive).")
    _require_positive_int64(amount, "Raise amount")

    existing = (
        await session.execute(
            select(FundingRound).where(
                FundingRound.company_id == company.id,
                FundingRound.status == "open",
            )
        )
    ).scalars().first()
    if existing is not None:
        raise BadInput(
            f"**{company.ticker}** already has an open funding round. "
            f"Close it before opening another."
        )

    new_shares = round(company.total_shares * equity_pct / (100 - equity_pct))
    if new_shares <= 0:
        raise BadInput(
            "That equity stake is too small to mint a single share at this "
            "company's size — raise the percentage."
        )
    if new_shares > money.MAX_INT64 - company.total_shares:
        raise BadInput("That round would create too many shares to store safely.")
    implied_valuation = amount / (equity_pct / 100)

    round_ = FundingRound(
        company_id=company.id,
        amount=amount,
        equity_pct=equity_pct,
        total_new_shares=new_shares,
        amount_raised=0,
        shares_minted=0,
        status="open",
    )
    session.add(round_)
    await session.flush()

    return RaiseResult(
        ticker=company.ticker,
        amount=amount,
        equity_pct=equity_pct,
        new_shares=new_shares,
        implied_valuation=implied_valuation,
    )


# ---------------------------------------------------------------------------
# $invest — fill a funding round (accredited gate)
# ---------------------------------------------------------------------------

@dataclass
class InvestResult:
    ticker: str
    amount_invested: int
    shares: int
    pct_of_company: float
    round_closed: bool


async def invest(
    session: AsyncSession,
    state: ServerState,
    investor_id: int,
    ticker: str,
    amount: int,
) -> InvestResult:
    """Buy into an open funding round, subject to the accredited-investor gate.

    The investor's trailing income (wages + dividends over the last
    ``INCOME_WINDOW_DAYS`` business days) is annualized to a
    ``BUSINESS_DAYS_PER_YEAR``-day year; below ``ACCREDITED_THRESHOLD`` the
    purchase is refused (:class:`NotAllowed`).
    """
    _require_business_day(state)
    _require_positive_int64(amount, "Investment amount")

    # An owner can't buy into their own round: wallet -> treasury with shares
    # minted back to themselves is a free anti-dilution lever over the outside
    # investors in the same round. ``$deposit`` is the owner's funding path (§8).
    owned = await lookups.get_company(session, state.guild_id, ticker)
    if owned.owner_id == investor_id:
        raise NotAllowed(
            f"You own **{owned.ticker}** — fund it with "
            f"`$deposit {owned.ticker} <amount>` instead of investing in your "
            f"own round."
        )

    # Accredited-investor gate (§11).
    trailing = await money.trailing_income(
        session, state.guild_id, investor_id, state.game_day, INCOME_WINDOW_DAYS
    )
    annual = trailing * (BUSINESS_DAYS_PER_YEAR / INCOME_WINDOW_DAYS)
    if annual < ACCREDITED_THRESHOLD:
        raise NotAllowed(
            f"⛔ Accredited investors only. Your trailing income annualizes to "
            f"{fmt(annual)} nug/yr (need {fmt(ACCREDITED_THRESHOLD)}). "
            f"Keep earning — or found your own."
        )

    company = owned

    round_ = (
        await session.execute(
            select(FundingRound).where(
                FundingRound.company_id == company.id,
                FundingRound.status == "open",
            )
        )
    ).scalars().first()
    if round_ is None:
        raise NotFound(f"**{company.ticker}** has no open funding round.")

    remaining = round_.amount - round_.amount_raised
    if remaining <= 0:
        round_.status = "closed"
        raise NotFound(f"**{company.ticker}**'s funding round is already full.")

    contribution = min(amount, remaining)

    investor = await money.get_or_create_user(session, state.guild_id, investor_id)
    if investor.wallet < contribution:
        raise InsufficientFunds(
            f"You need {fmt(contribution)} nug to take that slice, "
            f"but your wallet holds {fmt(investor.wallet)} nug."
        )

    # Shares for this tranche; if this contribution completes the round, hand the
    # exact remainder so the mint matches total_new_shares with no rounding drift.
    funded_after = round_.amount_raised + contribution
    fully_funded = funded_after >= round_.amount
    target_minted = (
        round_.total_new_shares
        if fully_funded
        else (round_.total_new_shares * funded_after) // round_.amount
    )
    shares = target_minted - round_.shares_minted

    # Refuse a contribution too small to buy even one share — otherwise the
    # investor would be charged for nothing (the money would land in the
    # treasury but mint zero equity).
    if shares <= 0:
        raise BadInput(
            "That amount is too small to buy any shares in this round — invest more."
        )
    if company.total_shares > money.MAX_INT64 - shares:
        raise BadInput("This investment would create too many shares to store safely.")
    if company.treasury > money.MAX_INT64 - contribution:
        raise BadInput("The company treasury is too large to accept that investment.")

    company.total_shares += shares

    holding = await session.get(Holding, (company.id, investor_id))
    if holding is None:
        holding = Holding(company_id=company.id, user_id=investor_id, shares=shares)
        session.add(holding)
    else:
        holding.shares += shares
    await session.flush()

    await money.inject_capital(session, state, investor, company, contribution, state.game_day)

    round_.amount_raised += contribution
    round_.shares_minted += shares
    round_closed = round_.amount_raised >= round_.amount
    if round_closed:
        round_.status = "closed"

    pct = (holding.shares / company.total_shares * 100) if company.total_shares > 0 else 0.0

    return InvestResult(
        ticker=company.ticker,
        amount_invested=contribution,
        shares=shares,
        pct_of_company=pct,
        round_closed=round_closed,
    )


@dataclass
class CloseRoundResult:
    ticker: str
    amount_raised: int
    target_amount: int
    shares_minted: int


async def close_round(
    session: AsyncSession,
    state: ServerState,
    actor_id: int,
    ticker: str,
    *,
    manager: bool = False,
) -> CloseRoundResult:
    """Close an open round, preserving completed investments and minted shares.

    The owner may close their own round. Manager authority must be passed
    explicitly by the command layer.
    """
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    if company.owner_id != actor_id and not manager:
        raise NotAllowed(
            f"Only the owner of **{company.ticker}** or a Manager can close that round."
        )
    round_ = (
        await session.execute(
            select(FundingRound).where(
                FundingRound.company_id == company.id,
                FundingRound.status == "open",
            )
        )
    ).scalars().first()
    if round_ is None:
        raise NotFound(f"**{company.ticker}** has no open funding round.")
    round_.status = "closed"
    return CloseRoundResult(
        ticker=company.ticker,
        amount_raised=round_.amount_raised,
        target_amount=round_.amount,
        shares_minted=round_.shares_minted,
    )


@dataclass
class DepositResult:
    ticker: str
    amount: int
    treasury_after: int


async def deposit(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    ticker: str,
    amount: int,
) -> DepositResult:
    """Transfer an owner's existing wallet funds into their company treasury."""
    _require_positive_int64(amount, "Deposit amount")
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    lookups.require_owner(company, owner_id)
    owner = await money.get_or_create_user(session, state.guild_id, owner_id)
    if owner.wallet < amount:
        raise InsufficientFunds(
            f"You need {fmt(amount)} nug, but your wallet holds {fmt(owner.wallet)} nug."
        )
    if company.treasury > money.MAX_INT64 - amount:
        raise BadInput("The company treasury is too large to accept that deposit.")
    await money.inject_capital(
        session, state, owner, company, amount, state.game_day, tx_type="deposit"
    )
    return DepositResult(company.ticker, amount, company.treasury)


# ---------------------------------------------------------------------------
# $dividend — pro-rata payout from treasury (§12)
# ---------------------------------------------------------------------------

@dataclass
class DividendPayout:
    user_id: int
    shares: int
    gross: int
    tax: int
    net: int


@dataclass
class DividendResult:
    ticker: str
    amount: int
    per_share: float
    payouts: list[DividendPayout]
    total_tax: int


async def pay_dividend(
    session: AsyncSession,
    state: ServerState,
    owner_id: int,
    ticker: str,
    amount: int,
) -> DividendResult:
    """Distribute ``amount`` from the company treasury pro-rata across the cap
    table, taxed like wages (§12). Owner only."""
    _require_business_day(state)
    _require_positive_int64(amount, "Dividend amount")
    company = await lookups.get_company(
        session, state.guild_id, ticker, private_only=True
    )
    lookups.require_owner(company, owner_id)

    if amount > company.treasury:
        raise InsufficientFunds(
            f"**{company.ticker}**'s treasury holds {fmt(company.treasury)} nug — "
            f"not enough for a {fmt(amount)} nug dividend."
        )

    holders = await lifecycle.cap_table(session, company)
    if not holders:
        raise BadInput(f"**{company.ticker}** has no shareholders to pay.")
    shares_by_user = {uid: sh for uid, sh in holders}

    results = await money.payout_prorata(
        session, state, company, holders, amount, state.game_day, tx_type="dividend"
    )

    per_share = amount / company.total_shares if company.total_shares > 0 else 0.0
    payouts: list[DividendPayout] = []
    total_tax = 0
    for user_id, gross, tax in results:
        total_tax += tax
        payouts.append(
            DividendPayout(
                user_id=user_id,
                shares=shares_by_user.get(user_id, 0),
                gross=gross,
                tax=tax,
                net=gross - tax,
            )
        )

    return DividendResult(
        ticker=company.ticker,
        amount=amount,
        per_share=per_share,
        payouts=payouts,
        total_tax=total_tax,
    )
