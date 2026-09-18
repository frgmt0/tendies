"""SQLAlchemy ORM models — the Tendies data model (§16).

Every economy is scoped per Discord guild. All stored money is an integer
number of nuggies; valuations and net worth are floats computed on read and
are never stored. Discord IDs are 64-bit, so user/guild IDs use ``BigInteger``.

A couple of small, deliberate extensions to the spec's table sketch:

* ``equity_grants.days_elapsed`` — lets vesting be computed deterministically
  (``vested = total * days_elapsed // vest_days``) so it fully vests by the
  deadline with no floating-point drift.
* ``transactions.game_day`` / ``user_id`` / ``company_id`` — the ledger is the
  source of truth for both the income gate (wages+dividends by user over a
  trailing window of *game* days) and valuation (realized revenue by company),
  so it needs game-day and entity columns to query efficiently.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Account-label prefixes used in the transaction ledger's src/dst fields.
def pool_acct() -> str:
    return "pool"


def wallet_acct(user_id: int) -> str:
    return f"wallet:{user_id}"


def treasury_acct(company_id: int) -> str:
    return f"treasury:{company_id}"


class ServerState(Base):
    """The macro singleton — one row per guild."""

    __tablename__ = "server_state"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pool_balance: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inflation_index: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    game_day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    weekday: Mapped[str] = mapped_column(String(16), nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.15)
    base_founding_fee: Mapped[int] = mapped_column(BigInteger, nullable=False, default=50_000)
    fee_multiplier: Mapped[int] = mapped_column(Integer, nullable=False, default=4)


class User(Base):
    """A player's wallet within a guild. Net worth is computed on read."""

    __tablename__ = "users"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class PlayerProfile(Base):
    """Per-player progression + preferences, kept in its own table so it can be
    added to a live database with no column migration (``create_all`` creates a
    missing table but never alters an existing one). One row per (guild, user),
    created lazily on first clock-in.
    """

    __tablename__ = "player_profiles"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: consecutive business days clocked in (resets on a missed business day).
    clockin_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_clockin_day: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: highest streak milestone (in days) already paid out, so each is one-time.
    milestone_claimed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: opted in to a ping when they forget to clock in on a business day.
    reminder_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: whether we've asked the opt-in question (asked exactly once, ever).
    reminder_prompted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: last game day we pinged them, to ping at most once per day.
    last_reminded_day: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("guild_id", "ticker", name="uq_company_guild_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(4), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: null for state-owned companies.
    owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    industry: Mapped[str] = mapped_column(String(32), nullable=False)
    treasury: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_shares: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    is_state: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: consecutive insolvent business days; bankruptcy fires past the grace.
    insolvent_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    employments: Mapped[list["Employment"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class MarketClose(Base):
    """Immutable business-day quotes for honest daily moves and weekend prices."""

    __tablename__ = "market_closes"
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    game_day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    treasury: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    avg_daily_revenue: Mapped[float] = mapped_column(Float, nullable=False)
    sentiment: Mapped[float] = mapped_column(Float, nullable=False)
    nominal_value: Mapped[float] = mapped_column(Float, nullable=False)
    real_value: Mapped[float] = mapped_column(Float, nullable=False)
    share_price: Mapped[float] = mapped_column(Float, nullable=False)


class Holding(Base):
    """Outright, vested equity. Dividends, acquisitions, and net worth all read
    this one table — every payout is the same pro-rata operation over it."""

    __tablename__ = "holdings"

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shares: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    company: Mapped["Company"] = relationship(back_populates="holdings")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    daily_wage: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: revenue a clocked-in holder of this job generates per business day.
    productivity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    equity_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vest_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    company: Mapped["Company"] = relationship(back_populates="jobs")


class Employment(Base):
    __tablename__ = "employment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    #: wage + productivity are snapshotted at hire so later job edits don't apply.
    daily_wage: Mapped[int] = mapped_column(BigInteger, nullable=False)
    productivity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    clocked_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hired_at: Mapped[dt.date] = mapped_column(Date, nullable=False)

    company: Mapped["Company"] = relationship(back_populates="employments")
    grant: Mapped["EquityGrant | None"] = relationship(
        back_populates="employment",
        cascade="all, delete-orphan",
        uselist=False,
    )


class EquityGrant(Base):
    """Vesting in progress, kept apart from :class:`Holding` so unvested shares
    can't earn dividends or be cashed out early."""

    __tablename__ = "equity_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employment_id: Mapped[int] = mapped_column(
        ForeignKey("employment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    total_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: shares vested so far AND moved into holdings.
    vested_shares: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    vest_days: Mapped[int] = mapped_column(Integer, nullable=False)
    #: business days elapsed since hire; drives deterministic, drift-free vesting.
    days_elapsed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: total_shares / vest_days, for display only.
    daily_vest: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    employment: Mapped["Employment"] = relationship(back_populates="grant")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: pending | accepted | rejected | withdrawn
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    applied_at: Mapped[dt.date] = mapped_column(Date, nullable=False)


class Offer(Base):
    """Acquisition offers. At most one *open* offer per (acquirer, target)."""

    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    acquirer_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: open | accepted | declined | void
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")


class FundingRound(Base):
    """An open funding round (§11). The spec's §16 sketch omits a rounds table,
    but ``$raise``/``$invest`` need persisted round state, so it lives here.

    ``total_new_shares`` are minted *incrementally* as investments arrive (an
    undersubscribed round dilutes less), so ``company.total_shares`` only grows
    by what's actually bought. At most one ``open`` round per company.
    """

    __tablename__ = "funding_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: target nuggies to raise.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: equity percentage offered (0–100), as a float.
    equity_pct: Mapped[float] = mapped_column(Float, nullable=False)
    #: shares minted if the round fills completely.
    total_new_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_raised: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    shares_minted: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: open | closed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    game_day: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    industry: Mapped[str] = mapped_column(String(32), nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, nullable=False)
    #: random | admin
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    blurb: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Transaction(Base):
    """The ledger. The income gate and any audit are just queries over this."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    #: the game day this transaction belongs to (drives income-gate windowing).
    game_day: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    #: wage | state_wage | tax | dividend | founding_fee | revenue | invest
    #: | acquisition | print | bankruptcy | seed | streak_bonus
    type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    src: Mapped[str | None] = mapped_column(String(48), nullable=True)
    dst: Mapped[str | None] = mapped_column(String(48), nullable=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: the player this transaction concerns (recipient of wage/dividend, payer of
    #: fee/investment), for the income gate and per-user audits.
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    #: the company this transaction concerns (for realized-revenue valuation).
    company_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
