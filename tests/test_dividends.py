"""Dividends (§12) — reproduce the worked example exactly.

800,000 distributed over a 920,000 / 80,000 share cap table at 15% tax:
    @owner (920,000 sh) -> 736,000  (-110,400 tax) = 625,600
    @inv   (80,000 sh)  ->  64,000  (-9,600 tax)   =  54,400
    tax -> pool: 120,000
"""

from __future__ import annotations

import pytest

from tendies.config import STARTING_POOL
from tendies.errors import InsufficientFunds
from tendies.models import Holding
from tendies.services import companies, investment

OWNER = 8001
INVESTOR = 8002


async def _company_with_cap_table(w):
    """Found a company and split its 1,000,000 shares 920k / 80k."""
    state = w.state
    await w.make_rich(OWNER, 1_000_000)
    company = (await companies.found_company(w.session, state, OWNER, "DIV", "Div Co", "tech")).company
    await w.session.flush()

    # Move 80,000 shares from owner to an investor by hand to get the §12 split.
    owner_h = await w.session.get(Holding, (company.id, OWNER))
    owner_h.shares = 920_000
    w.session.add(Holding(company_id=company.id, user_id=INVESTOR, shares=80_000))
    await w.session.flush()

    # Fund the treasury (conserving transfer) so it can pay the dividend.
    state.pool_balance -= 1_000_000
    company.treasury += 1_000_000
    await w.session.flush()
    return company


async def test_dividend_worked_example(world):
    w = world
    state = w.state
    company = await _company_with_cap_table(w)
    await w.assert_supply(STARTING_POOL)

    pool_before = state.pool_balance
    owner_wallet_before = await w.wallet(OWNER)
    inv_wallet_before = await w.wallet(INVESTOR)
    res = await investment.pay_dividend(w.session, state, OWNER, "DIV", 800_000)
    await w.session.flush()

    assert res.amount == 800_000
    assert res.per_share == 800_000 / 1_000_000  # 0.80 / sh
    assert res.total_tax == 120_000

    by_user = {p.user_id: p for p in res.payouts}
    owner = by_user[OWNER]
    inv = by_user[INVESTOR]

    assert owner.shares == 920_000
    assert owner.gross == 736_000
    assert owner.tax == 110_400
    assert owner.net == 625_600

    assert inv.shares == 80_000
    assert inv.gross == 64_000
    assert inv.tax == 9_600
    assert inv.net == 54_400

    # Gross payouts sum to the dividend amount (nothing leaks).
    assert owner.gross + inv.gross == 800_000

    # Tax flowed to the pool; treasury fell by exactly the dividend amount.
    assert state.pool_balance == pool_before + 120_000
    assert company.treasury == 1_000_000 - 800_000

    # Wallets rise by the net amounts.
    assert await w.wallet(OWNER) - owner_wallet_before == 625_600
    assert await w.wallet(INVESTOR) - inv_wallet_before == 54_400

    # Conservation intact.
    await w.assert_supply(STARTING_POOL)


async def test_dividend_over_treasury_is_rejected(world):
    w = world
    state = w.state
    company = await _company_with_cap_table(w)
    with pytest.raises(InsufficientFunds):
        await investment.pay_dividend(w.session, state, OWNER, "DIV", company.treasury + 1)
