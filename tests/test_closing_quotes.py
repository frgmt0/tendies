import datetime as dt
from tendies import valuation, events
from tendies.services import companies


async def test_weekend_quote_retains_friday_event_and_balances(world):
    w = world
    await w.make_rich(1, 100_000)
    co = (await companies.found_company(w.session, w.state, 1, 'ONE', 'One', 'tech')).company
    w.state.game_day = dt.date(2024, 1, 5)
    co.treasury = 1000
    await events.create_admin_event(w.session, w.state, 'tech', 2.5, 'Boom')
    await valuation.record_closes(w.session, w.state)
    friday = await valuation.company_valuation(w.session, w.state, co)
    w.state.game_day = dt.date(2024, 1, 6)
    co.treasury += 500
    w.state.inflation_index = 2.0
    saturday = await valuation.company_valuation(w.session, w.state, co)
    assert saturday.sentiment == 2.5
    assert saturday.frozen
    assert saturday.share_price == friday.share_price
    assert saturday.real_value == friday.real_value
    assert saturday.delta_today == 0.0


async def test_daily_move_compares_actual_close(world):
    w = world
    await w.make_rich(1, 100_000)
    co = (await companies.found_company(w.session, w.state, 1, 'ONE', 'One', 'tech')).company
    co.treasury = 1000
    await valuation.record_closes(w.session, w.state)
    w.state.game_day += dt.timedelta(days=1)
    co.treasury = 1200
    value = await valuation.company_valuation(w.session, w.state, co)
    assert round(value.delta_today, 6) == 0.2
