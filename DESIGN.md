# Tendies — v1 design and operating contract

Tendies is a cooperative/competitive Discord economy. Each guild has its own pool, workers, companies, currency, and ledger. Players earn **nuggies** (`nug`), start businesses, hire one another, raise capital, receive dividends, and acquire competitors. Managers govern taxes, inflation, and market events. The default command prefix is `$`; `/bug` is a Discord application command.

This document describes the shipped v1 contract. Secondary share trading, order queues, IPOs, and monetary-policy features explicitly listed in §19 are future work, not implied features of `$market`. The Python services and regression tests implement the rules below; README documents running them.

## 1. The economic loop

Money circulates among the guild pool, wallets, and private-company treasuries:

- State wages and streak bonuses: pool → wallets.
- Private production: pool → company treasuries.
- Private wages, dividends, and acquisition proceeds: treasury → wallets.
- Funding rounds and owner deposits: wallet → treasury.
- Taxes, founding fees, and bankruptcy remnants: wallets/treasuries → pool.
- Only Manager money printing increases the total supply after initial bootstrap.

The accounting invariant is `supply = pool + all wallets + all company treasuries`. Every transfer is atomic, uses integer nuggies, and records a ledger entry. Shares and modeled business value are not money. Displaying a high valuation does not create spendable cash or guarantee a buyer.

## 2. Pool and money supply

A new guild starts with 1,000,000,000,000 nug, all in the pool, and players start with zero. The pool pays state employees and buys private output. Production is capped in aggregate at 10% of the available pool per business close. A shrinking pool therefore reduces private revenue across the economy rather than privileging the first company processed.

Taxes recycle money; they do not destroy it. Bankruptcy returns any remaining treasury to the pool. `$pool` reports the pool, supply, tax rate, and inflation index. `$stats` reports flows, employment, industry composition, and concentration.

## 3. Inflation and units

`$print <amount>` requires Manager privileges and an explicit reaction confirmation. Printing adds the amount to the pool and updates:

```
index_after = index_before × (1 + amount / supply_before)
real_amount = nominal_amount / inflation_index
```

The index starts at 1 and does not decay in v1. Wallets, valuations, and net worth display real purchasing-power values; command amounts, wages, treasury cash flows, and ledger entries use nominal integer nuggies. Printing 200B into a 1T supply produces index 1.2; an unchanged 10B nominal wallet is then worth 8.33B real.

Amounts must be finite, positive where required, and fit the database's signed 64-bit representation. Fractional percentages are permitted only where documented. Nonfinite inputs must return player-facing errors rather than corrupting state.

## 4. Real calendar, weekends, and recovery

Production uses the **host server's local calendar**, including its timezone and daylight-saving transitions. A game day is local midnight up to the next local midnight; DST days can be 23 or 25 hours. `GAME_TIMEZONE` can explicitly select an IANA zone for testing or another deployment, but an unset value follows the host. Discord guilds do not expose a universal timezone, so all economies in one deployment use its clock.

`server_state.game_day` is the current unsettled date. Closing settles that date **before** advancing it. Friday's attendance is paid at the Friday close (Saturday midnight); the Saturday and Sunday closes do not produce or pay wages. Monday opens at local Monday midnight.

The scheduler checks persisted dates and catches up after restart. Each recovery batch commits its closes and cursor advances in one database transaction. A repeated check of an already-current date does nothing. Recorded attendance can be paid only once; catch-up never invents attendance for offline days. Missed business days still advance vesting and revenue history. A failure rolls back that close and leaves it eligible for retry. Commands synchronize the calendar before acting, so late scheduler execution does not admit a weekday action into a stale day.

Markets are closed on weekends: no clock-ins, production, wages, investments, dividend execution, or acquisition acceptance. Founding, job management, funding-round preparation, and sending offers remain available. Financial commands explain that users must retry Monday; **there is no queued order or guaranteed Monday execution**. Existing companies retain their last business close on the valuation board. New companies without a close show an initial quote.

Production ignores `TICK_INTERVAL_SECONDS` in normal calendar mode. `GAME_TIME_MODE=accelerated` replaces the wall-clock schedule with interval-driven development days and enables Manager `$setday` / `$forcetick`; those controls are rejected in calendar mode. Reminder preferences are opt-in and deduplicated per game day; reminders run partway through a business day.

## 5. Events

Each new week rolls zero to two events, with a business date, industry (or whole market), multiplier, and headline. Initial bootstrap establishes the current week's calendar. Weekly rolling must not duplicate on restarts.

An event affects its own business day's production and live valuation. `$event <industry|all> <multiplier> <headline>` creates an event for the current date. Multipliers must be finite and greater than zero, with a 100× ceiling. The current-date event is included when that same date closes; advancing first would wrongly miss it. Multiple active event multipliers combine according to `events.active_multipliers`.

Events influence operating decisions and the attractiveness of equity funding. They do not create a secondary market or a way to sell holdings on demand.

## 6. Player progression

The first useful sequence is `$help` → `$jobs` → `$apply <id>` → `$clockin` → next daily close → `$balance`. State jobs accept immediately. A player holds at most one job per guild. Private applications await the company owner's decision.

A clock-in marks participation for the current business day. It does **not** pay an immediate wage. Clock-out withdraws that participation before settlement. Clocked-in employees cannot be fired or swept into an acquisition before settlement. A voluntary quit must wait for close or explicitly clock out to forfeit the shift. Quitting or being fired ends employment; vested holdings remain, unvested grants are forfeited. The rule is close-time attendance, not hourly wages or accrued partial shifts.

Consecutive business-day clock-ins build a streak across weekends. Milestones at 10, 20, and 60 days award one-time taxed pool bonuses of 25K, 75K, and 250K. The milestone ledger prevents repeated rewards from clock-in/out cycling. Players can opt into a daily reminder with `$reminders on` and turn it off at any time.

Owners can promote employees with a positive wage increase. Wages and productivity are snapshotted on employment; changing a reusable job listing does not silently rewrite existing contracts.

## 7. State companies

Bootstrap creates McNuggie's, Public Works, and The Postal Service with seven always-open jobs. Wages range from 3,000 to 3,500 nug. State jobs are reusable roles and auto-accept applicants. State companies have no shares, cannot receive investments or be acquired, and generate no private revenue.

State payroll draws from the pool and is paid pro-rata if it cannot all be covered. A shortage is surfaced in the close report. Opening/closing state agencies is not a Manager feature in v1.

## 8. Private companies

`$found <ticker> <name> <industry>` creates an active private company. Tickers are 1–4 alphanumeric characters and unique within a guild, including retired tickers. The founder pays:

```
fee = base_founding_fee × fee_multiplier ** currently_owned_active_companies
```

Defaults produce fees of 50K, 200K, 800K, and 3.2M. The fee returns to the pool. A company starts with an empty treasury and 1,000,000 shares held by its founder. Ownership grants management authority and is distinct from a percentage shareholding.

`$deposit <ticker> <amount>` moves an owner's wallet cash into their company's treasury without creating shares or money. The deposit is not a loan: there is no privileged withdrawal or interest. Cash can return through a taxed pro-rata dividend, employment, or a company sale.

`$company` shows treasury, capitalization, employees, output, and valuation. Industry selection is a fixed set from `config.INDUSTRIES`, with common aliases accepted.

## 9. Jobs and the daily close

Owners post a title, description, wage, and optional share grant/vesting duration with `$postjob`. A private posting is a **reusable hiring role**, not an implicit one-person seat; owners review applicants and control hiring. This makes expansion an intentional owner action. Application listings and Discord output must stay within platform message limits.

Each clocked-in worker produces their fixed productivity (default 12,000 nug), adjusted by that day's industry event, and costs the agreed wage. A company with excessive wages can fail even if its treasury began the day solvent.

For the current unsettled business date, one transaction performs:

1. Read that date's events.
2. Advance active equity grants and mint newly vested shares.
3. Calculate tentative output for every active private company.
4. Compute one aggregate recession ratio and transfer realized revenue pool → treasuries.
5. Pay private payroll from the resulting treasuries, withholding tax; pay state payroll from the pool.
6. Apply insolvency and bankruptcy cleanup.
7. Clear attendance and persist business-day closing quotes.
8. Advance the date; initialize the next week's events when entering Monday.

On a weekend date, clear attendance and advance without production, wages, vesting, or a new business closing quote. Revenue always precedes payroll. Rounding cannot cause the pool or an account to go negative.

Private payroll is pro-rata when short. Three consecutive insolvent business closes trigger bankruptcy. A solvent close resets the counter. Bankruptcy voids outstanding offers and rounds, closes jobs, ends employment/grants, removes holdings, retires shares, returns remaining cash to the pool, and deactivates the company atomically. Inactive companies cannot participate in subsequent financial actions.

## 10. Equity and vesting

Job grants vest over business closes while employment remains active. Clock-in is required for wages/production, but grant vesting follows continued employment, not attendance. For a grant of N shares over D days, cumulative vested shares are `floor(N × elapsed / D)` with the final close delivering the exact remainder.

Newly vested shares are minted into holdings and added to total shares. Grants are dilutive; unvested shares do not exist in the cap table, receive no dividends, and are not cashed out in an acquisition. Termination forfeits only the unvested amount. The invariant is `company.total_shares == sum(holdings.shares)` for an active private company.

## 11. Raising and investing

`$raise <ticker> <amount> <equity%>` opens at most one round per company. The quoted percentage describes the new investors' approximate stake after full subscription:

```
new_shares = round(existing_shares × percentage / (100 - percentage))
```

With 1,000,000 existing shares, offering 10% mints 111,111 new shares at full subscription, not another million. Integer rounding is explicit. Investments move wallet cash into the treasury and mint the corresponding portion; no unpurchased shares are issued. A fully subscribed round closes automatically. `$closeround <ticker>` lets the owner or a Manager close an unfinished round, retaining contributions and shares already issued; it does not refund completed investments.

`$invest` requires annualized qualifying income of at least 200,000 nug, based on the trailing 30 business-day income window and 250-business-day annualization. Qualifying income is defined by the ledger's wage/dividend categories; deposits and acquisition proceeds do not manufacture eligibility. Read the live `$help invest` and service calculation for exact current eligibility.

An investor cannot buy more than the remaining round. Tiny inputs that would buy no shares are rejected. Buying shares is subject to the weekday gate. There is no `$sell`, transfer marketplace, bid/ask spread, or market-maker redemption in v1.

## 12. Dividends and tax

`$dividend <ticker> <amount>` distributes existing company cash across actual shareholders. The sum of all gross allocations equals the amount; integer remainders are distributed deterministically. Each recipient's tax goes to the pool and the remainder to their wallet. State companies and unfunded payouts are rejected.

The default tax rate is 15%, adjustable from 0% up to but not including 95%. Wages, streak bonuses, dividends, and acquisition shareholder payouts are taxed. Founding fees and capital deposits are transfers with their own rules, not taxable income. Repeated concurrent commands cannot spend the same treasury twice.

## 13. Valuation and leaderboards

```
avg_daily_revenue = trailing realized revenue over the business-day window
annual_revenue = avg_daily_revenue × 250
nominal_value = treasury + annual_revenue × 3 × industry_sentiment
real_value = nominal_value / inflation_index
share_price = real_value / total_shares
net_worth = wallet / inflation_index + sum(shares_owned × share_price)
```

The revenue window is ten business days and includes recorded zero-output closes. Live weekday quotes respond to cash flows, production history, inflation, and sentiment. `Δ today` compares the current real share price with the last actual business close; a company with no baseline shows zero. Closing records are persisted after payroll, before the date advances.

Weekend quotes retain the previous close, including Friday's event sentiment. A weekend deposit or print does not rewrite that closing quote; real wallet values still reflect the current inflation index. This is a quoted-price display convention, not a promise to redeem equity at that price.

`$market [page]` / `$stocks` is the company valuation board. `$leaderboard` / `$rich` ranks players by real wallet plus holdings value. These are distinct views. A high company valuation is not treasury cash.

## 14. Acquisitions

`$acquire <acquirer> <target> <amount>` creates or replaces the pair's open offer; money is not reserved until acceptance. `$accept <acquirer> [target]` resolves an offer to a company owned by the caller; if the acquirer has offered on several companies owned by that caller, the target argument is required. `$decline <acquirer> [target]` rejects an offer with the same disambiguation rule. Acceptance is a weekday operation and revalidates ownership, activity, and available funds.

Acceptance atomically distributes the acquirer's payment pro-rata to all target shareholders, with tax; transfers the target treasury and open job listings to the acquirer; terminates all target employment and unvested grants; voids other offers/rounds; wipes the target holdings and share count; and deactivates the target. Employees do not transfer and must reapply. There is no severance in v1.

The acquirer gains cash and recruiting listings, not automatically productive staff or inherited revenue history. Its valuation follows the formula; there is no scripted acquisition price bump.

## 15. Commands and bug reports

Player commands: `$help [command]`, `$balance` (`$bal`), `$jobs [page]`, `$apply`, `$clockin`, `$clockout`, `$reminders on|off`, `$quit [ticker]`.

Owner/business commands: `$found`, `$company`, `$postjob`, `$applicants`, `$hire`, `$fire`, `$promote`, `$deposit`, `$raise` (`$fundraise`), `$closeround`, `$invest`, `$dividend` (`$div`), `$acquire`, `$accept <acquirer> [target]`, `$decline <acquirer> [target]`.

Information: `$market [page]` (`$stocks`), `$leaderboard` (`$rich`), `$pool`, `$today`.

Managers: `$print`, `$taxrate`, `$event`, `$stats` (`$macro`, `$dashboard`). `$setday` and `$forcetick` are accelerated-development-only. Manager means the configured role or Manage Server permission. Every permission check is enforced on the server, never trusted from a UI button.

`/bug` opens a small modal for summary, reproduction details, and expected behavior. Its response is private to the caller and links to a prefilled public GitHub issue. The player reviews and submits using their own GitHub account; the bot holds no GitHub write credential and does not silently publish reports. Reports include the release identifier, not tokens, message histories, or automatically collected user/guild IDs. Form invocation is rate limited. Reports should avoid private information.

## 16. Persistence and concurrency

SQLAlchemy models live in `src/tendies/models.py`. Production uses a single bot process with a persistent SQLite database, WAL, foreign keys, full synchronization, a busy timeout, and serialized application transactions. An exclusive process lease prevents two local bot processes from operating on the same SQLite path. This is a single-host design, not an active/active cluster. Postgres remains supported by the ORM but is not required for this deployment.

Tables: `server_state` (guild calendar/pool/policy); `users` (guild wallets); `player_profiles` (streaks/reminders); `companies`; `holdings`; `jobs`; `employment`; `equity_grants`; `applications`; `funding_rounds`; `offers`; `events`; `transactions`; and `market_closes` (business closing quotes).

Company IDs are internal; player-facing identifiers are tickers and Discord members. Guild filters must be present at every lookup boundary. Ledger rows retain operation type, source/destination, amount, game date, and optional player/company attribution. Money movements, share movements, and close cursors commit together or roll back together.

`create_all` creates missing tables, not arbitrary column migrations. Future schema changes require explicit migration and restore tests; deploying old code does not reverse a database migration.

## 17. Tunables and pace

`src/tendies/config.py` is authoritative for constants. Starting pool: 1T. First founding fee: 50K, multiplier: 4. Founding shares: 1M. Default productivity: 12K/worker. Tax: 15%. Aggregate daily revenue cap: 10% of pool. Revenue multiple: 3. Annualization: 250 business days. Accredited threshold: 200K/year over 30 business days. Revenue window: 10 business days. Insolvency grace: 3 business days. Weekly events: at most 2.

Streak bonuses shorten the initial wait: a fry cook nets 2,550/day, and the taxed 10-day bonus is another 21,250. At the default settings the first founding fee becomes reachable around business day 12 if no money is spent elsewhere. This is a slow social economy on a real calendar; accelerated mode exists for playtesting. Changes to pace should be tested as complete multi-player sequences, not only individual formulas.

## 18. Release and recovery acceptance

A release must pass token-free tests covering conservation, isolation, permission checks, share accounting, bankruptcy/acquisition cleanup, Friday settlement, current-date events, DST/local dates, repeated recovery, concurrent balance updates, quote freezing, bug reporting, and backup/health tooling.

The production pipeline must keep credentials and SQLite outside release folders; use the deployer SSH environment mechanism; run a locked dependency install and tests; preserve a consistent database backup before release; confirm the running release's fresh Discord-ready heartbeat; and roll back code/environment on a failed deployment. systemd restarts the bot after failure and starts it after boot. Calendar recovery remains necessary after restart.

A desktop timer polls only the configured public repository's `main`. Tests run in the candidate checkout before activation; secrets are not passed to those tests/builds. The poller has no GitHub write token and needs no inbound cloud-agent access. A PR and passing CI are the collaboration path; merging `main` authorizes production execution. Access to merge is therefore operationally privileged. Local deployer locks prevent overlapping release changes.

Online SQLite backups are checked before retention; restore must be rehearsed against a disposable copy. Backups on the desktop survive process failures and releases but do not protect against loss of its disk: copy verified snapshots to another machine for that. A service-ready check proves connection and database access; it does not prove every real Discord interaction. No test suite can promise zero bugs—`/bug`, logs, targeted regressions, and rollback support continued operation.

## 19. Deliberately deferred

Secondary player-to-player share trading, weekend order queues, IPOs, hostile takeovers, bonds, price drift, inflation decay, adjustable state agencies, acquisition severance, and a remote authenticated deployment MCP server are outside this release. The future deployer MCP transport can call the same tested release commands without changing the game's persistence contract.
