# Tendies — Design Spec

A Discord economy bot where each server is a self-contained capitalist sandbox. Players start broke, grind state jobs, found companies, hire and fire each other, raise capital, trade equity, and acquire rivals. The server's moderators act as a central bank and treasury rolled into one.

Currency: **nuggies** (short unit: `nug`). Command prefix: `$`. One economy per Discord server (guild) — nothing crosses server boundaries.

This is the v1 spec. Anything marked *(v2)* is deliberately out of scope for the first build but noted so the schema doesn't paint us into a corner.

---

## 1. The core idea in one loop

Everything in the game is one circuit of money moving between three places: the **pool**, **wallets**, and **company treasuries**.

```
                    $print (Manager, inflationary)
                            │
                            ▼
        ┌──────────────────────────────────────┐
        │              THE POOL                  │  starts at 1,000,000,000,000 nug
        └──────────────────────────────────────┘
           │  ▲                       │       ▲
  state    │  │ taxes &               │       │ taxes, founding fees,
  wages    │  │ fees          private │       │ bankrupt treasuries
           ▼  │               revenue ▼       │
        ┌─────────┐                ┌──────────────┐
        │ WALLETS │ ◄── wages ──── │  TREASURIES  │
        └─────────┘ ── invest ──►  └──────────────┘
                  ── dividends ◄──
```

Money leaves the pool two ways: **state-company wages** (the faucet that keeps broke newbies earning) and **private-company revenue** (companies "selling" their output to the market, where the market's cash *is* the pool). Money returns to the pool three ways: **taxes**, **fees**, and **bankrupt company treasuries**.

The interesting consequence, which falls out of the design rather than being bolted on: the Tendies Managers have to *govern*. If state payroll plus realized revenue drains the pool faster than taxes refill it, the pool shrinks, companies can't realize full revenue, and the server tips into recession. Managers can raise taxes (and annoy everyone), cut state jobs (and starve the newbie pipeline), or print money (and eat the inflation hit). There's no neutral move. That tension is the game.

---

## 2. The pool and the money supply

The pool is a single number per server, seeded at **1 trillion nug**. It is the only reservoir; every nuggie that exists anywhere was drawn from it.

Revenue draws *from the pool*, it is not minted fresh. This is the decision that makes the pool mean something. When a company sells its daily production, the buyer is "the market," and the market pays out of the pool. A healthy pool pays full output; a draining pool can't, and everyone's revenue scales down together (see §9, the recession step).

`$print` is the only way to add genuinely new money, and it's the nuclear option — see §3.

```
You:      $pool
Tendies:  🍗 Server Treasury
          Pool: 842,103,920,000 nug
          Inflation index: 1.084  (everything shown in real terms is ÷1.084)
          Tax rate: 15%
          Money supply (pool + all wallets + all treasuries): 1,000,000,000,000 nug
```

---

## 3. Currency and inflation

`$print <amount>` (Manager only) raises the pool by `amount`. It always succeeds — there's no ceiling — but it has a cost that hits everyone who already holds wealth.

The model is a single `inflation_index`, starting at `1.0`:

```
index_after = index_before × (1 + amount / money_supply_before)
```

Then **every valuation, wallet, and leaderboard figure is shown in real terms** by dividing nominal nuggies by the index. After a big print, nobody's nominal balance changes, but everyone's *real* net worth drops, and the people hurt most are exactly the ones who already built wealth. That's the disincentive — printing is a tax on savers, paid invisibly.

Worked example. Money supply is 1.0T, index is 1.0. A Manager prints 200B to bail out the pool during a crisis:

```
index_after = 1.0 × (1 + 200,000,000,000 / 1,000,000,000,000) = 1.20
```

A player whose wallet held 10B nug still has 10B nominal, but their real net worth on `$leaderboard` now reads `8.33B (real)`. Every company valuation drops by the same 1/1.20 factor. Nothing was confiscated; everything just got worth less.

**The index never decays in v1.** Inflation is sticky — that's the point. The economy "grows back into it" only as real production continues over time. *(v2: allow the index to drift down when real revenue rises quarter over quarter, i.e. the economy genuinely outgrew the money it printed.)*

```
You:      $print 200B
Tendies:  ⚠️ This will raise the pool by 200,000,000,000 nug and push
          inflation from 1.000 → 1.200. Every real balance on the server
          drops ~16.7%. React ✅ within 60s to confirm.
You:      [reacts ✅]
Tendies:  💸 Printed 200B. Pool: 1,042B. Inflation index: 1.200.
          The market will remember this.
```

---

## 4. Time and market hours

Game-time is tracked in the database, not inferred from a startup flag — if the bot restarts on a Wednesday it must not think it's Monday again.

- `game_day` is a date, and `weekday` derives from it.
- The bot is **seeded once** with a starting weekday (`$setday monday` or a config value on first run).
- A scheduled **tick** advances the game by one day each cadence (default: one real day = one game day; configurable faster for testing).
- **Monday–Friday are full ticks** (production, wages, events, price movement — see §9).
- **Saturday & Sunday are closed ticks**: the day advances, but there is no production, no wages, no events, and share prices freeze. Back-office actions — founding, applying, hiring, posting jobs, sending acquisition offers — still work. The market floor is empty but the lawyers are still in the building.
- `$setday <weekday>` (Manager) corrects drift if the schedule ever slips.

```
You:      $today
Tendies:  📅 Tuesday — market OPEN.
          Next event window: rolled Monday, see $market for today's movers.

You:      $today          (on a Saturday)
Tendies:  📅 Saturday — market CLOSED. Prices frozen until Monday.
          You can still found companies, post jobs, and review applicants.
```

---

## 5. Market events

Events are the day-trading hook. They're rare on purpose.

- At each **Monday tick**, roll **0–2 events for the week**. Each rolled event is assigned a random business day (Mon–Fri), an industry, and a multiplier.
- An event lasts **one business day only**. On its day, the affected industry's multiplier replaces the default `1.0`; the next day it's back to normal.
- Multipliers run both directions: a medicine breakthrough might be `×2.5`, an oil glut `×0.4`, a sector scandal `×0.6`. A rare server-wide crash applies a `×0.5` to *every* industry for one day.
- Two sources: **random** (the weekly roll) and **admin** (`$event`), so a Manager can hand-author a "BREAKING:" storyline and fire it for the current day.

The multiplier hits two things at once: that day's **production revenue** for companies in the industry (§9, step 4) and their **valuation** for the day via sentiment (§13). So a player who buys into Medicine the business day before a breakthrough, then sells into the spike, actually profits — the price moved because the math moved, not because of a hardcoded "stock goes up" rule.

```
Tendies:  📰 BREAKING — Monday's roll is in.
          This week: 🧬 Wednesday — Medicine breakthrough (×2.5).
          Plan accordingly.

You:      $event energy 0.4 "Pipeline rupture in the Gulf"   (Manager)
Tendies:  📰 BREAKING — Pipeline rupture in the Gulf.
          ⚡ Energy ×0.4 for the rest of today. Frackers, condolences.
```

---

## 6. Player lifecycle

You spawn with an empty wallet. The path out:

1. **Read the board.** `$jobs` lists open positions across the server, including the always-open state jobs.
2. **Apply.** `$apply <job_id>` files an application. Private employers review and choose; state jobs auto-accept.
3. **Clock in.** `$clockin` each business day does two things at once — it collects your wage that day *and* counts you as contributing to your employer's production. You're auto-clocked-out every night, so it's a daily ritual (the engagement hook).
4. **Build a stake.** Save wages until you can either pay a founding fee (§8) or clear the investor income gate (§11).
5. **Branch.** Found your own company, invest in others', or both. Quit any time with `$quit`; you keep whatever equity has vested and lose the rest.

```
You:      $balance
Tendies:  💰 @you — 47,300 nug (real). Unemployed. No holdings.

You:      $jobs
Tendies:  🍗 Open positions (page 1/3)
          [STATE] McNuggie's — Fry Cook — 3,000 nug/day — $apply 1
          [STATE] Public Works — Pothole Tech — 3,500 nug/day — $apply 2
          MOON — Junior Driller — 6,000 nug/day + 5,000 sh vesting/60d — $apply 14
          GKAS — Sauna Attendant — 4,200 nug/day — $apply 19

You:      $apply 1
Tendies:  ✅ Hired at McNuggie's as Fry Cook (state job, auto-accepted).
          Wage 3,000 nug/day. $clockin on weekdays to get paid.
```

---

## 7. State-owned companies (the bootstrap)

The first thing a new server needs is somewhere for broke players to earn, or the whole economy deadlocks (newbies need jobs → jobs need companies → companies need treasuries → treasuries need workers → workers need to not be broke). State companies break that loop.

- Seeded at server setup, funded directly from the pool. Examples: **McNuggie's** (food service), **Public Works** (infrastructure), **The Postal Service** (logistics).
- They have **no shares, no equity, cannot be invested in, and cannot be acquired**. They are pure faucets.
- Their jobs are **always open and auto-accept**. Low wage, no equity — enough to live on, not enough to get rich.
- They **don't produce revenue.** They only pay wages, straight from the pool. That's the simulation of a government agency: a budget line, not a profit center.

Because state wages are the pool's main outflow at the bottom of the economy, they're also the early-warning signal for a crisis: when the pool can't cover state payroll, Managers have to act (tax, cut, or print).

*(v2: Managers can `$openstatejob` / `$closestatejob` to tune the faucet — open more agencies in a downturn, trim them when the pool is tight, which is its own little fiscal-policy game.)*

---

## 8. Companies

`$found <ticker> <name> <industry>` creates a private company. The ticker is a **1–4 character code, unique per server** (4 is the classic shape; shorter is allowed, like real stock tickers), and it's the company's handle everywhere — jobs, trades, acquisitions.

Founding costs a fee that goes **to the pool** (a sink), and the fee scales with how many companies you **currently own**:

```
fee = base_founding_fee × (fee_multiplier ^ companies_currently_owned)
```

With `base = 50,000` and `multiplier = 4`: your 1st company costs 50K, 2nd costs 200K, 3rd costs 800K, 4th costs 3.2M. This kills company-spam without punishing a serial founder who sells before building again — basing it on *currently owned* rather than *ever founded* means offloading a company brings your next fee back down.

Founding mints **1,000,000 shares**, all to you. You own 100%.

```
You:      $found MOON "Moon Mining Inc." materials
Tendies:  🏭 Founded Moon Mining Inc. (MOON) in Materials.
          Fee: 50,000 nug → pool. You hold 1,000,000 sh (100%).
          Treasury: 0 nug. Post a job with $postjob MOON to start producing.

You:      $found SPCE "Spacey Adventures LLC" tech
Tendies:  🏭 Founded Spacey Adventures LLC (SPCE) in Tech.
          Fee: 200,000 nug (your 2nd company) → pool. You hold 1,000,000 sh (100%).
```

`$company <ticker>` shows the full picture:

```
You:      $company MOON
Tendies:  🏭 Moon Mining Inc. (MOON) — Materials
          Owner: @you
          Treasury: 1,240,000 nug
          Shares: 1,000,000 total
            @you ............ 920,000 (92.0%)
            @investor1 ...... 80,000 (8.0%)
          Employees clocked in today: 4 / 6
          Avg daily revenue (10d): 96,400 nug
          Valuation (real): 73,540,000 nug → share price 73.54 nug
```

---

## 9. Jobs, employment, and the daily tick

### Posting and hiring

`$postjob <ticker>` opens an interactive prompt for a title, description, **daily wage**, and an optional **equity grant** (share count + vesting period in business days). Applicants queue up; the owner reviews with `$applicants <ticker>` and chooses `$hire` / `$fire`.

```
You:      $postjob MOON
Tendies:  Reply with: <title> | <daily wage> | <equity sh, or 0> | <vest days, or 0>
          Then paste the description in your next message.
You:      Senior Driller | 8000 | 10000 | 90
You:      Operate the rigs, hit quota, don't fall in. Night shifts available.
Tendies:  📋 Posted: MOON — Senior Driller — 8,000 nug/day + 10,000 sh vesting over 90d.
          Job ID 22. Applicants will show in $applicants MOON.

You:      $applicants MOON
Tendies:  📨 MOON — Senior Driller (job 22)
          a) @newbie42 — net worth 12K, currently Fry Cook @ McNuggie's
          b) @veteran — net worth 4.1M, owns GKAS
          $hire MOON a   |   $hire MOON b   |   $fire later with $fire MOON @user

You:      $hire MOON a
Tendies:  ✅ @newbie42 hired at MOON, 8,000 nug/day, 10,000 sh vesting over 90 business days.
```

### Each clocked-in worker is both an asset and a liability

A clocked-in employee **produces** output (revenue, into the treasury) and **costs** their wage (out of the treasury) every business day. Hire faster than you can produce and payroll eats the treasury — that's bankruptcy, and it's supposed to be possible.

### The daily tick — order is load-bearing

This runs once per business day. Get the order wrong and solvent companies look bankrupt or shortages become first-come-first-served. The sequence:

**Step 1 — Advance the day.** Move `game_day` forward, update `weekday`. If it's a weekend, do only this and step 7 (closed tick). If it's Monday, also roll the week's events (§5).

**Step 2 — Apply today's events.** Load any event for today (rolled or admin) and set the active industry multipliers. Must happen before production so multipliers actually land.

**Step 3 — Vest.** Advance every active equity grant; migrate newly-vested shares from `equity_grants` into `holdings`.

**Step 4 — Produce, then realize revenue (the recession step).** For each active private company:

```
tentative_revenue = Σ(clocked_in_worker.productivity) × industry_multiplier
```

Sum tentative revenue across *all* companies. If that total exceeds the **daily realizable cap** (a fraction of the current pool, default 10%), scale **every** company down by the same ratio, then move nuggies `pool → treasury`. The cap applies to the *aggregate*, not per company — that's what makes a shortage shared instead of a race. Healthy pool: everyone produces at full. Draining pool: everyone earns less together, smoothly. That's a recession players can feel.

**Step 5 — Pay payroll (revenue first, then wages).** Revenue already landed in step 4. Now pay each clocked-in worker their wage `treasury → wallet`. If `treasury < total payroll`, the company is insolvent: pay workers pro-rata what's left, flag the company. Persistent insolvency (default: 3 business days) → **bankruptcy**: treasury returns to pool, open offers and jobs void, holdings are wiped, company deactivates. Putting revenue before payroll is what makes "overhire and die" fair rather than arbitrary — a company that genuinely earned enough makes payroll; one that overhired doesn't.

State companies skip step 4 and pay wages straight from the **pool**. When the pool can't cover state payroll, that's the crisis bell.

**Step 6 — Withhold tax.** On every wage paid (and on dividends, when declared off-tick), send `tax_rate × amount` from `wallet → pool`. This is the refill loop that balances the state-wage drain.

**Step 7 — Auto clock-out.** Set every employee `clocked_in = false`. Tomorrow they must `$clockin` again.

```
You:      $clockin
Tendies:  ⏰ Clocked in at MOON. You'll be paid 8,000 nug at today's tick
          and you're contributing to MOON's production. See you at close.

          [at the daily tick]
Tendies:  🧾 Daily close — Tuesday
          MOON produced 102,000 nug (Materials ×1.0). Treasury → 1,198,000.
          Payroll: 6 workers, 41,000 nug paid out.
          Tax withheld: 6,150 nug → pool. Net to MOON treasury: +55,000.
```

> **Implementation note (resolved):** taxes are withheld *as part of* each wage/dividend payout (money.pay_wage / pay_state_wage / payout_prorata), so step 6 is folded into step 5 rather than being a separate pass. The order that matters — revenue (step 4) before payroll (step 5) — is preserved.

---

## 10. Equity and vesting

Equity grants from jobs vest over a set number of business days. A grant of 10,000 shares over 90 days vests `≈111 sh/day`; on the tick, vested chunks move into your `holdings`. Quit before full vesting and you keep only what's vested — the rest is forfeit. This is what gives "join a company, build a stake, then leave to start your own" real weight.

Vested shares are normal holdings: they earn dividends, count toward net worth, and get cashed out in an acquisition.

```
You:      $quit MOON
Tendies:  You've vested 3,330 / 10,000 sh at MOON (37 business days in).
          Quitting keeps the 3,330 vested and forfeits 6,670. Confirm with ✅.
You:      [reacts ✅]
Tendies:  👋 You left MOON. 3,330 MOON shares are yours. Wage stopped.
```

---

## 11. Investment and the accredited gate

`$raise <ticker> <amount> <equity%>` opens a funding round. The implied valuation is `amount / equity%`. Eligible investors fill it with `$invest`.

```
You:      $raise MOON 5000000 10
Tendies:  📈 MOON is raising 5,000,000 nug for 10% (1,000,000 new sh).
          Implied valuation: 50,000,000 nug. Open to accredited investors.
          $invest MOON <amount> to take a slice.
```

The **accredited-investor gate**: to `$invest`, your annualized income must clear **200,000 nug/yr**. Income is measured over a **trailing 30 business days** of wages + dividends received, annualized to a 250-business-day year:

```
annualized_income = (wages + dividends, last 30 business days) × (250 / 30)
```

Below the bar, you can still work, found, and hold equity from grants — you just can't buy into other people's rounds yet. It's a soft wall that gives grinding toward "real investor" a point.

```
You:      $invest MOON 1000000
Tendies:  ⛔ Accredited investors only. Your trailing income annualizes to
          83,400 nug/yr (need 200,000). Keep earning — or found your own.
```

When a round is open, an investor's `$invest MOON <amount>` buys `(amount / round_amount) × round_shares` of the newly-minted shares, paid wallet → treasury, shares minted into holdings. A round closes when fully subscribed (or a Manager/owner closes it).

---

## 12. Dividends

`$dividend <ticker> <amount>` (owner) distributes `amount` from the treasury **pro-rata across the cap table**. Dividends are taxed like wages (§9 step 6). They happen off-tick, whenever the owner chooses.

```
You:      $dividend MOON 800000
Tendies:  💸 MOON paid an 800,000 nug dividend across 1,000,000 sh (0.80/sh).
            @you (920,000 sh) → 736,000 (−110,400 tax) = 625,600 nug
            @investor1 (80,000 sh) → 64,000 (−9,600 tax) = 54,400 nug
          Tax → pool: 120,000 nug.
```

---

## 13. Valuation and net worth

Computed on read, never stored.

```
avg_daily_revenue = mean daily realized revenue over last 10 business days
annual_revenue    = avg_daily_revenue × 250
nominal_value     = treasury + annual_revenue × revenue_multiple × sentiment
real_value        = nominal_value / inflation_index
share_price       = real_value / total_shares
net_worth         = wallet + Σ(shares_held × share_price of that company)   (all real)
```

`sentiment` is the company's industry multiplier for the day — `1.0` normally, the event multiplier on an event day. So valuations breathe with events, which is the day-trader's whole reason to watch `$market`. `revenue_multiple` (default 3, a price-to-sales knob) sets how richly the market capitalizes earnings.

Two distinct views, two commands:

- `$market` (a.k.a. `$stocks`) — the **stock exchange**: companies, share prices, industry sentiment, today's movers. Frozen on weekends.
- `$leaderboard` (a.k.a. `$rich`) — **players ranked by real net worth**, with their holdings listed. This is where acquisitions and accumulated capital show up.

```
You:      $market
Tendies:  📊 The Nuggie Exchange — Wednesday  (🧬 Medicine ×2.5 today)
          TICKER  PRICE(real)   Δ today   INDUSTRY
          PFEZ     312.40 nug   ▲ +148%   Medicine   ← breakthrough
          MOON      73.54 nug   ▲ +0.4%   Materials
          SPCE      41.10 nug   ▼ −1.2%   Tech
          GKAS      18.90 nug   —  0.0%   Food

You:      $leaderboard
Tendies:  🏆 Richest tycoons (real net worth)
          1. @you — 43.2B nug — Moon Mining Inc. (MOON) · Spacey Adventures LLC (SPCE) · Gary's Kitchen and Sauna (GKAS)
          2. @rival — 11.8B nug — PfizerZ (PFEZ)
          3. @newbie42 — 612K nug — (employee, no companies)
```

---

## 14. Acquisitions

Acquisitions are **company-to-company**, paid from the acquirer's treasury. A solo player with cash but no company can't buy anyone — they'd found a shell first, which is correct.

`$acquire <acquirer> <target> <offer>` sends an offer and pings the target's owner. The target owner responds with `$accept <acquirer>` or `$decline <acquirer>` — keyed by the **acquirer's ticker**, not a random ID. From the seller's chair the natural question is "who's trying to buy me," so `$accept MOON` reads cleanly. One open offer per acquirer→target pair (a new one replaces the old). If two suitors circle the same target, `$accept MOON` vs `$accept ROCK` disambiguates itself — no ugly IDs anywhere.

On accept:

1. The offer is paid from the acquirer's treasury, distributed **pro-rata to the target's entire cap table** (owner gets the lion's share; investors and equity-holding employees get their slices automatically). This is the universal payout path — same machinery as dividends.
2. The acquirer absorbs the target's **treasury and production capacity** (its jobs and clocked-in workers transfer to the acquirer).
3. The target's **wage-only employees are laid off** — the company ceased to exist. They can re-apply if the new owner wants them back ("just apply, I'll let you in"). Equity holders aren't "fired from their equity"; they were already cashed out in step 1. Only the *job* ends. No employment transfer — that path is a swamp of inherited wage terms and reset vesting, and layoffs are the most true-to-life thing in M&A anyway.

**The stock reaction is emergent, not hardcoded.** Run the deal through the valuation formula: the acquirer paid cash (treasury down) and gained production (revenue up). If the production gained is worth more than the cash bled, the acquirer's valuation rises — an accretive deal, stock up. If they overpaid, treasury dropped more than production added and their *own* stock falls. Value-destroying acquisitions are real; let the math decide whether the market should be happy. Don't force "stock goes up on acquisition."

> **Implementation note (resolved):** "absorb production capacity" means the target's **open jobs** transfer to the acquirer (re-pointed company_id) and its clocked-in *equity-cashed-out* workers don't transfer as employees — per step 3, all of the target's employees end employment (the company ceased to exist). The acquirer gains the *jobs* (capacity to hire), the treasury, and any open positions; existing employees are laid off and re-apply. Vesting grants tied to ended employment are forfeited (unvested) — vested shares were already cashed out in step 1. Keep it simple and faithful: transfer treasury + open jobs, end all target employment, deactivate the target.

**Severance is off in v1.** It opens questions not worth answering yet (on top of the offer or skimmed from it? flat or scaled by tenure?), and laid-off workers already have the re-apply path. The acquisition payout stays one clean pro-rata operation. *(v2: acquirer can optionally attach a severance pool that splits across non-equity staff.)*

```
You:      $acquire MOON SPCE 5B
Tendies:  🤝 MOON offers 5,000,000,000 nug to acquire SPCE.
          @spce_owner: $accept MOON or $decline MOON.

@spce_owner: $accept MOON
Tendies:  🤝 Deal closed. MOON acquires SPCE for 5,000,000,000 nug.
          Cap table paid out:
            @spce_owner (90%) → 4,500,000,000 nug (−tax)
            @investor1 (10%)  → 500,000,000 nug (−tax)
          SPCE's treasury + 3 jobs + 2 clocked-in workers → MOON.
          1 wage-only employee laid off (re-apply to MOON if wanted).
          📊 MOON's production base grew. Watch its price at the next tick.
```

---

## 15. Command reference

| Command | Who | Purpose |
|---|---|---|
| `$balance` / `$bal` | anyone | Your wallet (real), job, and holdings |
| `$jobs` | anyone | List open positions |
| `$apply <job_id>` | anyone | Apply to a job (state jobs auto-accept) |
| `$clockin` / `$clockout` | employee | Collect wage + contribute production for the day |
| `$quit <ticker>` | employee | Leave a job; keep vested equity, forfeit the rest |
| `$found <ticker> <name> <industry>` | anyone | Found a company (scaling fee → pool) |
| `$company <ticker>` | anyone | Company detail: treasury, cap table, revenue, valuation |
| `$postjob <ticker>` | owner | Post a job (title, wage, optional equity grant) |
| `$applicants <ticker>` | owner | Review applicants |
| `$hire <ticker> <applicant>` / `$fire <ticker> @user` | owner | Hire / fire |
| `$raise <ticker> <amount> <equity%>` | owner | Open a funding round |
| `$invest <ticker> <amount>` | accredited | Buy into an open round |
| `$dividend <ticker> <amount>` | owner | Pay a pro-rata dividend |
| `$acquire <acquirer> <target> <offer>` | acquirer owner | Send an acquisition offer |
| `$accept <acquirer>` / `$decline <acquirer>` | target owner | Respond to an offer |
| `$market` / `$stocks` | anyone | The stock exchange |
| `$leaderboard` / `$rich` | anyone | Players by real net worth |
| `$pool` | anyone | Pool, inflation index, tax rate, money supply |
| `$today` | anyone | Game day + market open/closed |
| `$print <amount>` | **Manager** | Add money to the pool (inflationary) |
| `$taxrate <percent>` | **Manager** | Set the tax rate |
| `$setday <weekday>` | **Manager** | Correct the game day |
| `$event <industry> <multiplier> "<blurb>"` | **Manager** | Fire an event for today |
| `$forcetick` | **Manager** | Advance the game one day immediately (ops/testing) |

"Manager" = a member with a configurable **Tendies Manager** role (or basic mod perms). Everything else is open to all members.

---

## 16. Data model

Postgres, scoped per guild. Each server is an independent economy. (See `src/tendies/models.py` for the authoritative schema; the columns below match it, with `equity_grants.days_elapsed` and `transactions.{game_day,user_id,company_id}` added for correctness.)

**`server_state`** — the macro singleton.
`guild_id` (pk) · `pool_balance` · `inflation_index` (default 1.0) · `game_day` (date) · `weekday` · `tax_rate` · `base_founding_fee` · `fee_multiplier`

**`users`**
`(guild_id, user_id)` (pk) · `wallet`. Net worth is computed on read, never stored.

**`companies`**
`id` (pk) · `guild_id` · `ticker` (unique per guild, 1–4 chars) · `name` · `owner_id` (null for state-owned) · `industry` · `treasury` · `total_shares` · `is_state` (bool) · `active` (bool) · `insolvent_days` (counter for bankruptcy)

**`holdings`** — outright, vested equity. Dividends, acquisitions, and net worth all read this one table.
`(company_id, user_id)` (pk) · `shares`

**`equity_grants`** — vesting in progress, kept separate from `holdings`.
`id` (pk) · `employment_id` · `total_shares` · `vested_shares` · `vest_days` · `days_elapsed` · `daily_vest`

**`jobs`**
`id` (pk) · `company_id` · `title` · `description` · `daily_wage` · `productivity` · `equity_shares` (nullable) · `vest_days` (nullable) · `open` (bool)

**`employment`**
`id` (pk) · `company_id` · `user_id` · `job_id` · `daily_wage` · `productivity` · `clocked_in` (bool) · `hired_at`

**`applications`**
`id` (pk) · `job_id` · `user_id` · `status` · `applied_at`

**`offers`** — acquisitions. Unique on `(acquirer_id, target_id)` where status is open.
`id` (pk) · `acquirer_id` · `target_id` · `amount` · `status`

**`events`**
`id` (pk) · `guild_id` · `game_day` · `industry` · `multiplier` · `source` (random/admin) · `blurb`

**`transactions`** — the ledger. The income gate is just a query over this; so is any audit.
`id` (pk) · `guild_id` · `ts` · `game_day` · `type` · `src` · `dst` · `amount` · `user_id` · `company_id` · `note`

Why `holdings` and `equity_grants` are first-class and separate: making holdings a real table means dividends, acquisition payouts, net worth, and keep-your-vested-shares-on-quit all become the *same* pro-rata operation over one table. Vesting lives apart so "earned but not yet yours" can't accidentally earn dividends or get cashed out early.

---

## 17. Tunable constants

These are the knobs that set game feel. (Authoritative values live in `src/tendies/config.py`.)

| Constant | Default | What it controls | Notes |
|---|---|---|---|
| `STARTING_POOL` | 1,000,000,000,000 | Initial money supply | The whole sandbox's headroom |
| `BASE_FOUNDING_FEE` | 50,000 | Cost of 1st company | **#1 knob for early-game pace** |
| `FEE_MULTIPLIER` | 4 | Fee growth per owned company | 50K → 200K → 800K → 3.2M |
| `SHARES_AT_FOUNDING` | 1,000,000 | Shares minted on founding | Big number = granular pricing |
| `STATE_WAGE` | 3,000–3,500/day | Entry state-job wages | The floor income |
| `PRIVATE_WAGE` (typical) | 5,000–8,000/day | Set by owners | Must beat state to attract talent |
| `PRODUCTIVITY` (per worker) | ~12,000/day | Revenue a worker generates | Keep > wage so labor is profitable |
| `REVENUE_MULTIPLE` | 3 | Price-to-sales in valuation | Higher = richer valuations |
| `BUSINESS_DAYS_PER_YEAR` | 250 | Annualization factor | Used in valuation + income gate |
| `RECESSION_CAP` | 10% of pool/day | Max aggregate daily revenue | The recession throttle |
| `DEFAULT_TAX_RATE` | 15% | Wage + dividend tax | Manager-adjustable, the refill |
| `ACCREDITED_THRESHOLD` | 200,000/yr | Investor gate | Annualized trailing-30 income |
| `INCOME_WINDOW` | 30 business days | Income measurement window | Trailing, annualized ×(250/30) |
| `INSOLVENCY_GRACE` | 3 business days | Before bankruptcy fires | Tune for forgiveness |
| `MAX_EVENTS_PER_WEEK` | 2 | Event frequency | Rare on purpose |
| `AVG_REVENUE_WINDOW` | 10 business days | Valuation revenue window | Trailing realized revenue |

A sanity check on early-game pace with these defaults: a fry cook nets ~2,550 nug/day after 15% tax. The first founding fee is 50,000, so roughly **20 business days** of grinding to start a company from zero.

---

## 18. The bugs that will actually bite

Three spots where subtle ordering or scope errors hide. Worth a test each:

1. **Revenue before payroll (§9 steps 4→5).** Reverse them and a company that earns plenty looks bankrupt because wages tried to pay before revenue landed. Test: a company whose only income is today's production should still make payroll.
2. **Aggregate recession scaling (§9 step 4).** The cap applies to *total* tentative revenue, scaling everyone by one ratio. Apply it per company and you've quietly rebuilt first-come-first-served. Test: two companies, pool too small for both, both should get the same fraction.
3. **Bankruptcy unwinding (§9 step 5).** When a company dies, return treasury to pool, void open acquisition offers involving it, close its jobs, and wipe its holdings — in that order, in one transaction. Test: bankrupt a company that has an open offer out *and* an open round, and confirm nothing dangles.

---

## 19. Roadmap

**v1 (this spec).** The full loop: pool, state jobs, founding, employment, production, the daily tick, wages, taxes, dividends, raises, the accredited gate, acquisitions, events, inflation, two leaderboards.

**v2.** Inflation decay when real revenue grows; Manager fiscal policy (open/close state jobs); acquisition severance pools; secondary share market; gentle daily price drift.

**v3.** IPOs, mergers of equals, hostile takeovers, sector indices, a bond market, Manager-run multi-week storylines.
