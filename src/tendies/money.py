"""Money primitives + the transaction ledger.

Every nuggie movement in the game goes through one of these helpers, so money
conservation is enforced in exactly one place. The invariant the whole economy
rests on:

    money_supply == STARTING_POOL + (sum of all $print amounts)

Every operation here is a *transfer* (it conserves supply) except
:func:`print_money`, which is the only minting operation. All stored money is
an integer number of nuggies; tax and pro-rata splits use exact integer math
(``Decimal`` floor for tax, largest-remainder for splits) so nothing leaks.

These helpers mutate ORM objects already attached to ``session`` and append
``Transaction`` rows; the caller's surrounding ``session`` transaction makes the
whole thing atomic. They do *not* commit.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import gameday
from .errors import BadInput
from .models import (
    Company,
    ServerState,
    Transaction,
    User,
    pool_acct,
    treasury_acct,
    wallet_acct,
)

# SQLite INTEGER values are signed 64-bit. Reject larger user-controlled values
# at the service edge instead of letting a flush fail with an opaque OverflowError.
MAX_INT64 = (1 << 63) - 1

# Transaction types whose amounts count as a player's income for the gate.
INCOME_TX_TYPES = ("wage", "state_wage", "dividend")


# ---------------------------------------------------------------------------
# Exact integer arithmetic helpers
# ---------------------------------------------------------------------------

def tax_amount(gross: int, rate: float) -> int:
    """Tax withheld on ``gross`` at ``rate``, floored to an integer nuggie.

    Floored (not rounded) so the payee never loses more than the stated rate
    and the result is deterministic regardless of platform float behavior.
    """
    if gross <= 0 or rate <= 0:
        return 0
    return int((Decimal(gross) * Decimal(str(rate))).to_integral_value(rounding=ROUND_FLOOR))


def prorata_split(amount: int, holders: list[tuple[int, int]]) -> dict[int, int]:
    """Split ``amount`` across ``holders`` (``(key, shares)``) pro-rata by shares.

    Uses the largest-remainder method so the returned integers sum to *exactly*
    ``amount`` (no nuggies minted or destroyed). Ties broken deterministically by
    larger share count, then by key, so results are reproducible.
    """
    holders = [(k, s) for k, s in holders if s > 0]
    total = sum(s for _, s in holders)
    if amount <= 0 or total <= 0 or not holders:
        return {}

    base: dict[int, int] = {}
    allocated = 0
    remainders: list[tuple[int, int, int]] = []  # (remainder, shares, key)
    for key, shares in holders:
        exact = amount * shares
        q, r = divmod(exact, total)
        base[key] = q
        allocated += q
        remainders.append((r, shares, key))

    leftover = amount - allocated
    # Hand out the leftover nuggies to the largest fractional remainders.
    remainders.sort(key=lambda x: (-x[0], -x[1], x[2]))
    for i in range(leftover):
        base[remainders[i][2]] += 1
    return base


# ---------------------------------------------------------------------------
# Ledger + account access
# ---------------------------------------------------------------------------

def record_tx(
    session: AsyncSession,
    *,
    guild_id: int,
    game_day: dt.date,
    type: str,
    amount: int,
    src: str | None = None,
    dst: str | None = None,
    user_id: int | None = None,
    company_id: int | None = None,
    note: str | None = None,
) -> Transaction:
    """Append a ledger row. Synchronous (just stages an INSERT)."""
    tx = Transaction(
        guild_id=guild_id,
        game_day=game_day,
        type=type,
        amount=amount,
        src=src,
        dst=dst,
        user_id=user_id,
        company_id=company_id,
        note=note,
    )
    session.add(tx)
    return tx


async def get_or_create_user(session: AsyncSession, guild_id: int, user_id: int) -> User:
    user = await session.get(User, (guild_id, user_id))
    if user is None:
        user = User(guild_id=guild_id, user_id=user_id, wallet=0)
        session.add(user)
        await session.flush()
    return user


# ---------------------------------------------------------------------------
# Primitive transfers (each conserves the money supply)
# ---------------------------------------------------------------------------

async def charge_fee(
    session: AsyncSession,
    state: ServerState,
    user: User,
    fee: int,
    game_day: dt.date,
    *,
    note: str | None = None,
    company_id: int | None = None,
) -> None:
    """wallet -> pool (founding fees and other sinks). Caller ensures funds."""
    if fee <= 0:
        return
    user.wallet -= fee
    state.pool_balance += fee
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type="founding_fee",
        amount=fee,
        src=wallet_acct(user.user_id),
        dst=pool_acct(),
        user_id=user.user_id,
        company_id=company_id,
        note=note,
    )


async def inject_capital(
    session: AsyncSession,
    state: ServerState,
    investor: User,
    company: Company,
    amount: int,
    game_day: dt.date,
    *,
    tx_type: str = "invest",
) -> None:
    """wallet -> treasury (investment or owner deposit). Capital is not income."""
    investor.wallet -= amount
    company.treasury += amount
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type=tx_type,
        amount=amount,
        src=wallet_acct(investor.user_id),
        dst=treasury_acct(company.id),
        user_id=investor.user_id,
        company_id=company.id,
    )


async def realize_revenue(
    session: AsyncSession,
    state: ServerState,
    company: Company,
    amount: int,
    game_day: dt.date,
) -> None:
    """pool -> treasury (production revenue). Logged every business day for every
    active private company (even at 0) so the valuation average is honest."""
    if amount > 0:
        state.pool_balance -= amount
        company.treasury += amount
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type="revenue",
        amount=amount,
        src=pool_acct(),
        dst=treasury_acct(company.id),
        company_id=company.id,
    )


async def pay_wage(
    session: AsyncSession,
    state: ServerState,
    company: Company,
    user: User,
    gross: int,
    game_day: dt.date,
) -> tuple[int, int]:
    """Private payroll: treasury -> wallet (gross), then wallet -> pool (tax).

    Returns ``(gross_paid, tax_withheld)``. Caller ensures the treasury can
    cover ``gross`` (the tick pays pro-rata when it can't).
    """
    if gross <= 0:
        return 0, 0
    tax = tax_amount(gross, state.tax_rate)
    company.treasury -= gross
    user.wallet += gross
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type="wage",
        amount=gross,
        src=treasury_acct(company.id),
        dst=wallet_acct(user.user_id),
        user_id=user.user_id,
        company_id=company.id,
    )
    _withhold(session, state, user, tax, game_day, company_id=company.id)
    return gross, tax


async def pay_state_wage(
    session: AsyncSession,
    state: ServerState,
    company: Company,
    user: User,
    gross: int,
    game_day: dt.date,
) -> tuple[int, int]:
    """State payroll: pool -> wallet (gross), then wallet -> pool (tax).

    Returns ``(gross_paid, tax_withheld)``. Caller ensures the pool can cover
    ``gross`` (the tick scales state wages down when it can't — the crisis bell).
    """
    if gross <= 0:
        return 0, 0
    tax = tax_amount(gross, state.tax_rate)
    state.pool_balance -= gross
    user.wallet += gross
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type="state_wage",
        amount=gross,
        src=pool_acct(),
        dst=wallet_acct(user.user_id),
        user_id=user.user_id,
        company_id=company.id,
    )
    _withhold(session, state, user, tax, game_day, company_id=company.id)
    return gross, tax


async def pay_bonus(
    session: AsyncSession,
    state: ServerState,
    user: User,
    gross: int,
    game_day: dt.date,
    *,
    note: str | None = None,
) -> tuple[int, int]:
    """A streak/loyalty bonus: pool -> wallet, taxed into the pool like a wage.

    Conserving (a transfer from the pool, not minting). Returns
    ``(gross_paid, tax_withheld)``, or ``(0, 0)`` if the bonus is non-positive
    or the pool can't currently cover it (the bonus is simply skipped, never
    paid on credit). Recorded as ``streak_bonus`` — deliberately NOT in
    :data:`INCOME_TX_TYPES`, so a reward can't be farmed to clear the
    accredited-investor income gate.
    """
    if gross <= 0 or state.pool_balance < gross:
        return 0, 0
    tax = tax_amount(gross, state.tax_rate)
    state.pool_balance -= gross
    user.wallet += gross
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type="streak_bonus",
        amount=gross,
        src=pool_acct(),
        dst=wallet_acct(user.user_id),
        user_id=user.user_id,
        note=note,
    )
    _withhold(session, state, user, tax, game_day)
    return gross, tax


def _withhold(
    session: AsyncSession,
    state: ServerState,
    user: User,
    tax: int,
    game_day: dt.date,
    *,
    company_id: int | None = None,
) -> None:
    """wallet -> pool tax withholding. Internal to wage/dividend payouts."""
    if tax <= 0:
        return
    user.wallet -= tax
    state.pool_balance += tax
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type="tax",
        amount=tax,
        src=wallet_acct(user.user_id),
        dst=pool_acct(),
        user_id=user.user_id,
        company_id=company_id,
    )


async def payout_prorata(
    session: AsyncSession,
    state: ServerState,
    payer: Company,
    holders: list[tuple[int, int]],
    amount: int,
    game_day: dt.date,
    *,
    tx_type: str,
    note: str | None = None,
) -> list[tuple[int, int, int]]:
    """Distribute ``amount`` from ``payer``'s treasury pro-rata across ``holders``
    (``(user_id, shares)``), taxing each recipient's slice into the pool.

    This is the universal payout path — dividends and acquisition payouts both
    use it. Conservation: treasury falls by ``amount``; wallets rise by
    ``amount - total_tax``; pool rises by ``total_tax``.

    Returns ``[(user_id, gross, tax), ...]``.
    """
    split = prorata_split(amount, holders)
    if not split:
        return []
    payer.treasury -= sum(split.values())
    results: list[tuple[int, int, int]] = []
    for user_id, gross in split.items():
        if gross <= 0:
            continue
        recipient = await get_or_create_user(session, state.guild_id, user_id)
        recipient.wallet += gross
        record_tx(
            session,
            guild_id=state.guild_id,
            game_day=game_day,
            type=tx_type,
            amount=gross,
            src=treasury_acct(payer.id),
            dst=wallet_acct(user_id),
            user_id=user_id,
            company_id=payer.id,
            note=note,
        )
        tax = tax_amount(gross, state.tax_rate)
        _withhold(session, state, recipient, tax, game_day, company_id=payer.id)
        results.append((user_id, gross, tax))
    return results


async def return_treasury_to_pool(
    session: AsyncSession,
    state: ServerState,
    company: Company,
    game_day: dt.date,
) -> int:
    """Bankruptcy: a dead company's treasury returns to the pool. Returns the
    amount returned."""
    amount = company.treasury
    if amount:
        company.treasury = 0
        state.pool_balance += amount
        record_tx(
            session,
            guild_id=state.guild_id,
            game_day=game_day,
            type="bankruptcy",
            amount=amount,
            src=treasury_acct(company.id),
            dst=pool_acct(),
            company_id=company.id,
        )
    return amount


async def print_money(
    session: AsyncSession,
    state: ServerState,
    amount: int,
    game_day: dt.date,
) -> float:
    """The only minting operation. Raises the pool by ``amount`` and pushes the
    inflation index by ``index *= (1 + amount / supply_before)``. Returns the
    new inflation index. Money supply rises by exactly ``amount``."""
    supply_before = await money_supply(session, state.guild_id)
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or amount <= 0
        or amount > MAX_INT64 - supply_before
    ):
        raise BadInput("That print would make the money supply too large to store safely.")
    if supply_before > 0:
        state.inflation_index = state.inflation_index * (1 + amount / supply_before)
    state.pool_balance += amount
    record_tx(
        session,
        guild_id=state.guild_id,
        game_day=game_day,
        type="print",
        amount=amount,
        src=None,
        dst=pool_acct(),
        note=f"index->{state.inflation_index:.6f}",
    )
    return state.inflation_index


# ---------------------------------------------------------------------------
# Read-only aggregate queries (used by $pool, valuation, the income gate, tests)
# ---------------------------------------------------------------------------

async def money_supply(session: AsyncSession, guild_id: int) -> int:
    """pool + Σ wallets + Σ treasuries. Should equal STARTING_POOL plus all
    prints, always. The conservation invariant tests assert on this."""
    state = await session.get(ServerState, guild_id)
    pool = state.pool_balance if state else 0
    wallets = await session.scalar(
        select(func.coalesce(func.sum(User.wallet), 0)).where(User.guild_id == guild_id)
    )
    treasuries = await session.scalar(
        select(func.coalesce(func.sum(Company.treasury), 0)).where(
            Company.guild_id == guild_id
        )
    )
    return int(pool) + int(wallets or 0) + int(treasuries or 0)


async def trailing_income(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    current_game_day: dt.date,
    window_days: int,
) -> int:
    """Sum of wages + dividends a user received over the trailing
    ``window_days`` business days (inclusive of the current day).

    A dividend paid by a company the recipient *owns* does not count (§11): an
    owner can deposit their own cash, pay it back out to themselves, and the
    round trip would otherwise manufacture accredited-investor eligibility out
    of money they already had.
    """
    cutoff = gameday.business_days_before(current_game_day, window_days - 1)
    total = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .select_from(Transaction)
        .outerjoin(Company, Transaction.company_id == Company.id)
        .where(
            Transaction.guild_id == guild_id,
            Transaction.user_id == user_id,
            Transaction.type.in_(INCOME_TX_TYPES),
            Transaction.game_day >= cutoff,
            Transaction.game_day <= current_game_day,
            or_(
                Transaction.type != "dividend",
                Company.owner_id.is_(None),
                Company.owner_id != user_id,
            ),
        )
    )
    return int(total or 0)


async def avg_daily_revenue(
    session: AsyncSession,
    company_id: int,
    current_game_day: dt.date,
    window_days: int,
) -> float:
    """Mean realized daily revenue for a company over the trailing
    ``window_days`` business days. Averages over the revenue rows that exist
    (one per active business day, possibly 0); returns 0.0 if none.

    Company ids are globally unique, but the ledger is also guild-scoped, so the
    query pins ``Transaction.guild_id`` to the company's own guild (§16: a guild
    filter at every lookup boundary). Defense in depth against a cross-guild id.
    """
    cutoff = gameday.business_days_before(current_game_day, window_days - 1)
    company_guild = (
        select(Company.guild_id).where(Company.id == company_id).scalar_subquery()
    )
    rows = (
        await session.execute(
            select(Transaction.amount).where(
                Transaction.type == "revenue",
                Transaction.company_id == company_id,
                Transaction.guild_id == company_guild,
                Transaction.game_day >= cutoff,
                Transaction.game_day <= current_game_day,
            )
        )
    ).scalars().all()
    if not rows:
        return 0.0
    return sum(int(r) for r in rows) / len(rows)
