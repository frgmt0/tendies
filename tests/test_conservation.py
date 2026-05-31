"""Money conservation through a long mixed sequence.

    money_supply == STARTING_POOL + Σ(prints)

Every operation except ``$print`` is a pure transfer; the invariant must hold at
every step of a realistic playthrough and survive a print exactly.
"""

from __future__ import annotations

from tendies import gameday
from tendies.config import STARTING_POOL
from tendies.services import acquisitions, companies, economy, employment, investment

from conftest import first_state_job


OWNER = 1001
INVESTOR = 1002
WORKER = 1003
ACQUIRER_OWNER = 1004


async def test_conservation_through_full_playthrough(world):
    w = world
    state = w.state

    # Supply starts exactly at STARTING_POOL.
    await w.assert_supply(STARTING_POOL)

    # Fund founders from the pool (a conserving transfer).
    await w.make_rich(OWNER, 5_000_000)
    await w.make_rich(ACQUIRER_OWNER, 5_000_000)
    await w.assert_supply(STARTING_POOL)

    # Found two companies for OWNER (fees 50k, 200k) and one for ACQUIRER_OWNER.
    aaa = (await companies.found_company(w.session, state, OWNER, "AAA", "Alpha Inc", "tech")).company
    await w.session.flush()
    bbb = (await companies.found_company(w.session, state, ACQUIRER_OWNER, "BBB", "Beta Inc", "tech")).company
    await w.session.flush()
    ccc = (await companies.found_company(w.session, state, OWNER, "CCC", "Gamma Inc", "food")).company
    await w.session.flush()
    await w.assert_supply(STARTING_POOL)

    # Post a job at AAA with equity, apply, hire.
    job = await companies.post_job(
        w.session, state, OWNER, "AAA", "Engineer", "build", 8_000, 100_000, 10
    )
    await w.session.flush()
    apply_res = await employment.apply_to_job(w.session, state, WORKER, job.id)
    assert not apply_res.auto_accepted
    apps = await companies.list_applicants(w.session, state, OWNER, "AAA")
    assert len(apps) == 1
    await companies.hire(w.session, state, OWNER, "AAA", apps[0].application_id)
    await w.session.flush()

    # Fund AAA's treasury so it can make payroll (conserving transfer pool->treasury).
    state.pool_balance -= 2_000_000
    aaa.treasury += 2_000_000
    await w.session.flush()
    await w.assert_supply(STARTING_POOL)

    # Worker clocks in, then several producing ticks (wages, tax, revenue all conserve).
    for _ in range(5):
        # ensure it's a business day before clocking in
        if not gameday.is_business_day(state.game_day):
            await w.tick()
        from tendies.services.employment import clock_in
        await clock_in(w.session, state, WORKER)
        await w.session.flush()
        await w.tick()
        await w.assert_supply(STARTING_POOL)

    # Dividend off AAA's treasury.
    await investment.pay_dividend(w.session, state, OWNER, "AAA", 100_000)
    await w.session.flush()
    await w.assert_supply(STARTING_POOL)

    # Owner opens a round; investor (made accredited via direct income) invests.
    # Drive enough income into INVESTOR via a state job so the gate clears.
    cid, sjob = await first_state_job(w)
    await employment.apply_to_job(w.session, state, INVESTOR, sjob)
    await w.session.flush()
    for _ in range(30):
        if not gameday.is_business_day(state.game_day):
            await w.tick()
            continue
        from tendies.services.employment import clock_in
        # investor may have been auto-clocked out; re-clock each business day.
        try:
            await clock_in(w.session, state, INVESTOR)
        except Exception:
            pass
        await w.session.flush()
        await w.tick()
    await w.assert_supply(STARTING_POOL)

    await w.make_rich(INVESTOR, 50_000_000)
    await w.assert_supply(STARTING_POOL)
    await investment.open_round(w.session, state, OWNER, "AAA", 10_000_000, 10.0)
    await w.session.flush()
    await investment.invest(w.session, state, INVESTOR, "AAA", 10_000_000)
    await w.session.flush()
    await w.assert_supply(STARTING_POOL)

    # Acquisition: BBB (acquirer) buys CCC (target). Fund BBB's treasury first.
    state.pool_balance -= 5_000_000
    bbb.treasury += 5_000_000
    await w.session.flush()
    await acquisitions.offer(w.session, state, ACQUIRER_OWNER, "BBB", "CCC", 1_000_000)
    await w.session.flush()
    await acquisitions.accept(w.session, state, OWNER, "BBB")
    await w.session.flush()
    await w.assert_supply(STARTING_POOL)

    # No prints happened, so supply is still exactly STARTING_POOL.
    await w.assert_supply(STARTING_POOL)


async def test_conservation_after_print(world):
    w = world
    state = w.state
    await w.assert_supply(STARTING_POOL)

    printed = 200_000_000_000
    await economy.apply_print(w.session, state, printed)
    await w.session.flush()

    # Supply rose by exactly the printed amount; everything else conserves.
    await w.assert_supply(STARTING_POOL + printed)

    # A subsequent transfer (make_rich) preserves the inflated supply.
    await w.make_rich(OWNER, 10_000)
    await w.assert_supply(STARTING_POOL + printed)
