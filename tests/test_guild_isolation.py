"""Guild isolation at every lookup boundary (§16).

Company ids, job ids, and transaction rows are global autoincrements; tickers
are unique only *within* a guild. Every service lookup therefore has to filter
on the caller's guild, or one server's command settles another server's state.
These are regression tests for three confirmed cross-guild holes.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select

from tendies import money
from tendies.config import AVG_REVENUE_WINDOW_DAYS, STARTING_POOL
from tendies.errors import NotFound
from tendies.models import Company, Job, Transaction
from tendies.services import acquisitions, companies, economy, employment

from conftest import World

GUILD_A = 70_001
GUILD_B = 70_002

#: the same Discord user is a member of both guilds — the pivot of the exploit.
CROOK = 71_001
A_RIVAL = 71_002
B_RIVAL = 71_003
WORKER = 71_004


@pytest_asyncio.fixture
async def two_guilds(db):
    """Two bootstrapped guilds sharing one session (so flushes are visible to
    both), each anchored on the same Monday."""
    session = db.sessionmaker()
    monday = dt.date(2024, 1, 1)
    state_a = await economy.ensure_bootstrapped(session, GUILD_A, monday)
    state_b = await economy.ensure_bootstrapped(session, GUILD_B, monday)
    await session.flush()
    a = World(db, session, state_a)
    b = World(db, session, state_b)
    await a.clear_events()
    await b.clear_events()
    try:
        yield a, b
        await session.commit()
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# $accept / $decline must not reach across guilds
# ---------------------------------------------------------------------------

async def test_accept_cannot_resolve_another_guilds_offer(two_guilds):
    a, b = two_guilds
    session = a.session

    # Guild B: B_RIVAL's BUY has an open offer for CROOK's TGT.
    await b.make_rich(B_RIVAL, 5_000_000)
    await b.make_rich(CROOK, 5_000_000)
    buy_b = (await companies.found_company(session, b.state, B_RIVAL, "BUY", "Buyer B", "tech")).company
    tgt_b = (await companies.found_company(session, b.state, CROOK, "TGT", "Target B", "food")).company
    b.state.pool_balance -= 20_000_000
    buy_b.treasury += 20_000_000
    await session.flush()
    await acquisitions.offer(session, b.state, B_RIVAL, "BUY", "TGT", 10_000_000)
    await session.flush()

    # Guild A: the very same tickers exist, owned by different people. CROOK
    # owns TGT here too, so the (ticker, owner) shape of guild B's offer matches
    # perfectly — only the guild filter separates them.
    await a.make_rich(A_RIVAL, 5_000_000)
    await a.make_rich(CROOK, 5_000_000)
    await companies.found_company(session, a.state, A_RIVAL, "BUY", "Buyer A", "tech")
    await companies.found_company(session, a.state, CROOK, "TGT", "Target A", "food")
    await session.flush()

    supply_a = await money.money_supply(session, GUILD_A)
    supply_b = await money.money_supply(session, GUILD_B)

    with pytest.raises(NotFound):
        await acquisitions.accept(session, a.state, CROOK, "BUY")

    # Declining from the wrong guild must not touch it either.
    with pytest.raises(NotFound):
        await acquisitions.decline(session, a.state, CROOK, "BUY")

    await session.flush()
    assert await money.money_supply(session, GUILD_A) == supply_a == STARTING_POOL
    assert await money.money_supply(session, GUILD_B) == supply_b == STARTING_POOL
    # Guild B's offer is untouched and its target still exists.
    assert tgt_b.active is True
    assert tgt_b.treasury == 0


async def test_accept_still_works_in_the_owning_guild(two_guilds):
    """The guild filter narrows the query; it must not break the real path."""
    a, b = two_guilds
    session = a.session

    await b.make_rich(B_RIVAL, 5_000_000)
    await b.make_rich(CROOK, 5_000_000)
    buy_b = (await companies.found_company(session, b.state, B_RIVAL, "BUY", "Buyer B", "tech")).company
    tgt_b = (await companies.found_company(session, b.state, CROOK, "TGT", "Target B", "food")).company
    b.state.pool_balance -= 20_000_000
    buy_b.treasury += 20_000_000
    await session.flush()
    await acquisitions.offer(session, b.state, B_RIVAL, "BUY", "TGT", 10_000_000)
    await session.flush()

    result = await acquisitions.accept(session, b.state, CROOK, "BUY")
    await session.flush()

    assert result.target_ticker == "TGT"
    assert tgt_b.active is False
    assert await money.money_supply(session, GUILD_B) == STARTING_POOL


# ---------------------------------------------------------------------------
# $apply must not reach another guild's payroll
# ---------------------------------------------------------------------------

async def test_apply_cannot_join_another_guilds_job(two_guilds):
    a, b = two_guilds
    session = a.session

    await b.make_rich(B_RIVAL, 5_000_000)
    await companies.found_company(session, b.state, B_RIVAL, "BCO", "B Co", "tech")
    await session.flush()
    b_job = await companies.post_job(
        session, b.state, B_RIVAL, "BCO", "Mole", "", 9_000, 0, 0
    )
    await session.flush()

    # A worker acting in guild A hands the bot guild B's global job id.
    with pytest.raises(NotFound):
        await employment.apply_to_job(session, a.state, WORKER, b_job.id)

    # Nothing queued and nobody hired in either guild.
    apps = await companies.list_applicants(session, b.state, B_RIVAL, "BCO")
    assert apps == []
    from tendies.lookups import get_employment

    assert await get_employment(session, GUILD_A, WORKER) is None
    assert await get_employment(session, GUILD_B, WORKER) is None


async def test_apply_cannot_join_another_guilds_state_job(two_guilds):
    """State jobs auto-accept, so the cross-guild hole was an instant hire."""
    a, b = two_guilds
    session = a.session

    b_state_job = (
        await session.execute(
            select(Job)
            .join(Company, Job.company_id == Company.id)
            .where(Company.guild_id == GUILD_B, Company.is_state == True)  # noqa: E712
            .order_by(Job.id)
        )
    ).scalars().first()
    assert b_state_job is not None

    with pytest.raises(NotFound):
        await employment.apply_to_job(session, a.state, WORKER, b_state_job.id)

    from tendies.lookups import get_employment

    assert await get_employment(session, GUILD_B, WORKER) is None


# ---------------------------------------------------------------------------
# Valuation revenue is scoped to the company's own guild
# ---------------------------------------------------------------------------

async def test_avg_daily_revenue_ignores_rows_from_another_guild(two_guilds):
    a, b = two_guilds
    session = a.session

    await a.make_rich(A_RIVAL, 5_000_000)
    co = (await companies.found_company(session, a.state, A_RIVAL, "REV", "Rev Co", "tech")).company
    await session.flush()

    session.add(
        Transaction(
            guild_id=GUILD_A,
            game_day=a.state.game_day,
            type="revenue",
            amount=10_000,
            company_id=co.id,
        )
    )
    # A row tagged with the company's id but another guild's id must not count.
    session.add(
        Transaction(
            guild_id=GUILD_B,
            game_day=a.state.game_day,
            type="revenue",
            amount=1_000_000_000,
            company_id=co.id,
        )
    )
    await session.flush()

    avg = await money.avg_daily_revenue(
        session, co.id, a.state.game_day, AVG_REVENUE_WINDOW_DAYS
    )
    assert avg == pytest.approx(10_000.0)
