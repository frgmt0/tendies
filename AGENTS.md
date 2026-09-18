# Working on Tendies

Read README.md and DESIGN.md for the implemented game and deployment contract.

- Run `./scripts/pipeline.sh check`; it works locally and in cloud environments without Discord credentials, SSH, or deployer.
- Use a branch and PR. GitHub CI checks `test` before merge; desktop polls merged `main` and tests again before deploying. A cloud agent does not need direct desktop access.
- Do not use production credentials or databases in development/tests. `.env.prod` is ignored and transferred only with deployer's protected environment support.
- Money must move through money.py, stay integer/nonnegative, and preserve supply except explicit printing. Preserve guild isolation and cap-table invariants.
- A close settles the current date before advancing. Keep calendar/DST/restart tests when changing scheduling.
- A single bot owns production SQLite. Do not bypass Database.session transaction serialization for runtime mutations.
- Add regression coverage for behavioral fixes and update docs when rules change. Keep deliberately deferred features explicit.
- Schema column changes need explicit migrations and restore tests. Code rollback does not undo data changes.
- Do not expose a new deployment server or remote agent credentials. Current production communication is outbound Git polling.
