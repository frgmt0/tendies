"""Inflation (§3).

$print pushes the index by  index *= (1 + amount / supply_before).
Nominal balances are unchanged; REAL net worth drops by exactly 1/factor.
"""

from __future__ import annotations

import pytest

from tendies.config import STARTING_POOL
from tendies.errors import BadInput
from tendies.services import economy, employment

HOLDER = 10001


async def test_print_pushes_index_per_formula(world):
    w = world
    state = w.state
    assert state.inflation_index == 1.0

    supply_before = await w.money_supply()
    assert supply_before == STARTING_POOL

    amount = 200_000_000_000  # 200B
    # Preview matches the real apply.
    projected = economy.projected_index(state.inflation_index, amount, supply_before)
    expected = 1.0 * (1 + amount / supply_before)
    assert projected == pytest.approx(expected)

    new_index = await economy.apply_print(w.session, state, amount)
    await w.session.flush()
    assert new_index == pytest.approx(expected)
    assert state.inflation_index == pytest.approx(1.2)  # 1 + 200B/1T


async def test_real_net_worth_drops_nominal_unchanged(world):
    w = world
    state = w.state

    # Give a holder a nominal wallet.
    await w.make_rich(HOLDER, 10_000_000_000)  # 10B
    nominal_before = await w.wallet(HOLDER)

    bal_before = await employment.balance(w.session, state, HOLDER)
    assert bal_before.wallet_real == pytest.approx(nominal_before)  # index 1.0

    supply_before = await w.money_supply()
    amount = 200_000_000_000
    await economy.apply_print(w.session, state, amount)
    await w.session.flush()

    factor = 1 + amount / supply_before  # 1.2
    nominal_after = await w.wallet(HOLDER)
    bal_after = await employment.balance(w.session, state, HOLDER)

    # Nominal wallet unchanged.
    assert nominal_after == nominal_before
    # Real wallet dropped by exactly 1/factor.
    assert bal_after.wallet_real == pytest.approx(nominal_before / factor)
    assert bal_after.net_worth == pytest.approx(bal_before.net_worth / factor)


async def test_print_rejects_nonpositive(world):
    w = world
    with pytest.raises(BadInput):
        await economy.apply_print(w.session, w.state, 0)
    with pytest.raises(BadInput):
        await economy.apply_print(w.session, w.state, -5)
