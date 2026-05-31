"""Number + display formatting for nuggie figures.

Two styles are used across the bot:

* ``fmt`` — full, comma-grouped integers (``1,000,000,000,000``) for precise
  views like ``$pool`` and ``$dividend`` breakdowns.
* ``abbr`` — abbreviated with a K/M/B/T suffix (``43.2B``) for leaderboards and
  dense tables.

"Real" figures divide a nominal value by the inflation index before formatting.
"""

from __future__ import annotations


def fmt(n: int | float) -> str:
    """Comma-grouped integer, e.g. ``1,042,000,000``."""
    return f"{int(round(n)):,}"


def abbr(n: int | float) -> str:
    """Abbreviated magnitude with one decimal, e.g. ``43.2B``, ``612.0K``.

    Values under 1,000 are shown exactly with comma grouping.
    """
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= threshold:
            return f"{sign}{n / threshold:.1f}{suffix}"
    return f"{sign}{int(round(n)):,}"


def real(nominal: int | float, inflation_index: float) -> float:
    """Convert a nominal nuggie figure to real terms."""
    if inflation_index <= 0:
        return float(nominal)
    return float(nominal) / inflation_index


def fmt_real(nominal: int | float, inflation_index: float) -> str:
    """Comma-grouped real value (nominal / index)."""
    return fmt(real(nominal, inflation_index))


def abbr_real(nominal: int | float, inflation_index: float) -> str:
    """Abbreviated real value (nominal / index)."""
    return abbr(real(nominal, inflation_index))


def pct(x: float) -> str:
    """Format a fraction as a signed percentage, e.g. ``+148%``, ``-1.2%``."""
    p = x * 100
    if abs(p) >= 10:
        return f"{p:+.0f}%"
    return f"{p:+.1f}%"


def shares_pct(shares: int, total: int) -> str:
    """``shares`` as a percentage of ``total`` (``92.0%``)."""
    if total <= 0:
        return "0.0%"
    return f"{shares / total * 100:.1f}%"
