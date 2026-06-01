# Tendies 🍗

Tendies is a per-server Discord economy bot: a self-contained capitalist sandbox where every guild runs its own independent economy denominated in **nuggies** (`nug`). Players spawn broke, grind always-open state jobs, found companies, hire and fire each other, raise capital, trade equity, pay dividends, and acquire rivals — while the server's moderators act as a combined central bank and treasury (printing money, setting taxes, firing market events). Nothing crosses server boundaries; each guild is its own little economy.

## The core loop

Every nuggie that exists was drawn from one reservoir, and all money moves around a single circuit between three places — the **pool**, player **wallets**, and company **treasuries**:

- **Pool → wallets**: state companies pay wages straight from the pool (the faucet that keeps broke newbies earning).
- **Pool → treasuries**: private companies "sell" their daily production to the market, and the market's cash *is* the pool (capped in aggregate per day — a draining pool scales everyone's revenue down together, which is how a recession is felt).
- **Treasuries → wallets**: payroll and dividends.
- **Wallets → treasuries**: investing in funding rounds.
- **Back to the pool**: taxes (withheld on wages and dividends), founding fees, and the treasuries of bankrupt companies.
- **New money**: only `$print` (Manager-only) creates genuinely new nuggies, and it raises the server-wide inflation index, quietly shrinking everyone's *real* wealth.

Because the pool is finite, Managers have to govern: too much state payroll plus realized revenue drains the pool, taxes refill it, and the tension between starving the newbie pipeline, taxing everyone, and printing (eating the inflation hit) is the game.

## Setup

Tendies is a [uv](https://docs.astral.sh/uv/) project (Python ≥ 3.12).

1. **Install dependencies**

   ```sh
   uv sync
   ```

2. **Configure the environment** — copy the example file and edit it:

   ```sh
   cp .env.example .env
   ```

   | Variable | Default | Notes |
   |---|---|---|
   | `DISCORD_TOKEN` | _(none)_ | **Required.** Your Discord bot token. The bot refuses to start without it. |
   | `DATABASE_URL` | `sqlite+aiosqlite:///tendies.db` | SQLite by default (handy for solo testing). For production use Postgres: `postgresql+asyncpg://user:password@host:5432/tendies`. |
   | `COMMAND_PREFIX` | `$` | Command prefix. |
   | `MANAGER_ROLE` | `Tendies Manager` | Name of the role that grants Manager (central-bank) powers. |
   | `TICK_INTERVAL_SECONDS` | `86400` | Real seconds between game-day ticks. `86400` = one real day per game day. Set low (e.g. `60`) to speed up the economy for testing. |

3. **Create a Discord bot + token** — at the [Discord Developer Portal](https://discord.com/developers/applications), create an Application, add a Bot, and copy its token into `DISCORD_TOKEN`.

4. **Enable the required gateway intents** — on the bot's page, under *Privileged Gateway Intents*, turn on both:
   - **Message Content Intent** (prefix commands need to read message text), and
   - **Server Members Intent** (resolves member roles and mentions for hiring and Manager checks).

5. **Invite the bot** — generate an OAuth2 URL with the `bot` scope and, at minimum, the **Send Messages**, **Read Message History**, and **Add Reactions** permissions (reactions back the `✅` confirmation prompts; the bot also posts the daily-close announcement to the server's system channel or the first channel it can write to).

6. **Launch**

   ```sh
   uv run tendies
   ```

### Bootstrapping a server

There is no manual setup step. The first time *any* command runs in a guild, the bot bootstraps that server's economy automatically: it seeds the **pool** to 1,000,000,000,000 nug, sets the inflation index to `1.0` and the default tax rate to 15%, records the current game day, and creates the **state-owned companies** (McNuggie's, Public Works, The Postal Service) as share-less, revenue-less faucets with their always-open, auto-accepting jobs.

**Manager commands** (the central-bank / treasury controls) are restricted to members who either hold the configured **Tendies Manager** role (see `MANAGER_ROLE`) or have the **Manage Server** permission. Everything else is open to all members.

## Command reference

Currency is nuggies (`nug`); the prefix is `$` by default. Wallet, valuation, and net-worth figures are shown in **real** terms (adjusted for the inflation index); flows like wages, tax, and revenue are shown nominal.

| Command | Who | Purpose |
|---|---|---|
| `$help` / `$help <command>` | anyone | Getting-started menu, or detail for one command (e.g. `$help found` shows the industries and your live founding-fee ladder) |
| `$balance` / `$bal` | anyone | Your wallet (real), job, and holdings |
| `$jobs` | anyone | List open positions (incl. always-open state jobs) |
| `$apply <job_id>` | anyone | Apply to a job (state jobs auto-accept) |
| `$clockin` | employee | Collect today's wage + contribute to your employer's production |
| `$clockout` | employee | Clock out for the day |
| `$quit <ticker>` | employee | Leave a job; keep vested equity, forfeit the rest |
| `$found <ticker> <name> <industry>` | anyone | Found a company (scaling fee → pool) |
| `$company <ticker>` | anyone | Company detail: treasury, cap table, revenue, valuation |
| `$postjob <ticker>` | owner | Post a job (title, wage, optional equity grant) |
| `$applicants <ticker>` | owner | Review applicants |
| `$hire <ticker> <applicant>` | owner | Hire an applicant |
| `$fire <ticker> @user` | owner | Fire an employee |
| `$raise <ticker> <amount> <equity%>` | owner | Open a funding round (alias `$fundraise`) |
| `$invest <ticker> <amount>` | accredited | Buy into an open round |
| `$dividend <ticker> <amount>` | owner | Pay a pro-rata dividend (alias `$div`) |
| `$acquire <acquirer> <target> <offer>` | acquirer owner | Send a company-to-company acquisition offer |
| `$accept <acquirer>` | target owner | Accept an offer (keyed by the acquirer's ticker) |
| `$decline <acquirer>` | target owner | Decline an offer |
| `$market` / `$stocks` | anyone | The stock exchange — prices, sentiment, today's movers (frozen on weekends) |
| `$leaderboard` / `$rich` | anyone | Players ranked by real net worth, with holdings |
| `$pool` | anyone | Pool balance, inflation index, tax rate, money supply |
| `$today` | anyone | Game day + market open/closed |
| `$print <amount>` | **Manager** | Add money to the pool (inflationary; requires `✅` confirmation) |
| `$taxrate <percent>` | **Manager** | Set the wage + dividend tax rate |
| `$setday <weekday>` | **Manager** | Correct the game day if the schedule drifts |
| `$event <industry> <multiplier> "<blurb>"` | **Manager** | Fire a market event for today |
| `$forcetick` | **Manager** | Advance the game one day immediately (ops/testing) |

## Project layout

Source lives in `src/tendies/`, importable as `tendies.<module>`.

**Engine core** (the frozen contract — shared primitives every slice builds on):

- `config.py` — tunable game constants (§17 knobs), industries, state-company seeds, and environment-driven `Settings`.
- `models.py` — SQLAlchemy ORM: `ServerState`, `User`, `Company`, `Holding`, `Job`, `Employment`, `EquityGrant`, `Application`, `Offer`, `FundingRound`, `Event`, `Transaction`, plus account helpers.
- `errors.py` — `GameError` and its player-facing subclasses (`NotFound`, `NotAllowed`, `InsufficientFunds`, `BadInput`).
- `money.py` — the single chokepoint for all money movement (fees, capital injection, revenue, wages, dividends, tax withholding, the ledger).
- `lifecycle.py` — cap-table reads and the unwinding helpers used on quit, firing, and bankruptcy.
- `lookups.py` — resolution helpers (`get_state`, `get_company`, `require_owner`, `get_employment`).
- `valuation.py` — on-read company valuation, share prices, net worth, and the two leaderboards.
- `events.py` — market-event rolling and active multipliers.
- `tick.py` — the load-bearing daily tick (`run_tick`) and `is_market_open`.
- `gameday.py` — business-day / weekend calendar helpers.
- `formatting.py` — number formatting (commas, K/M/B/T abbreviation, real-terms conversion, percentages).
- `discordutil.py` — the Discord edge: manager checks, embeds, reaction confirmations, interactive prompts, and the shared amount parser.
- `emojis.py` — the server's custom emoji glyphs (currency, industries, events, etc.) in one place; cogs reference names, not raw IDs.
- `help_menu.py` — the custom `$help` command (`commands.HelpCommand` subclass): an onboarding landing page plus per-command detail, with the `$found` industry list and fee ladder derived live from `config` + the guild's `ServerState`.
- `db.py` — async SQLAlchemy engine + session context manager.
- `bot.py` — `TendiesBot`: owns `db`, `settings`, and the scheduler; bootstraps guilds on demand and renders `GameError`s centrally.
- `scheduler.py` — advances every guild's economy each tick interval and posts the daily-close announcement.
- `__init__.py` — the `tendies` console entry point.

**Services** (`services/` — pure game logic; take `(session, state, ...)`, mutate ORM + call `money`/`lifecycle`, never touch Discord, raise `GameError`):

- `economy.py` — bootstrap, money printing, tax rate, the game-day cursor, and the `$pool` read.
- `companies.py` — founding, company detail, posting jobs, applicants, hiring/firing.
- `employment.py` — applying, clocking in/out, quitting.
- `investment.py` — funding rounds, the accredited gate, dividends.
- `acquisitions.py` — offers, accept/decline, and the M&A unwind.

**Cogs** (`cogs/` — thin Discord command layer; parse args, open a session, fetch state, call a service, render the result):

- `player.py` — `$balance`, `$jobs`, `$apply`, `$clockin`, `$clockout`, `$quit`.
- `company.py` — `$found`, `$company`, `$postjob`, `$applicants`, `$hire`, `$fire`.
- `capital.py` — `$raise`, `$invest`, `$dividend`, `$acquire`, `$accept`, `$decline`.
- `market.py` — `$market`, `$leaderboard`, `$pool`, `$today`.
- `admin.py` — Manager commands: `$print`, `$taxrate`, `$setday`, `$event`, `$forcetick`.

## Running the tests

```sh
uv run pytest
```

## Design

The full design specification — the macroeconomic model, the load-bearing daily-tick ordering, the data model, and the bugs that will actually bite — lives in [`DESIGN.md`](DESIGN.md). The authoritative values for every tunable knob (the §17 "Tunable constants" table: starting pool, founding fees, productivity, tax rate, the recession cap, the accredited threshold, and more) live as plain module constants in [`src/tendies/config.py`](src/tendies/config.py), where the engine imports them directly and tests can monkeypatch them.
