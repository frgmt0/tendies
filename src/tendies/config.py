"""Configuration and tunable game constants for Tendies.

Two kinds of settings live here:

* **Tunables** (the §17 knobs) — module-level constants that set game feel.
  They are intentionally plain module constants so the engine can import them
  directly and tests can monkeypatch them.
* **Runtime config** (:class:`Settings`) — environment-driven values such as the
  Discord token, database URL, and tick cadence. Loaded once from the
  environment (and a ``.env`` file if present).

Nothing here touches the database; this module is import-safe everywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load a local .env if present. Safe to call at import time; no-op without a file.
load_dotenv()


# ---------------------------------------------------------------------------
# §17 Tunable constants — the knobs that set game feel.
# ---------------------------------------------------------------------------

#: Initial money supply, seeded entirely into the pool at server setup.
STARTING_POOL = 1_000_000_000_000

#: Cost of founding your 1st company (scales up per company currently owned).
BASE_FOUNDING_FEE = 50_000

#: Founding fee grows by this factor per company you *currently* own.
#: fee = BASE_FOUNDING_FEE * (FEE_MULTIPLIER ** companies_currently_owned)
FEE_MULTIPLIER = 4

#: Shares minted to the founder when a company is created. Founder owns 100%.
SHARES_AT_FOUNDING = 1_000_000

#: Price-to-sales multiple used in valuation. Higher = richer valuations.
REVENUE_MULTIPLE = 3

#: Annualization factor. A "year" is this many business days.
BUSINESS_DAYS_PER_YEAR = 250

#: Max *aggregate* realizable revenue per business day, as a fraction of the
#: current pool. The recession throttle (§9 step 4). Applies to the SUM across
#: all companies, scaling everyone by the same ratio.
RECESSION_CAP_FRACTION = 0.10

#: Default wage + dividend tax rate. Manager-adjustable; this is the refill loop.
DEFAULT_TAX_RATE = 0.15

#: Accredited-investor income gate (annualized nug/yr) required to ``$invest``.
ACCREDITED_THRESHOLD = 200_000

#: Trailing window (business days) over which income is measured for the gate.
INCOME_WINDOW_DAYS = 30

#: Consecutive insolvent business days before bankruptcy fires.
INSOLVENCY_GRACE_DAYS = 3

#: Maximum market events rolled per week (rare on purpose).
MAX_EVENTS_PER_WEEK = 2

#: Window (business days) for the average-daily-revenue figure in valuation.
AVG_REVENUE_WINDOW_DAYS = 10

#: Default per-worker daily productivity (revenue a clocked-in worker generates).
#: Kept above the typical private wage so labor is profitable.
DEFAULT_PRODUCTIVITY = 12_000


# ---------------------------------------------------------------------------
# Industries. Companies pick one; events target one. A closed set keeps event
# targeting meaningful (an event can only move sectors that exist).
# ---------------------------------------------------------------------------

INDUSTRIES: tuple[str, ...] = (
    "food",
    "materials",
    "tech",
    "medicine",
    "energy",
    "logistics",
    "infrastructure",
    "entertainment",
    "finance",
)

#: Friendly aliases accepted from users, normalized to a canonical industry.
INDUSTRY_ALIASES: dict[str, str] = {
    "food service": "food",
    "foodservice": "food",
    "restaurant": "food",
    "material": "materials",
    "mining": "materials",
    "technology": "tech",
    "software": "tech",
    "pharma": "medicine",
    "pharmaceutical": "medicine",
    "health": "medicine",
    "healthcare": "medicine",
    "oil": "energy",
    "power": "energy",
    "shipping": "logistics",
    "delivery": "logistics",
    "transport": "logistics",
    "construction": "infrastructure",
    "infra": "infrastructure",
    "media": "entertainment",
    "games": "entertainment",
    "gaming": "entertainment",
    "bank": "finance",
    "banking": "finance",
}


def normalize_industry(raw: str) -> str | None:
    """Map user input to a canonical industry, or ``None`` if unrecognized."""
    if raw is None:
        return None
    key = raw.strip().lower()
    if key in INDUSTRIES:
        return key
    return INDUSTRY_ALIASES.get(key)


# ---------------------------------------------------------------------------
# State-owned company seeds (§7). Pure faucets: no shares, no revenue, jobs
# always open and auto-accepting. Wages paid straight from the pool.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateJobSeed:
    title: str
    daily_wage: int
    description: str


@dataclass(frozen=True)
class StateCompanySeed:
    name: str
    industry: str
    jobs: tuple[StateJobSeed, ...]


STATE_COMPANIES: tuple[StateCompanySeed, ...] = (
    StateCompanySeed(
        name="McNuggie's",
        industry="food",
        jobs=(
            StateJobSeed("Fry Cook", 3000, "Drop the baskets, hit the timer, repeat. The backbone of the economy."),
            StateJobSeed("Drive-Thru Attendant", 3100, "Smile into the speaker. Upsell the sauce. Make change."),
            StateJobSeed("Sauce Specialist", 3200, "Guardian of the secret ratios. Do not reveal the recipe."),
        ),
    ),
    StateCompanySeed(
        name="Public Works",
        industry="infrastructure",
        jobs=(
            StateJobSeed("Pothole Tech", 3500, "Find hole. Fill hole. The city thanks you, eventually."),
            StateJobSeed("Sanitation Worker", 3300, "Keep the streets clean and the economy moving."),
        ),
    ),
    StateCompanySeed(
        name="The Postal Service",
        industry="logistics",
        jobs=(
            StateJobSeed("Mail Carrier", 3400, "Neither snow nor rain nor a draining pool stays these couriers."),
            StateJobSeed("Sorting Clerk", 3200, "Route the parcels. Mind the conveyor. Stay caffeinated."),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Runtime configuration (environment-driven).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """Runtime configuration loaded from the environment."""

    discord_token: str
    database_url: str
    command_prefix: str
    manager_role: str
    tick_interval_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", ""),
            database_url=os.getenv(
                "DATABASE_URL", "sqlite+aiosqlite:///tendies.db"
            ),
            command_prefix=os.getenv("COMMAND_PREFIX", "$"),
            manager_role=os.getenv("MANAGER_ROLE", "Tendies Manager"),
            tick_interval_seconds=int(os.getenv("TICK_INTERVAL_SECONDS", "86400")),
        )


def get_settings() -> Settings:
    """Return runtime settings loaded from the environment."""
    return Settings.from_env()
