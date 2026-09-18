# Tendies 🍗

A Discord economy where your server earns wages, builds companies, hires friends, funds businesses, and argues about monetary policy. Each Discord server has its own nuggies (`nug`), treasury pool, businesses, and ledger.

**Start playing:** `$help` → `$jobs` → `$apply <id>` → `$clockin`. Wages arrive at the daily close. Save for a company, recruit workers, raise capital, and pay dividends. Use `/bug` to prepare a public [GitHub issue](https://github.com/frgmt0/tendies/issues), auto-labelled `bug` and `from-discord`; the drafted report is also logged so the owner has a record even if it's never submitted.

Production days follow the desktop server's local calendar, including DST. Weekdays run midnight to midnight; Friday wages settle at Saturday midnight. Markets close on weekends. Investment, dividends, and acquisition acceptance reopen Monday; there is no pending-trade queue. `$market` displays company valuations—v1 equity purchases happen through funding rounds, not a secondary share exchange.

[DESIGN.md](DESIGN.md) is the complete gameplay and operating contract, including accounting, time, failure recovery, and deferred features.

## Development

Requires Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Python 3.12 (uv can install it). Checks need no Discord token, SSH, or deployer, so this works in cloud-agent checkouts:

```sh
git clone https://github.com/frgmt0/tendies.git
cd tendies
./scripts/pipeline.sh check
```

To run a development bot, create a **separate Discord application/token** and use a separate database:

```sh
cp .env.example .env
chmod 600 .env
# Edit .env to supply the DEVELOPMENT token.
uv run tendies
```

Enable **Message Content Intent** and **Server Members Intent** in the [Discord Developer Portal](https://discord.com/developers/applications). Invite with `bot` and `applications.commands` scopes and View Channels, Send Messages, Embed Links, Read Message History, and Add Reactions permissions. `/bug` registers at startup; if an older invite lacks application-command access, reauthorize the bot with both scopes. Normal commands bootstrap a guild automatically.

The bot suppresses all mentions globally; the one exception is `$apply`, which pings the hiring company's owner. Without **Add Reactions**, confirmation prompts (`$print`, `$quit`, ...) fall back to typing `yes`/`no` instead of reacting ✅. Without **Embed Links**, the bot replies with a plain-text hint asking a moderator to grant the missing permission instead of silently failing. Calendar-mode startup that finds the stored game day ahead of the host's local today (leftover from an accelerated playtest or `$setday`) snaps the cursor back to today without settling anything — no closes, no payouts.

| Setting | Behavior |
|---|---|
| `DISCORD_TOKEN` | Required bot token. Use distinct production/development applications. |
| `DATABASE_URL` | Development example uses `sqlite+aiosqlite:///tendies-dev.db`; production path comes from `deploy.toml`. |
| `COMMAND_PREFIX` | `$` by default. |
| `MANAGER_ROLE` | `Tendies Manager`; Manage Server permission also grants Manager access. |
| `GAME_TIME_MODE` | `calendar` by default; `accelerated` explicitly enables development intervals. |
| `GAME_TIMEZONE` | Optional IANA zone; unset means the host's timezone, with DST. |
| `TICK_INTERVAL_SECONDS` | Only used in accelerated mode. Example: `60`. Does not shorten production days. |
| `GITHUB_REPOSITORY` | `frgmt0/tendies`, used for `/bug` issue links. |
| `TENDIES_HEALTH_FILE` | Production readiness heartbeat path; normally set by `deploy.toml`. |

For fast playtesting, set `GAME_TIME_MODE=accelerated` and `TICK_INTERVAL_SECONDS=60` in the development environment. `$setday` and `$forcetick` are available only in that mode. Tests use isolated databases and never connect to Discord.

## Commands

Amounts accept plain nuggies and K/M/B/T suffixes. Cash-flow inputs are nominal; wallet/net-worth displays are adjusted for inflation. `$help <command>` provides usage and live founding-fee information. Percent arguments (`$raise`, `$promote`, `$taxrate`) accept `10`, `10%`, or `0.5` interchangeably — the trailing `%` is cosmetic, and a bare number is always a percent, never a fraction, so `$taxrate 0.15` sets 0.15%, not 15%.

| Command | Who | Purpose |
|---|---|---|
| `$help [command]` | Anyone | Onboarding and command details |
| `$balance` / `$bal` | Anyone | Daily wage, clocked-in status, streak, last-close earnings (gross), next-close countdown, holdings, net worth |
| `$jobs [page]` / `$apply <id>` | Anyone | Find and apply for work; state jobs hire instantly; positions you've already applied to are marked `(applied)` |
| `$clockin` / `$clockout` | Employee | Join or withdraw from today's shift; wages settle at close |
| `$reminders on/off` | Anyone | Opt into a daily clock-in reminder |
| `$quit [ticker]` | Employee | Leave employment; retain vested equity |
| `$found <ticker> <name> <industry>` | Anyone | Found a company; the first fee is 50K |
| `$company <ticker>` | Anyone | Treasury, capitalization, employment, valuation, open job descriptions; owners also see pending inbound acquisition offers |
| `$postjob <ticker>` | Owner | Interactive reusable hiring-role posting; wage accepts K/M/B/T (e.g. `5K`), `cancel` aborts any prompt, and a malformed field line re-prompts once. Equity grants are capped at 10% of current shares and must vest over at least 5 business days |
| `$applicants <ticker>` | Owner | Review applications |
| `$hire <ticker> <letter or @user>` | Owner | Hire an applicant; owners cannot hire (or apply) into their own company |
| `$fire <ticker> @user` | Owner | End employment after any clocked-in shift settles |
| `$promote @user <percent>` | Owner | Raise an employee's wage; percent accepts `10`, `10%`, or `0.5` — a bare number is always a percent |
| `$deposit <ticker> <amount>` | Owner | Move your cash into the company without new shares |
| `$raise <ticker> <amount> <equity%>` | Owner | Open a funding round; alias `$fundraise`; percent accepts `10`, `10%`, or `0.5` — a bare number is always a percent |
| `$closeround <ticker>` | Owner/Manager | Close an unfinished round; completed investments stay |
| `$invest <ticker> <amount>` | Accredited player | Buy new equity in an open round, weekdays; owners cannot invest in their own round (use `$deposit`), and dividends from a company you own don't count toward accredited-investor income |
| `$dividend <ticker> <amount>` | Owner | Taxed pro-rata shareholder payout, weekdays; alias `$div` |
| `$acquire <acquirer> <target> <offer>` | Owner | Offer company cash to acquire another company; the offer must be at least the target's cash on hand, and a player cannot acquire a company they also own |
| `$accept <acquirer>` / `$decline <acquirer>` | Target owner | Respond to an acquisition offer; acceptance weekdays |
| `$market [page]` / `$stocks` | Anyone | Company valuations and open funding rounds; weekend quotes retain the last close |
| `$leaderboard` / `$rich` | Anyone | Players by real net worth |
| `$pool` / `$today` | Anyone | Economy and calendar status |
| `$print <amount>` | Manager | Create money with inflation; requires reaction confirmation |
| `$taxrate <percent>` | Manager | Set tax on wages, bonuses, dividends, and acquisition proceeds; percent accepts `10`, `10%`, or `0.5` — a bare number is always a percent, so `$taxrate 0.15` means 0.15% |
| `$event <industry or all> <multiplier> <headline>` | Manager | Affect the current business day's production and valuation; repeating `$event` for the same industry on the same day replaces the earlier event, and combined multipliers are clamped to 0.01x-100x |
| `$stats` / `$macro` | Manager | Macro dashboard and wealth concentration |
| `$setday <weekday>` / `$forcetick` | Manager in accelerated mode | Testing calendar controls; hidden from the `$help` landing page outside accelerated mode |
| `/bug` | Anyone in a guild | Short form → prefilled public GitHub issue, auto-labelled `bug`/`from-discord`; player reviews and submits |

Clock-in streaks award one-time taxed bonuses at 10, 20, and 60 business days. Weekends preserve streak continuity. A worker who is clocked in cannot be fired or lose their shift through an acquisition; a voluntary quitter can wait for settlement or explicitly clock out first.

## Production on desktop

The checked-in configuration targets `ssh desktop`, running as Jason's **systemd user service**. `deployer` is maintained separately in `~/Code/deployer` on the development Mac. The desktop has a provisioned copy of its release engine under `~/.local/share/tendies/tools/deployer` for local polling deployments. No self-SSH key or inbound deployment API is required.

- Bot service: `deployer-tendies.service`, `Restart=always`, three-second restart delay; user lingering starts it at boot.
- Releases: `/home/jason/.local/share/deployer/tendies/releases/` with a `current` symlink.
- Persistent SQLite: `/home/jason/.local/share/tendies/tendies.db`.
- Readiness: `/home/jason/.local/share/tendies/health.json`.
- Secrets: deployer's protected `.deployer/environment` outside releases.
- Backups: `/home/jason/.local/share/tendies/backups/`.

Create your local production environment file once:

```sh
cp .env.prod.example .env.prod
chmod 600 .env.prod
# Edit DISCORD_TOKEN. Do not put the development database URL in this file.
./scripts/pipeline.sh deploy-dry-run
./scripts/pipeline.sh deploy
./scripts/pipeline.sh status
```

The script tests a clean committed checkout, records the source revision, and invokes `deployer deploy --env .env.prod`. `TENDIES_SECRETS_FILE` can select another local file. This uses deployer's supported credential transfer: SSH to a `0600` environment file, excluded from uploads, available to the running service but not setup/build commands. Values in the file override `[env]`, so keep production paths in `deploy.toml` unless deliberately changing them. A failed release restores the preceding code, environment, and service configuration.

Remote setup uses `uv sync --frozen --no-dev`, then readiness must show a live process, a fresh Discord-ready heartbeat, the expected release, and a readable SQLite database. Local tests alone are not sufficient deployment evidence. Runtime uses one exclusive SQLite lease plus serialized transactions; do not start a second bot against its database.

## PRs, cloud agents, and automatic releases

1. Make a branch and edit code/docs. Run `./scripts/pipeline.sh check`.
2. Open a PR. GitHub Actions runs the same check on a clean Linux checkout using locked dependencies, with no production secrets.
3. Merge a passing PR into `main`.
4. Desktop's `tendies-poll.timer` checks approximately every five minutes (plus up to 45 seconds of jitter), fetches `main`, creates an isolated candidate checkout, runs the checks again, and deploys using the same deployer release engine. No changed revision means no deployment.
5. A failed check keeps the current release. A failed activation triggers deployer's restore behavior. Inspect logs if polling fails.

The poller reuses the protected production environment rather than checking credentials into Git. It does not poll feature branches or execute unmerged PRs. Anyone who can merge code to `main` can change production behavior; keep merge access limited. The repo's required CI check is `test`.

The initial host setup installs uv and Python 3.12, provisions the deployer package, and clones the public repository into `~/.local/share/tendies/source`. Once the first release is healthy, enable the timers on desktop:

```sh
ssh desktop '~/.local/share/deployer/tendies/current/scripts/pipeline.sh install-timers'
```

Poll/backup units live in `scripts/systemd/`; these are specific to this desktop's paths. Porting to another user/server requires updating `deploy.toml`, units, and the adapter's deployer path. The future authenticated deployer MCP service can reuse these commands; this release exposes no deployment port.

## Operations, backups, and rollback

```sh
./scripts/pipeline.sh logs --lines 80
./scripts/pipeline.sh restart
./scripts/pipeline.sh backup
./scripts/pipeline.sh backup-fetch
./scripts/pipeline.sh rollback
```

Backups use SQLite's online backup API, verify the snapshot with `PRAGMA quick_check`, use private permissions, and retain the newest 30. The daily timer and predeployment setup create backups; `backup-fetch` copies snapshots to this checkout's ignored `.backups/` directory for an additional machine copy. Regularly copy snapshots off desktop: same-disk backups cannot survive disk loss. Runtime data is never part of a release upload.

Before deliberately rolling back, pause polling so it does not immediately redeploy the current `main` revision:

```sh
ssh desktop 'systemctl --user stop tendies-poll.timer'
./scripts/pipeline.sh rollback
```

Rollback restores code/environment, **not player data**. Fix or revert the offending commit through a PR, then restart polling when ready. Restore data only deliberately, with the bot stopped:

```sh
ssh desktop
systemctl --user stop tendies-poll.timer
systemctl --user stop deployer-tendies.service
~/.local/share/deployer/tendies/current/scripts/pipeline.sh restore-local /absolute/path/to/backup.sqlite3 --confirm
systemctl --user start deployer-tendies.service
# Check service and heartbeat before restarting the poll timer.
systemctl --user start tendies-poll.timer
```

A restore first preserves the existing database, verifies the selected snapshot, and replaces the stopped database. Tests exercise restoration on disposable copies. Never copy only a live SQLite `.db` file while ignoring its WAL; use the backup command. SQLite files survive service crashes and reboots. Startup recovers missed calendar closes without inventing attendance or paying a date twice.

Useful diagnostics:

```sh
ssh desktop 'systemctl --user status tendies-poll.timer tendies-backup.timer'
ssh desktop 'journalctl --user-unit tendies-poll.service -n 80 --no-pager'
ssh desktop 'cd ~/.local/share/deployer/tendies/current && /usr/bin/python3 src/tendies/ops.py healthcheck'
```

## Source layout and contribution expectations

`services/` implements game rules without Discord. `money.py` owns cash/ledger operations; `tick.py` settlement; `scheduler.py` local calendar recovery; `valuation.py` live and closing quotes; `db.py` transaction serialization; `models.py` schema. `cogs/` implements commands, including `/bug` in `feedback.py`. `health.py` provides the process lease/heartbeat; `ops.py` provides backup/restore/readiness tooling. `scripts/pipeline.sh` is the operator/cloud entry point.

Add a regression for a reported bug, preserve guild isolation and accounting invariants, and update DESIGN/README when behavior changes. Schema changes need an explicit migration and restore plan: `create_all` does not alter existing columns. Do not check in `.env*`, databases, backups, tokens, or local machine state. Do not run production tokens in cloud tests. The test suite covers core rules and command behavior; Discord connection and real user interactions still need deployment verification.
