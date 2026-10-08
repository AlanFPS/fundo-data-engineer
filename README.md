# Fundo data engineer take-home

A small local version of "SQL Server → BigQuery", with the three problems worked in code:

1. **Move only what changed**: incremental load (SQL Server Change Tracking plus a rowversion watermark) into a DuckDB warehouse. It handles deletes, re-runs, replays and crashes mid-run.
2. **Resolve duplicate customers**: explicit identity rules, a reversible merge and a database constraint that stops new duplicates.
3. **Prove the data is correct**: a source-vs-warehouse check that reports gaps, missed deletes, changed values and duplicate keys, plus a demo that breaks the warehouse on purpose.

Read [SOLUTION.md](SOLUTION.md) first for the decisions and the numbers. This file covers how to run it.

## Run it

You need Docker with Compose v2 and `make`. SQL Server needs about 2 GB of RAM. The first run pulls the SQL Server image (about 1.6 GB) and builds a small Python image.

```bash
docker compose up -d --wait   # SQL Server 2022 (source). Returns once it accepts connections.
make demo                     # the whole story, end to end (about 1 minute after the first run)
```

That's all. `make demo` resets everything and prints nine steps. Each step ends with a line `-> OK: ...`; the run stops at the first `-> UNEXPECTED`. In order:

| Step | What happens | What to look for |
|---|---|---|
| 1 | Seed SQL Server: 5.3k customers, 11.5k advances, 5k cards, 140k transactions, 42k history rows, a scratch table | row counts |
| 2 | Initial load: every table is a full snapshot | `committed: 203,193 rows, 13.8 MB` |
| 3 | One simulated business day at the source (about 1% of rows: inserts, updates, deletes) | per-table `+ins ~upd -del` |
| 4 | Check *before* loading | `PASS`, with the unloaded changes shown under `lag`: behind is not wrong |
| 5 | Incremental load | the same `+ins ~upd -del` counts as step 3; `moved 1,307 rows / 94.6 KB instead of 203,193 rows / 13.8 MB` |
| 6 | Run again, then deliberately replay the last window | 0 rows; the warehouse fingerprint is identical before and after the replay |
| 7 | Another day, then the load is killed (`os._exit`) after applying 3 of 5 tables | the check still passes (nothing partial landed); the re-run recovers |
| 8 | Duplicate customers: plan, apply, then two customers try to sign up again | merge summary, score against the planted truth, every survivor keeps a default card, `rejected by UX_Customers_Ssn_Active` |
| 9 | Break the warehouse four ways, check, repair, check | `VERDICT: FAIL - 4 problem(s)`, then `VERDICT: PASS` |

At the end it prints measured sizes and bytes moved per load (`make stats`).

## Commands one at a time

```bash
make help        # list targets
make seed        # (re)create the source database; also resets the warehouse
make load        # incremental load (full snapshot the first time)
make check       # is the warehouse complete and correct right now? fails (non-zero exit) if not
make simulate    # one business day of changes at the source
make dedupe      # duplicate-customer plan, dry run: changes nothing, writes reports/*.csv
make dedupe-apply  # backup, then one transaction: merge, hold, exclude tests, add the unique index
make dedupe-undo   # reverse the last applied dedupe run from its change log
make break       # damage the warehouse on purpose
make repair      # full refresh of the tables the check fails on, then check again
make stats       # source sizes, change-tracking overhead, bytes moved per load
make test        # unit tests for the identity rules (no database)
make down        # stop and delete everything (database volume, warehouse, reports)
```

A typical session after `make seed && make load`:

```bash
make simulate && make check   # PASS, with the day's changes reported as lag
make load                     # moves only those changes; counts match what simulate printed
make load                     # 0 rows: running twice is harmless
```

## The failing-check demo

```bash
make load            # start from an up-to-date warehouse
make break
make check; echo "exit code: $?"   # make reports 2; the check itself exits 1
make repair
```

`make break` does four things:

1. Deletes 25 transactions at the source, then advances the loader's watermark without applying them. This is a loader bug that leaves the pipeline's own state saying "up to date".
2. Drops a block of 300 transactions from the warehouse.
3. Adds 100.00 to one advance's amount in the warehouse.
4. Loads one card twice.

`make check` then prints:

```
  table                    source  warehouse  missing unexpect  differ  dupkey  orphan   lag  result
  Customers                 5,321      5,321        0        0       0       0       -     0  PASS
  Advances                 11,625     11,625        0        0       1       0       0     0  FAIL
  Cards                     5,100      5,101        0        0       0       1       -     0  FAIL
  Transactions            141,449    141,174      300       25       0       0       0     0  FAIL
  AdvanceStatusHistory     42,077     42,077        0        0       0       0       -     0  PASS
  FAIL  Advances.different: content differs: keys 2; e.g. AdvanceId 2: Amount '400.00' in source vs '500.00' in warehouse
  FAIL  Cards.dup_keys: 1 extra copies of keys [5115]
  FAIL  Transactions.missing: in source, not in warehouse: keys 70000-70299
  FAIL  Transactions.unexpected: in warehouse, no longer in source: keys 141516, 141522, ...
VERDICT: FAIL - 4 problem(s) in Advances, Cards, Transactions. Repair with `make repair` ...
exit code: 2
```

`make repair` full-refreshes the three failing tables. Its load line shows what it fixed (`+300`, `-25`, `~1`, `-1`) and the check that follows passes. The exact keys and counts depend on how many simulated days have run.

## Looking inside

- **Warehouse**: `data/warehouse.duckdb`.
  - Schema `raw`: the replicated tables.
  - Schema `meta`: `sync_state` (watermarks), `load_log` (every load, with rows and bytes) and `check_results` (every check result, for a dashboard or an alert).
  - If you have the DuckDB CLI: `duckdb data/warehouse.duckdb`.
- **Source**: `127.0.0.1,14330` (published on loopback only), user `sa`, password `Fundo_local_2026!`, database `fundo`.
  - The dedupe adds schema `ops`: `DedupeRun`, `DedupeDecision` and `DedupeChange` (the undo log).
- **Reports** (written by `make dedupe`):
  - `reports/dedupe_decisions.csv`: every customer acted on, and why.
  - `reports/review_pairs.csv`: possible duplicates and possible test accounts for a human to decide.
  - `reports/contact_issues.csv`: malformed emails and phones, by customer.

## Layout

```
docker-compose.yml   SQL Server + the Python app image
Makefile             the commands above
sql/                 source schema, change-tracking setup, dedupe migration
fundo/
  seed.py            deterministic synthetic data; planted cases go to data/seed_truth.json
  simulate.py        one business day of source changes
  tables.py          what is replicated and with which strategy
  sync.py            the incremental loader
  checks.py          source-vs-warehouse reconciliation
  identity.py        what proves identity, what only suggests it, test accounts, contact validation
  dedupe.py          plan / apply / undo for duplicate customers
  breakit.py         the deliberate breakage
  demo.py, stats.py  the end-to-end story and the measurements
tests/               unit tests for the identity rules
```

## Troubleshooting

- **SQL Server exits with `Unable to create a new asynchronous I/O context`.** Your Docker emulates amd64 without the `io_setup` system call, as OrbStack does with Rosetta switched off. On Apple Silicon, turn Rosetta on (Docker Desktop: Settings → General → "Use Rosetta"; OrbStack: `orb config set rosetta true`). Or use the arm64 build of the same engine, Azure SQL Edge:
  ```bash
  cp .env.arm64 .env && make down && docker compose up -d --wait && make demo
  ```
- **Port 14330 is taken**: `MSSQL_PORT=14331 make demo`.
- **Files in `data/` owned by root** (Linux, when running `docker compose run` directly): the Makefile runs the app container as your user; use the make targets, or pass `HOST_UID=$(id -u) HOST_GID=$(id -g)`.
- **`refusing to drop and re-create database`**: `seed`, `demo` and `break` only run inside the compose stack (`FUNDO_ALLOW_RESET=1`), so a mistyped `MSSQL_HOST` can't drop a real database.
- **Start over**: `make down`.

## Where this was verified

Everything above was run end to end with `make test && make demo && make check` on macOS on Apple Silicon:

- **SQL Server 2022** (16.0.4295.3, under Rosetta): the compose default. After the audit fixes: 34 tests passed, all 9 demo steps OK in 61 s, final check PASS.
- **Azure SQL Edge** (arm64): the `.env.arm64` fallback. Verified end to end from a clean copy before the audit fixes; not re-run since.

`.github/workflows/demo.yml` runs the same three commands on x86 Linux on every push.
