import pytest

from tendies.cogs.admin import _parse_percent


@pytest.mark.parametrize(('raw', 'expected'), [
    ('1%', 0.01), ('0.5%', 0.005), ('15%', 0.15), ('15', 0.15), ('0.15', 0.15), ('0%', 0.0),
])
def test_explicit_percent_sign_keeps_small_tax_rates(raw, expected):
    assert _parse_percent(raw) == expected
