"""Acquisition services: offers, accepts, declines (§14).

Acquisitions are company-to-company, paid from the acquirer's treasury and
distributed pro-rata to the *target's* entire cap table — the same payout path
as dividends. On accept the acquirer absorbs the target's treasury and open
jobs, the target's employees are laid off, and the target deactivates. Offers
are keyed by the acquirer's ticker (``$accept MOON``), with at most one open
offer per (acquirer, target) pair.

Like every service these take an already-fetched ``ServerState``, mutate ORM
objects, route money through :mod:`tendies.money`, never commit, and never
import discord. User errors are :class:`~tendies.errors.GameError` subclasses.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .. import gameday, lifecycle, lookups, money
from ..errors import BadInput, GameError, InsufficientFunds, NotAllowed, NotFound
from ..formatting import fmt
from ..models import Company, Employment, Offer, ServerState, treasury_acct


# ---------------------------------------------------------------------------
# $acquire — send an offer
# ---------------------------------------------------------------------------

@dataclass
class OfferResult:
    acquirer_ticker: str
    target_ticker: str
    amount: int
    target_owner_id: int


async def offer(
    session: AsyncSession,
    state: ServerState,
    acquirer_owner_id: int,
    acquirer_ticker: str,
    target_ticker: str,
    amount: int,
) -> OfferResult:
    """Send (or replace) an open acquisition offer from the acquirer to the
    target. Owner of the acquirer only; paid later from its treasury."""
    acquirer = await lookups.get_company(
        session, state.guild_id, acquirer_ticker, private_only=True
    )
    lookups.require_owner(acquirer, acquirer_owner_id)

    target = await lookups.get_company(
        session, state.guild_id, target_ticker, private_only=True
    )

    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise BadInput("Offer amount must be a positive whole number.")
    if amount > money.MAX_INT64:
        raise BadInput("Offer amount is too large to store safely.")
    if acquirer.id == target.id:
        raise BadInput("A company can't acquire itself.")
    if acquirer.treasury < amount:
        raise InsufficientFunds(
            f"**{acquirer.ticker}**'s treasury holds {fmt(acquirer.treasury)} nug — "
            f"not enough to offer {fmt(amount)} nug for **{target.ticker}**."
        )

    # Replace any existing open offer for this (acquirer, target) pair.
    existing = (
        await session.execute(
            select(Offer).where(
                Offer.acquirer_id == acquirer.id,
                Offer.target_id == target.id,
                Offer.status == "open",
            )
        )
    ).scalars().all()
    for old in existing:
        old.status = "void"

    new_offer = Offer(
        acquirer_id=acquirer.id,
        target_id=target.id,
        amount=amount,
        status="open",
    )
    session.add(new_offer)
    await session.flush()

    return OfferResult(
        acquirer_ticker=acquirer.ticker,
        target_ticker=target.ticker,
        amount=amount,
        target_owner_id=target.owner_id,
    )


# ---------------------------------------------------------------------------
# Shared resolution: find the single open offer for (acquirer ticker -> caller)
# ---------------------------------------------------------------------------

async def _find_open_offer(
    session: AsyncSession,
    state: ServerState,
    target_owner_id: int,
    acquirer_ticker: str,
    target_ticker: str | None = None,
) -> tuple[Offer, Company, Company]:
    """Resolve the open offer where the acquirer has ``acquirer_ticker`` and the
    target is active and owned by ``target_owner_id``. Raises :class:`NotFound`
    if none, or :class:`GameError` if more than one matches."""
    acquirer = aliased(Company)
    target = aliased(Company)
    ticker_norm = (acquirer_ticker or "").strip().upper()

    stmt = (
        select(Offer, acquirer, target)
        .join(acquirer, Offer.acquirer_id == acquirer.id)
        .join(target, Offer.target_id == target.id)
        .where(
            Offer.status == "open",
            func.upper(acquirer.ticker) == ticker_norm,
            target.active == True,  # noqa: E712
            target.owner_id == target_owner_id,
        )
    )
    target_norm = (target_ticker or "").strip().upper()
    if target_ticker is not None:
        stmt = stmt.where(func.upper(target.ticker) == target_norm)
    rows = (await session.execute(stmt)).all()

    if not rows:
        raise NotFound(
            f"No open offer from **{ticker_norm}**"
            + (f" for **{target_norm}**." if target_ticker is not None else " for a company you own.")
        )
    if len(rows) > 1:
        targets = ", ".join(sorted(t.ticker for _, _, t in rows))
        raise GameError(
            f"**{ticker_norm}** has offers for more than one company you own "
            f"({targets}). Choose one with `$accept {ticker_norm} <target>`."
        )

    offer_row, acquirer_co, target_co = rows[0]
    return offer_row, acquirer_co, target_co


# ---------------------------------------------------------------------------
# $accept — close the deal
# ---------------------------------------------------------------------------

@dataclass
class AcceptResult:
    acquirer_ticker: str
    target_ticker: str
    amount: int
    payouts: list[tuple[int, int, int]]  # (user_id, gross, tax)
    treasury_absorbed: int
    jobs_transferred: int
    employees_laid_off: int


async def accept(
    session: AsyncSession,
    state: ServerState,
    target_owner_id: int,
    acquirer_ticker: str,
    target_ticker: str | None = None,
) -> AcceptResult:
    """Accept an open offer (caller is the target owner). Executes the M&A close
    in the load-bearing order from §14."""
    if not gameday.is_business_day(state.game_day):
        raise NotAllowed(
            "Financial operations are closed for the weekend. Try again Monday."
        )
    offer_row, acquirer, target = await _find_open_offer(
        session, state, target_owner_id, acquirer_ticker, target_ticker
    )

    clocked_in = int(
        await session.scalar(
            select(func.count())
            .select_from(Employment)
            .where(
                Employment.company_id == target.id,
                Employment.clocked_in == True,  # noqa: E712
            )
        )
        or 0
    )
    if clocked_in:
        raise NotAllowed(
            f"**{target.ticker}** has {clocked_in} employee(s) clocked in. "
            "Finish today's tick before closing the acquisition so earned payroll is honored."
        )

    if acquirer.treasury < offer_row.amount:
        raise InsufficientFunds(
            f"**{acquirer.ticker}**'s treasury holds {fmt(acquirer.treasury)} nug — "
            f"no longer enough to honor its {fmt(offer_row.amount)} nug offer."
        )

    amount = offer_row.amount

    # 1) Pay the offer pro-rata to the target's entire cap table (taxed).
    holders = await lifecycle.cap_table(session, target)
    if not holders:
        raise GameError(f"**{target.ticker}** has no shareholders to pay.")
    payouts = await money.payout_prorata(
        session, state, acquirer, holders, amount, state.game_day, tx_type="acquisition"
    )

    # 2) Absorb the target's treasury into the acquirer.
    treasury_absorbed = target.treasury
    if treasury_absorbed > 0:
        if acquirer.treasury > money.MAX_INT64 - treasury_absorbed:
            raise BadInput("The combined treasury is too large to store safely.")
        acquirer.treasury += treasury_absorbed
        target.treasury = 0
        money.record_tx(
            session,
            guild_id=state.guild_id,
            game_day=state.game_day,
            type="acquisition",
            amount=treasury_absorbed,
            src=treasury_acct(target.id),
            dst=treasury_acct(acquirer.id),
            company_id=acquirer.id,
            note="treasury absorbed",
        )

    # 3) Transfer the target's open jobs (production capacity) to the acquirer.
    jobs_transferred = await lifecycle.transfer_open_jobs(session, target, acquirer)

    # 4) Mark the offer accepted and void any other offers involving the target.
    offer_row.status = "accepted"
    await lifecycle.void_offers_involving(session, target)
    await lifecycle.close_funding_rounds(session, target)

    # 5) Lay off all of the target's employees (forfeiting unvested grants).
    employees_laid_off = await lifecycle.end_all_employment(session, target)

    # 6) The cap table was just cashed out, so the target's shares cease to
    #    exist (mirrors the bankruptcy path; keeps total_shares == Σ holdings).
    await lifecycle.wipe_holdings(session, target)

    # 7) The target ceases to exist.
    target.active = False

    return AcceptResult(
        acquirer_ticker=acquirer.ticker,
        target_ticker=target.ticker,
        amount=amount,
        payouts=payouts,
        treasury_absorbed=treasury_absorbed,
        jobs_transferred=jobs_transferred,
        employees_laid_off=employees_laid_off,
    )


# ---------------------------------------------------------------------------
# $decline — reject an offer
# ---------------------------------------------------------------------------

async def decline(
    session: AsyncSession,
    state: ServerState,
    target_owner_id: int,
    acquirer_ticker: str,
    target_ticker: str | None = None,
) -> None:
    """Decline an open offer (caller is the target owner)."""
    offer_row, _acquirer, _target = await _find_open_offer(
        session, state, target_owner_id, acquirer_ticker, target_ticker
    )
    offer_row.status = "declined"
