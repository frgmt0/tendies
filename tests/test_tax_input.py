"""Percent-input parsing — the one parser behind `$taxrate`, `$raise`, and
`$promote`.

Rule, deliberately changed from the original ``_parse_percent``: **a bare
number is always a percent.** The old parser treated anything ``<= 1`` as an
already-normalized fraction, so ``$taxrate 1`` silently meant 100% and
``$raise X 500K 0.5`` could not mean half a percent. The trailing ``%`` is now
cosmetic, and :func:`tendies.discordutil.parse_percent` returns *percent units*
(``"15%"`` → ``15.0``); callers that want a fraction divide by 100 themselves.
"""

from __future__ import annotations

import pytest

from tendies.discordutil import parse_percent
from tendies.errors import BadInput


# --------------------------------------------------------------------------
# Percent units in, percent units out.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(('raw', 'expected'), [
    ('1%', 1.0), ('1', 1.0),
    ('0.5%', 0.5), ('0.5', 0.5),
    ('2.5', 2.5), ('2.5%', 2.5),
    ('15%', 15.0), ('15', 15.0),
    ('10 %', 10.0), (' 10% ', 10.0),
    ('0%', 0.0), ('0', 0.0),
    ('100', 100.0),
    ('1_000', 1000.0), ('1,000', 1000.0),
])
def test_percent_sign_is_cosmetic(raw, expected):
    assert parse_percent(raw) == expected


def test_bare_small_numbers_are_percents_not_fractions():
    """The behavioural change: `0.15` is 0.15%, not 15%."""
    assert parse_percent('0.15') == 0.15
    assert parse_percent('1') == 1.0
    assert parse_percent('0.5') == 0.5


def test_already_numeric_input_passes_through():
    """discord.py-converted floats (and test callers) still work."""
    assert parse_percent(10.0) == 10.0
    assert parse_percent(25) == 25.0


@pytest.mark.parametrize('raw', ['', '   ', 'abc', '%', None, 'nan', 'inf', '-5'])
def test_rejects_junk(raw):
    with pytest.raises(BadInput):
        parse_percent(raw)


def test_error_message_uses_the_label():
    with pytest.raises(BadInput) as exc:
        parse_percent('abc', label='equity percentage')
    assert 'equity percentage' in str(exc.value)


# --------------------------------------------------------------------------
# $taxrate converts percent units to the stored fraction.
# --------------------------------------------------------------------------

async def test_taxrate_stores_a_fraction():
    from cog_harness import GUILD_ID, Harness
    from tendies import lookups

    h = await Harness.create()
    try:
        # The last commit's 0.5% case: still exactly 0.005 on the row.
        await h.invoke(h.ctx(9, manager=True), 'taxrate', '0.5%')
        async with h.db.session() as session:
            state = await lookups.get_state(session, GUILD_ID)
            assert state.tax_rate == pytest.approx(0.005)

        # And a bare `1` now means 1%, not 100%.
        await h.invoke(h.ctx(9, manager=True), 'taxrate', '1')
        async with h.db.session() as session:
            state = await lookups.get_state(session, GUILD_ID)
            assert state.tax_rate == pytest.approx(0.01)
    finally:
        await h.close()
