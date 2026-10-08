# Code Review: Fundo data-engineer take-home (incremental loader, dedupe, reconciliation checks)

**Reviewed Files**: `fundo/__main__.py`, `fundo/db.py`, `fundo/tables.py`, `fundo/sync.py`, `fundo/checks.py`, `fundo/identity.py`, `fundo/dedupe.py`, `fundo/seed.py`, `fundo/simulate.py`, `fundo/breakit.py`, `fundo/demo.py`, `fundo/stats.py`, `sql/source_schema.sql`, `sql/enable_change_tracking.sql`, `sql/dedupe_migration.sql`, `docker-compose.yml`, `Dockerfile`, `Makefile`, `requirements.txt`, `.github/workflows/demo.yml`, `tests/test_identity.py`, `README.md`, `SOLUTION.md`
**Review Date**: 2026-10-07
**Severity Summary**: 0 Critical, 6 High, 11 Minor, 7 Nit

The environment was SQL Server 2022 (16.0.4295.3) under Rosetta, with the state left after `make demo`. Each finding says whether it was **verified** (reproduced or measured) or is **from code reading** (not reproduced).

## Critical Issues

None found. Every path that writes data runs inside a transaction:
- **Loader**: one source snapshot, then one warehouse transaction.
- **Dedupe apply**: one transaction, after a backup.
- **Undo**: one transaction, with conflict detection.

The crash, replay and repair paths were exercised end to end by `make demo` on SQL Server 2022.

## High Priority Issues

### H1. SQL Server is published on every interface with a known `sa` password

- **Location**: `docker-compose.yml:15`, `README.md` ("Looking inside")
- **Severity**: HIGH (verified)
- **Problem**: The port mapping `"${MSSQL_PORT:-14330}:1433"` binds to `0.0.0.0` and `[::]`, and the `sa` password is printed in the README.
  - On this machine OrbStack has `docker.expose_ports_to_lan: true`, so anyone on the same network can log in as `sa`.
  - Reviewers on Docker Desktop get the same all-interfaces binding.
  - The data is synthetic, but `sa` can run `xp_cmdshell` once someone enables it, which means command execution inside the container.
- **Example**: `docker port fundo-de-mssql-1` prints `1433/tcp -> 0.0.0.0:14330` and `[::]:14330`.
- **Fix**: Bind to loopback only: `- "127.0.0.1:${MSSQL_PORT:-14330}:1433"`. The app container reaches the database over the compose network and doesn't need the published port.
- **Verification**: `docker port fundo-de-mssql-1` shows only `127.0.0.1:14330`, and `make demo` still passes.
- **Prevention**: Default to loopback bindings for every published port in dev compose files, and add a lint rule (e.g. a grep in CI) that rejects `- "NNNN:` mappings without a host IP.

### H2. A merge demotes the loser's default card even when the survivor has no default card

- **Location**: `fundo/dedupe.py:325-332`
- **Severity**: HIGH (verified: 5 survivors affected after `make demo`)
- **Problem**: Every moved card gets `IsDefault=False`, whatever the survivor has. When the survivor had no active default card, it ends up with active cards but no default. Repayment and autopay flows that charge the default card then have nothing to charge.
  - This is exactly the "say what breaks if you get cards wrong" case, and `SOLUTION.md` claims the survivor's default "stays", which assumes there is one.
  - Measured: 5 cards that were active defaults before the merge (customers 152, 265, 674, 1422, 5257) are now non-default on survivors with no default card. Found via `ops.DedupeChange` joined to `dbo.Cards`.
- **Example**:
  ```python
  change(cur, log, "Cards", "CardId", card_id, CustomerId=str(target), IsDefault=False,
         Status="removed" if dup_card else status)
  ```
- **Fix**: Per merge, check whether the survivor has an active default card. If not, keep the loser's active default (or its newest active card) as default; demote only when the survivor already has one.
  ```python
  has_default = db.scalar(cur, "SELECT COUNT(*) FROM dbo.Cards WHERE TRY_CAST(CustomerId AS INT) = %s "
                               "AND Status = 'active' AND IsDefault = 1", (target,)) > 0
  keep_default = (not has_default and is_default and status == "active" and not dup_card)
  change(..., IsDefault=keep_default, ...)
  ```
  Also add a data-quality check: "active customers with active cards but no default card". This should be its own finding, separate from the 68 already in the seed data.
- **Verification**: After `make demo`, the query above returns 0 for survivors; add a unit test with a survivor that has no cards and a loser that has one default card.
- **Prevention**: Treat "every customer with an active card has exactly one default" as an invariant: assert it in the checks after every dedupe apply.

### H3. Plan and apply are not atomic, so the "funded is untouchable" rule can be violated under concurrent writes

- **Location**: `fundo/dedupe.py:52-71` (funded flags computed at plan time), `fundo/dedupe.py:306-347` (apply), `fundo/dedupe.py:284-293` (`change()`)
- **Severity**: HIGH (from code reading; not reproduced, because the demo source is quiet during apply)
- **Problem**: `plan()` decides survivors and losers from a read taken *before* apply starts. `apply()` then moves every advance of each loser without re-checking anything.
  - If a loser's advance becomes `funded` between plan and apply, a funded customer is merged away and loses records. That breaks the first business rule.
  - `change()` reads the old value and then updates it, at READ COMMITTED and without locks. A concurrent edit in between makes the undo log record the wrong "old" value, so `--undo` would restore stale data.
  - New cards or advances inserted for a loser after apply's SELECT and before COMMIT stay on the merged customer. The data-quality check flags this only after the fact.
- **Example**:
  ```python
  cur.execute("SELECT AdvanceId FROM dbo.Advances WHERE CustomerId = %s", (cid,))  # no lock, no re-check
  for (aid,) in cur.fetchall():
      change(cur, log, "Advances", "AdvanceId", aid, CustomerId=target)
  ```
- **Fix**: Inside the apply transaction, for each merge:
  1. Lock the loser and survivor rows: `SELECT ... FROM dbo.Customers WITH (UPDLOCK, HOLDLOCK) WHERE CustomerId IN (%s, %s)`.
  2. Re-check that both are still `DedupeStatus IS NULL` and the loser still has no funded/paid-off advance. Skip the merge (and report it) if anything changed.
  3. Read children `WITH (UPDLOCK, HOLDLOCK)` so no new child can slip in before commit.
  4. Capture old values atomically, e.g. `UPDATE ... OUTPUT deleted.col, inserted.col`, instead of select-then-update.
- **Verification**: A test that funds a loser's advance between `plan()` and `apply()`. Apply must skip that merge, and the loser keeps its advance.
- **Prevention**: Any plan/apply tool must re-validate its preconditions under lock inside the write transaction; document this as a rule in the module docstring.

### H4. A reserved-domain email alone excludes a KYC-verified, paying customer as "test"

- **Location**: `fundo/identity.py:85-111` (`classify_test`, reserved-domain rule at line 97)
- **Severity**: HIGH (verified by code: `classify_test("Maria","Lopez","maria@example.com","212345678")` returns `"test"`)
- **Problem**: The brief warns that "the naive pattern catches real people". Real customers do type throwaway addresses like `x@example.com` to avoid spam.
  - Two rules (a reserved domain, or a QA mailbox on our own domain) exclude regardless of counter-evidence: a valid non-sandbox SSN that passed KYC, or real money moved.
  - Exclusion removes the customer from analytics and from the SSN uniqueness constraint (`DedupeStatus='test'`), so their duplicates are no longer prevented.
  - None of the planted test accounts has a valid SSN, so tightening the rule changes no seeded outcome.
- **Example**:
  ```python
  if domain and RESERVED_DOMAIN.search(domain):
      return "test", f"reserved domain, cannot receive mail ({domain})"
  ```
- **Fix**: Downgrade to `"review"` when the record carries a structurally valid SSN that is not a known QA SSN (`valid_ssn(ssn) and ssn not in QA_SSNS`):
  ```python
  real_identity = valid_ssn(ssn)
  if domain and RESERVED_DOMAIN.search(domain):
      return ("review" if real_identity else "test"), "reserved domain, cannot receive mail (...)"
  ```
  Optionally also pass `funded` and treat "funded + valid SSN" as a review signal.
- **Verification**: Add a parametrised test `("Maria","Lopez","maria@example.com","212345678","review")`, and re-run `make demo`; the score lines stay 14/14 and 3/3.
- **Prevention**: Every auto-exclusion rule should list the counter-evidence that overrides it. Keep a "real people who look like tests" fixture in the tests.

### H5. Destructive commands have no guard against a non-local target

- **Location**: `fundo/seed.py:393-398` (`create_database`: `SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE fundo`), `fundo/demo.py:24-27`, `fundo/breakit.py:17-20` (deletes source rows)
- **Severity**: HIGH (from code reading)
- **Problem**:
  - The target server is whatever `MSSQL_HOST`/`MSSQL_PORT` say. `make seed` and `make demo` drop any database named `fundo` on that server, killing open sessions, with no confirmation. `make break` deletes 25 rows at the source.
  - Inside compose the host is the local container. But the code also runs outside Docker (it defaults to `localhost`), and the brief scores "Safety: nothing destructive without a way back".
- **Example**:
  ```python
  cur.execute(f"IF DB_ID('{db.SOURCE_DB}') IS NOT NULL BEGIN "
              f"ALTER DATABASE {db.SOURCE_DB} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
              f"DROP DATABASE {db.SOURCE_DB}; END")
  ```
- **Fix**: Refuse unless an explicit opt-in is present, e.g. `FUNDO_ALLOW_RESET=1`, set only in the compose `app` service. Also check that the existing database carries a seed marker before dropping it, e.g. an extended property `fundo_seed=1` written by `seed.run()`. Apply the same guard to `breakit.run()`.
- **Verification**: `MSSQL_HOST=somewhere python -m fundo seed` without the flag exits with a clear error; `make demo` is unaffected.
- **Prevention**: Destructive commands should prove they're pointed at disposable infrastructure before acting.

### H6. On Linux, `make seed` and `make down` hit permission errors on files the container created as root

- **Location**: `Dockerfile` (no `USER`), `docker-compose.yml:37-38` (bind mount `./:/app`), `Makefile` targets `seed` (`rm -f data/warehouse.duckdb`) and `down` (`rm -rf data reports`)
- **Severity**: HIGH (from code reading; not reproduced, because macOS Docker Desktop/OrbStack map file ownership)
- **Problem**:
  - The app container runs as root, so on a Linux host `data/` and `reports/` and their files are created root-owned.
  - The Makefile then deletes them *on the host* as the invoking user. `make seed` (second run) and `make down` fail with "Permission denied".
  - The brief says it must run on the reviewers' machines. CI is unaffected because it never runs these targets.
- **Example**:
  ```make
  seed: up
  	rm -f data/warehouse.duckdb
  ```
- **Fix**: Either run the container as the host user (`user: "${UID:-1000}:${GID:-1000}"` in the `app` service; `HOME=/tmp` may be needed), or do the deletions inside the container. The second option is simplest: move the warehouse reset into `seed.run()`, and change `down` to run `docker compose run --rm --no-deps --entrypoint rm app -rf data reports` before `docker compose down -v`.
- **Verification**: On a Linux host (or the GitHub runner): `make demo && make seed && make down` succeeds.
- **Prevention**: Add `make seed && make down` to the CI workflow so ownership regressions fail CI.

## Minor Issues

### M1. README says a failing `make check` exits 1; `make` exits 2

- **Location**: `README.md` ("The failing-check demo" shows `exit code: 1`)
- **Severity**: MINOR (verified: `make check` printed exit code 2; `docker compose run --rm app check` printed 1)
- **Problem**: A reviewer following the README sees a different number than documented. The underlying CLI correctly returns 1; `make` turns any recipe failure into 2.
- **Fix**: Say "non-zero (the check exits 1; make reports 2)", or show `docker compose run --rm app check; echo $?`.
- **Verification**: Run the documented commands and compare.
- **Prevention**: Generate README output snippets from an actual run (e.g. a `make readme-snippets` target).

### M2. The dedupe backup is taken after the migration, and the backups are deleted by `make down`

- **Location**: `fundo/dedupe.py:309-313` (migration at line 309, backup at line 311), `Makefile` (`down: docker compose down -v`)
- **Severity**: MINOR (verified by reading the order of operations)
- **Problem**:
  - The migration adds columns, a schema and tables *before* `BACKUP DATABASE` runs, so the "before dedupe" backup is not a pre-change image. The SOLUTION says "takes a backup, then...".
  - The `.bak` files live in `/var/opt/mssql/data` inside the named volume, which `make down` deletes. The way back disappears with a routine cleanup.
- **Fix**: Take the backup first, then run the migration. Write backups to a bind-mounted `./backups/` directory (gitignored) so they outlive the volume.
- **Verification**: Restore the backup into a scratch database; `COL_LENGTH('dbo.Customers','DedupeStatus')` is NULL.
- **Prevention**: Treat "backup" as the first statement of any `--apply` path; assert it in a test.

### M3. `DuplicateOfCustomerId` has no foreign key

- **Location**: `sql/dedupe_migration.sql:3-6`, `fundo/simulate.py:66-70` (purges customers without children)
- **Severity**: MINOR (from code reading; 0 dangling links observed after the demo)
- **Problem**:
  - Nothing stops the survivor of a merge from being deleted. The simulator's "purge abandoned signups" job only excludes non-active rows, and a survivor with no advances or cards qualifies.
  - The merged rows would then point at nothing, the undo log would reference a missing customer, and person-level analytics would break.
- **Fix**: `ALTER TABLE dbo.Customers ADD CONSTRAINT FK_Customers_DuplicateOf FOREIGN KEY (DuplicateOfCustomerId) REFERENCES dbo.Customers (CustomerId);`. Also exclude referenced customers from purge jobs.
- **Verification**: Attempt to delete a survivor; the FK error is raised.
- **Prevention**: Every new reference column gets a foreign key unless there is a written reason not to.

### M4. Freshness mixes local-time and UTC timestamps

- **Location**: `fundo/sync.py:215,217` (`now()` into `TIMESTAMP`), `fundo/checks.py:214`
- **Severity**: MINOR (verified: with `TZ=America/Buenos_Aires`, DuckDB stored `19:48` while UTC was `22:48`)
- **Problem**:
  - DuckDB `now()` is a `TIMESTAMPTZ`; inserted into a `TIMESTAMP` column it becomes *local* time. The check subtracts it from Python's UTC.
  - Inside the container TZ is UTC, so it's correct today.
  - Run outside Docker in Buenos Aires, every table appears 3 hours stale; in UTC+ zones the age goes negative and staleness is never detected.
- **Fix**: Store `now() AT TIME ZONE 'UTC'` (or `TIMESTAMPTZ` columns) and compare in UTC; or pass the timestamp from Python (`datetime.now(timezone.utc)`).
- **Verification**: Run `check` with `TZ=America/Buenos_Aires` right after a load; freshness is about 0 h.
- **Prevention**: Use timezone-aware types for every audit timestamp.

### M5. Columns are replicated by denylist, so new source columns (possibly PII) flow automatically

- **Location**: `fundo/tables.py:11,15,19` (`exclude=`), `fundo/sync.py:64-79`
- **Severity**: MINOR (design, from code reading)
- **Problem**: Only listed columns are excluded. A column added later, such as `BankAccountNumber` or `SsnFull`, is copied to the warehouse on the next load; the schema-hash change triggers a silent full refresh. That contradicts the intent stated for `Ssn` ("PII the warehouse doesn't need").
- **Fix**: Make the column list an allowlist per table. On a schema change, fail with "new column X in Customers: add it to the allowlist or the denylist", instead of auto-refreshing.
- **Verification**: Add a column at the source; the next load stops with that message.
- **Prevention**: Use the same allowlist approach already used for tables.

### M6. Undo has no rollback on unexpected errors and cannot reverse a run that re-activates SSN duplicates

- **Location**: `fundo/dedupe.py:356-388`
- **Severity**: MINOR (from code reading)
- **Problem**:
  - Unlike `apply()`, `undo()` has no `try/except ... ROLLBACK`. A DB error leaves the transaction open until the process exits (SQL Server then rolls back, but the error output is unclear).
  - Undoing a *later* run whose restores would make two active customers share an SSN fails on `UX_Customers_Ssn_Active`, because only the run that created the index drops it.
- **Fix**: Wrap the transaction like `apply()`. When restoring `DedupeStatus` to NULL for a customer whose SSN is held by another active customer, set it to `'hold'` instead (and log that), or drop and recreate the index around the undo.
- **Verification**: Apply twice, undo the second run; it succeeds or explains the conflict.
- **Prevention**: Integration test for apply, apply, undo, undo.

### M7. Wrong hold reason for non-funded members of a double-funded group

- **Location**: `fundo/dedupe.py:135-149`
- **Severity**: MINOR (from code reading)
- **Problem**: `funded_ids` contains *every* member of a double-funded component, including non-funded ones. A non-funded third account in such a group is held with "both accounts have funded/paid-off advances", which misinforms the ops reviewer.
- **Fix**: `funded_ids = {c.id for comp in double_funded for c in comp if c.funded}`, and give non-funded members their own reason ("proven same person as two funded accounts; merge after they are resolved").
- **Verification**: Unit test with a three-member component (two funded, one not).
- **Prevention**: Assert reason text in plan tests.

### M8. The check holds a long snapshot transaction and an exclusive warehouse lock, and drill-down is unbounded

- **Location**: `fundo/checks.py:193-232`, `fundo/checks.py:118-125`
- **Severity**: MINOR (from code reading)
- **Problem**:
  - A scheduled check that overlaps a load fails on DuckDB's single-writer file lock, because the check opens the warehouse read-write to append `check_results`.
  - When most buckets disagree (e.g. a whole table lost), the drill-down fetches every row from both sides: the "full copy" the design avoids.
  - On a busy production source the snapshot transaction lives for the whole check and grows the version store.
- **Fix**: Run load then check sequentially in one scheduled job (or take a file lock). Cap drill-down, for example at 50 buckets: report "N buckets differ" and recommend a full refresh beyond that, or bisect.
- **Verification**: Break a whole table and confirm the check stays bounded.
- **Prevention**: Document scheduling constraints next to the CLI.

### M9. Customer PII is printed to stdout

- **Location**: `fundo/dedupe.py` (`summarize`: names and emails of test-looking and lookalike customers)
- **Severity**: MINOR (synthetic data here; matters in production)
- **Problem**: A scheduled job's stdout goes to log aggregation, so names and emails end up in logs with weaker access control than the database.
- **Fix**: Print counts and customer IDs only; keep names in the access-controlled reports.
- **Verification**: `make dedupe | grep @` returns nothing.
- **Prevention**: A logging rule: no direct identifiers in job output.

### M10. `make test` covers only identity rules and planning

- **Location**: `tests/test_identity.py`, `Makefile:test`
- **Severity**: MINOR
- **Problem**: The loader, checks, apply and undo are exercised only by `make demo`'s `expect()` calls. They need a database and are not part of `make test`. Regressions in, for example, `apply()` card handling (H2) are not caught by the fast tests.
- **Fix**: Add a `make integration` target that runs `demo` (it already asserts) plus targeted scenarios (H2, H3, M6). Run it in CI. The CI already runs `make demo`; also assert invariants such as "no survivor without a default card".
- **Verification**: CI fails when H2 is reintroduced.
- **Prevention**: Each bug fix lands with a scenario in the integration target.

### M11. Rolling out the unique index first breaks signups

- **Location**: `fundo/dedupe.py:341-343`, `SOLUTION.md` ("The app still has to turn that error into...")
- **Severity**: MINOR (documented, but the order matters)
- **Problem**: In production, creating `UX_Customers_Ssn_Active` before the application handles error 2601 turns every returning-customer signup into a server error.
- **Fix**: State the rollout order explicitly (app handling first, then the index), or create the index in a separate, later step (`--add-constraint`).
- **Verification**: n/a (process).
- **Prevention**: Treat constraints that reject writes as coordinated releases.

## Nits

### N1. `run_script` splits on every `;` and strips `--` even inside string literals

- **Location**: `fundo/db.py:69-76`
- **Severity**: NIT. Fine for the repo's own SQL files, which contain neither. Note it in the docstring, or split on a `GO`-style separator.

### N2. The canonical row text is ambiguous for `|` and `\N`

- **Location**: `fundo/checks.py:44-61`, `fundo/checks.py:126-130`
- **Severity**: NIT. A value containing `|` misaligns the column-level example in the report. A literal string `\N` hashes the same as NULL, so a change between them is invisible. To fix, escape `\` and `|` in text columns on both sides (`REPLACE`) before concatenating.

### N3. Bucket numbering differs for negative keys

- **Location**: `fundo/checks.py:109-122`
- **Severity**: NIT. SQL Server `k / b` truncates toward zero while DuckDB `k // b` floors, so negative keys land in different buckets. Keys here are positive identities. Use `FLOOR(k * 1.0 / b)` on both sides, or assert `k >= 0`.

### N4. The test helper builds an invalid date for ids above 31

- **Location**: `tests/test_identity.py` (`customer()`: `datetime(2025, 1, cid)`)
- **Severity**: NIT. Use `datetime(2025, 1, 1) + timedelta(days=cid)`.

### N5. SOLUTION says "after four days"; the demo simulates three

- **Location**: `SOLUTION.md` §1 ("about 1% of the Transactions table after four days")
- **Severity**: NIT. The demo has three `simulate` days, plus the dedupe and break steps.

### N6. `dedupe --apply --undo` silently undoes

- **Location**: `fundo/__main__.py:31`
- **Severity**: NIT. Use an argparse mutually exclusive group.

### N7. The check compares timestamps at millisecond precision

- **Location**: `fundo/checks.py:44-61`
- **Severity**: NIT. The schema uses `DATETIME2(3)`, so this is exact today. A future `DATETIME2(7)` column could drift below 1 ms unnoticed; compare in microseconds (`DATEDIFF_BIG(MICROSECOND, ...)` against `epoch_us`).

## Summary

The core mechanics are sound and were verified end to end on SQL Server 2022:
- one snapshot read, one warehouse transaction and keyed MERGEs;
- Change Tracking validity checks;
- the `MIN_ACTIVE_ROWVERSION()` bound;
- in-flight exclusion in the check;
- a transactional dedupe with a change log.

The problems are at the edges.

Fix first:
1. **H2 (default card)**: a verified defect in exactly the area the brief asks about ("what breaks with cards"). Five survivors are affected.
2. **H1 (exposed `sa`)**: a one-line fix with a real exposure on this machine.
3. **H4 (test exclusion overrides KYC evidence)**: also in the brief's focus area.
4. **H6 and H5**: they decide whether reviewers can run the repo safely and repeatedly on Linux.
5. **H3**: the right fix is re-validation under lock inside `apply()`. It matters for production more than for the demo, but the SOLUTION's "untouchable" claim depends on it.

The minor items are mostly documentation accuracy (M1, M2) and production-hardening notes (M4 to M11).

---

# Second-Pass Audit (code-audit-second)

**Review Date**: 2026-10-07
**Method**: Before opening the first review, I read all the code, SQL, build files, the brief, README.md and SOLUTION.md, and wrote down my own hypotheses. Then I gave a verdict on every first-review finding and looked for what it missed.
**Environment**: the same SQL Server 2022 container and warehouse that `make demo` left behind. Each claim is marked **verified** (reproduced or measured here) or **from code reading**.

**What I ran.** Nothing in the repo changed except this file.
- `make test`: 32 passed.
- `make check`: PASS, before and after my work.
- Read-only queries, rolled-back transactions and one session temp table against the source.
- Destructive reproductions only on throwaway copies of the warehouse (`-e WAREHOUSE_PATH=/app/data/<copy>`), deleted afterwards.
- Linux file-ownership emulation in a throwaway `alpine` container. The image was removed afterwards.

**Severity summary for this pass**:
- New findings: 0 Critical, 0 High, 7 Minor (S1–S7), 4 Nit (S8–S11).
- Reclassified: H3, H4 and H5 from High to Minor; M1, M2 and M6 from Minor to Nit.
- Disputed: N3. Partially correct: M4.

## Verdicts on the first review's findings

### H1. SQL Server is published on every interface with a known `sa` password
- **First Review Severity**: HIGH
- **My Verdict**: CONFIRMED
- **Reasoning**: Verified. `docker port fundo-de-mssql-1` prints `0.0.0.0:14330` and `[::]:14330`, and OrbStack reports `docker.expose_ports_to_lan: true`.
  - There is one more reason for HIGH: on Linux, Docker's published ports bypass `ufw`. A reviewer running this on a cloud VM exposes `sa`, with the password printed in the README, to the internet.
  - The app never uses the published port; it reaches `mssql` over the compose network. Binding to loopback only costs nothing.
- **Improved Fix**: as proposed (`"127.0.0.1:${MSSQL_PORT:-14330}:1433"`).

### H2. A merge demotes the loser's default card even when the survivor has no default card
- **First Review Severity**: HIGH
- **My Verdict**: CONFIRMED (evidence corrected)
- **Reasoning**: Re-measured from `ops.DedupeChange` for run `dedupe-20261007T164109`. 49 cards went from `IsDefault=1` to `0`. Five are still active and belong to survivors with no *active* default card: the same customers 152, 265, 674, 1422 and 5257. For 4 of the 5, the cause differs from the write-up:
  - **Survivor 5257** had no cards at all.
  - **Survivors 152, 265, 674 and 1422** do have a default card, but it is **expired**. Their only active card is the one the merge just demoted.

  The expired-card pattern is the realistic one: a customer whose card expired signs up again with a new card. So HIGH stands. Two factual slips in the first review:
  - SOLUTION.md never claims the survivor's default "stays". It says cards "move to the survivor as non-default" (SOLUTION.md:63).
  - The 68 "active customers with active cards but no active default" already *include* these 5 survivors (63 pre-existing plus 5 from the merge). They are not a separate population.
- **Improved Fix**:
  - Test for an *active* default, as the proposed `has_default` query does.
  - When the survivor's only default is expired, also demote that card, logged through `change()` so `--undo` restores it. The survivor then ends up with exactly one default.
  - Re-evaluate `has_default` after each card moved within the same merge.

### H3. Plan and apply are not atomic
- **First Review Severity**: HIGH
- **My Verdict**: CONFIRMED, RECLASSIFIED to MINOR
- **Reasoning**: The mechanism is right (from code reading). `dedupe.run()` reads and plans on one connection. `apply()` then runs the migration and a full `BACKUP DATABASE` before its transaction starts, and nothing re-checks the plan's premises. So the race window is mostly the backup's duration.

  It is still an edge case:
  - It needs a concurrent funding event inside a one-off window that an operator starts.
  - Most other races abort the whole transaction safely:
    - a new SSN duplicate fails `CREATE UNIQUE INDEX`;
    - a deleted customer makes `change()` fail on `fetchone()` returning `None`;
    - a deleted survivor violates `FK_Advances_Customers`.
  - Every change is logged and reversible with `--undo`.

  The likelier form of the same bug is missing from the first review. `make dedupe` writes the reports a human reviews, but `make dedupe-apply` **re-plans from scratch** and applies the new plan without showing a diff. What was approved is not what gets applied.
- **Improved Fix**: one mechanism covers both problems.
  1. Persist the dry-run plan (for example `ops.DedupeDecision` rows in a `planned` state, keyed by run id) and make `--apply <run_id>` require it.
  2. Inside the apply transaction, *after* the backup, lock the three tables for the one-off run (`TABLOCKX, HOLDLOCK`, or `sp_getapplock` in a maintenance window). Re-run `load()` and `plan()`, and abort with a diff if the result differs from the approved plan.

  The proposed per-row `UPDLOCK, HOLDLOCK` cannot stay narrow on Cards. The owner predicate `TRY_CAST(CustomerId AS INT) = @id` is on an `NVARCHAR(MAX)` column and cannot use an index, so it range-locks the whole table anyway. "Maintenance window" is the honest description.

### H4. A reserved-domain email alone excludes a KYC-verified customer as "test"
- **First Review Severity**: HIGH
- **My Verdict**: CONFIRMED, RECLASSIFIED to MINOR
- **Reasoning**: The code path is real: the domain test returns before the SSN is considered (identity.py:97). But it bites only in an edge case:
  - No customer in the data fits it. Every reserved-domain account is a planted QA account, and the one with an SSN has the sandbox SSN.
  - The action is a status flag. Nothing is moved or deleted, and it can be undone.
  - The trap the brief names (substrings, and staff on the company domain) is handled correctly.
- **Improved Fix**: As proposed, with two adjustments:
  - `valid_ssn()` already rejects `123456789` (it is in `PLACEHOLDER_SSNS`), so `valid_ssn(ssn)` alone is the right condition.
  - `dedupe.load()` already computes `funded`. Passing it into `classify_test` lets "has a funded advance" also downgrade the verdict to review.

### H5. Destructive commands have no guard against a non-local target
- **First Review Severity**: HIGH
- **My Verdict**: CONFIRMED, RECLASSIFIED to MINOR
- **Reasoning**: Every documented entry point goes through compose. Compose hard-codes `MSSQL_HOST: mssql` (docker-compose.yml:34, no `${}` interpolation) and passes no `MSSQL_PORT` to the app. So `make seed`, `make demo` and `make break` can only reach the compose container.
  - Pointing the code anywhere else needs a deliberate run outside Docker, with `MSSQL_SA_PASSWORD` exported (it has no default; db.py:32 raises `KeyError`), against a server that has a database named `fundo`.
  - Resetting synthetic data is what these commands are documented to do.
  - The brief's "nothing destructive without a way back" applies to the data operations, and those have one (backup and undo).

  A cheap guard is worth adding, but this is not HIGH.
- **Improved Fix**: Skip the environment flag and keep only the marker. `seed.run()` writes an extended property `fundo_seed=1`. `create_database()` and `breakit.run()` refuse to touch a `fundo` database without it. That stops the realistic accident (the wrong server) and leaves the documented commands unchanged.

### H6. On Linux, `make seed` and `make down` hit permission errors on files the container created as root
- **First Review Severity**: HIGH
- **My Verdict**: CONFIRMED (mechanism verified by emulation)
- **Reasoning**:
  - **Who creates the directories.** `data/` and `reports/` are gitignored, so on a fresh clone the app container creates them, running as root (`TRUTH_PATH.parent.mkdir`, `db.warehouse()`, `REPORTS.mkdir`). On rootful Docker Engine they end up `root:root 755`.
  - **Emulation.** A uid-1000 user owned the repo, and root created `data/` and `reports/`.
    - The `seed` recipe's `rm -f data/warehouse.duckdb` failed with "Permission denied" (exit 1). `-f` does not suppress permission errors.
    - The `down` recipe's `rm -rf data reports` failed on every file (exit 1).
  - **Not affected**: macOS (Docker Desktop, OrbStack), Docker Desktop for Linux, and rootless Docker or Podman. These map file ownership to the user.
  - **Affected**: rootful Docker Engine, which is most Linux installs and probably WSL2 with the repo on the Linux filesystem.
  - **CI stays green**: `make demo` works because it deletes inside the container.

  The failure is certain on a common reviewer platform and breaks the README's own "Commands one at a time" section, so HIGH stands.
- **Improved Fix**: Prefer the "delete inside the container" option:
  - `seed.run()` unlinks the warehouse itself, as `demo.run()` already does, and the Makefile drops its host-side `rm`.
  - `down:` becomes `$(COMPOSE) run --rm --no-deps --entrypoint rm app -rf /app/data /app/reports`, then `$(COMPOSE) down -v`.

  Avoid `user: "${UID:-1000}:${GID:-1000}"`. `UID` is a shell variable that is not exported, so compose silently falls back to 1000, which is right only by coincidence.

### M1. README says a failing `make check` exits 1; `make` exits 2
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED, RECLASSIFIED to NIT
- **Reasoning**: GNU make exits 2 on any recipe failure. Both codes are non-zero and nothing depends on the exact value; only the README wording is off.

### M2. Backup taken after the migration; backups deleted by `make down`
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED, RECLASSIFIED to NIT
- **Reasoning**: The order is as described, but the impact is small:
  - The migration only adds two nullable columns and an empty `ops` schema. Restoring the backup still restores every pre-merge *value*, and `--undo` leaves the same schema behind anyway.
  - `make down` is documented as "delete everything" for a disposable local database.

  Swapping the two lines is free. Where backups live is a production concern.

### M3. `DuplicateOfCustomerId` has no foreign key
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED
- **Reasoning**: Measured: 3 survivors or hold targets are currently active with no advances and no cards, so the simulator's purge can pick them. There are 0 dangling links so far.

  Once the FK is added, simulate's purge `DELETE` raises instead of skipping. The purge-filter change in the proposed fix is therefore required, not optional.

### M4. Freshness mixes local-time and UTC timestamps
- **First Review Severity**: MINOR
- **My Verdict**: PARTIALLY CORRECT (MINOR)
- **Reasoning**: The mechanism is right. "In UTC+ zones … staleness is never detected" is wrong: the age is off by the UTC offset.
  - East of UTC, staleness is detected late, after at most 40 h instead of 26 h.
  - West of UTC, it fails early.

  Only runs outside the container are affected.

### M5. Columns are replicated by denylist
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED

### M6. Undo has no rollback on errors and cannot reverse a run that re-activates SSN duplicates
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED, RECLASSIFIED to NIT
- **Reasoning**:
  - **First bullet**: cosmetic. The server rolls back on disconnect, as the finding itself says.
  - **Second bullet**: needs a contrived sequence. Once the index exists, no two active customers can share an SSN. A later run can therefore only produce `exclude_test` decisions, because merges and holds both need a shared SSN among active customers. Undoing that run collides only if a new active customer took the excluded account's SSN in between. The realistic version is the KYC sandbox SSN, covered by S4.

### M7. Wrong hold reason for non-funded members of a double-funded group
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED
- **Reasoning**: My independent read reached the same line: `funded_ids` contains every member of the group. The seed does not exercise it, because every member of the 3 planted groups is funded.

### M8. The check holds a long snapshot and an exclusive warehouse lock; drill-down is unbounded
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED

### M9. Customer PII is printed to stdout
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED

### M10. `make test` covers only identity rules and planning
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED
- **Reasoning**: Caveat: CI's `make demo` is already an integration test with assertions over the main paths. The real gap is targeted scenarios. S1 and S5 below are exactly such scenarios.

### M11. Rolling out the unique index first breaks signups
- **First Review Severity**: MINOR
- **My Verdict**: CONFIRMED
- **Reasoning**: The index has a wider rollout hazard than failed duplicate signups: every write from a session with legacy SET options fails (S3).

### N1, N2, N4, N5, N6, N7
- **First Review Severity**: NIT
- **My Verdict**: CONFIRMED, each
- **Reasoning**:
  - N5: checked. `demo.py` calls `simulate.run()` three times.
  - N7: I reached the same point independently.

### N3. Bucket numbering differs for negative keys
- **First Review Severity**: NIT
- **My Verdict**: DISPUTED
- **Reasoning**: Verified: DuckDB's integer `//` truncates toward zero, like SQL Server's `/`. `-7 // 2` is `-3` in DuckDB 1.4.5, and `-7 / 2` is `-3` in SQL Server. Buckets are identical for negative keys, so no change is needed.

## Newly Identified Issues

### S1. `make repair` cannot fix duplicate keys in the append-only table, and the check keeps telling you to run it

- **Location**: `fundo/sync.py:162-184` (only change-tracking tables are rebuilt on a full refresh), `fundo/__main__.py:36-43`, `fundo/checks.py:261-263`
- **Severity**: MINOR (verified)
- **Problem**: A full refresh of `AdvanceStatusHistory` is a keyed MERGE into the existing table. That is deliberate: archive rows that the source deleted must survive.
  - If the warehouse holds two copies of a key, both match the source row, both are updated, and both stay.
  - The check fails `dup_keys` again, and its verdict says "Repair with `make repair`", so the operator loops.
  - The README's double-load breakage targets only `Cards`, a change-tracking table rebuilt by `CREATE OR REPLACE`. The demo never exercises this branch.
- **Example**: On a copy of the warehouse I duplicated `HistoryId 41621` and ran `repair`.
  - The load line showed `AdvanceStatusHistory full … ~upd 41,622`: both copies were "updated".
  - The check that followed printed `FAIL AdvanceStatusHistory.dup_keys: 1 extra copies of keys [41621]` and `VERDICT: FAIL … Repair with make repair`.
  ```python
  elif b.mode == "full" and t.strategy == "change_tracking":   # only CT tables are rebuilt
      ...
  else:                                                       # append-only full refresh = MERGE: duplicates survive
      wh.execute(f"MERGE INTO {target} AS t USING stg AS s ON t.{pk} = s.{pk} ...")
  ```
- **Fix**: Before the MERGE of an append-only full refresh, collapse duplicate keys and report them as deletes:
  ```python
  if b.mode == "full":  # append_only: keep archived rows, drop extra copies of a key
      before = wh.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
      wh.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT DISTINCT ON ({pk}) * FROM {target}")
      dup_removed = before - wh.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
  ```
- **Verification**: Duplicate a `HistoryId` in a warehouse copy and run `repair`; the check that follows passes. `make demo` output is unchanged.
- **Prevention**: Make `breakit` (or an integration scenario) plant one defect per table *strategy*, not only on change-tracking tables.

### S2. The check trusts the loader's own watermark and run time, so a loader that runs but stops advancing passes forever

- **Location**: `fundo/checks.py:91-106` (in-flight set built from the warehouse watermark), `fundo/checks.py:213-216` (freshness from `sync_state.synced_at`), `fundo/checks.py:150` (lag is `INFO` and never fails)
- **Severity**: MINOR (verified)
- **Problem**: Rows changed after the warehouse watermark are excluded from both sides and reported as lag. That is right for "behind is not wrong", but nothing bounds it:
  - Freshness measures when the loader last *ran*, because `synced_at` is written on every run, even one that moves nothing. It does not measure how old the oldest unloaded change is.
  - Lag never fails, whatever its size or age.

  A loader bug that keeps reporting success without advancing the watermark (or that rewinds it) therefore hides every row in that window, gaps included, for as long as the bug lasts. The rows are not lost, because a working load would pick them up, but the check never says so.

  The first review's summary praises in-flight exclusion; this is its blind spot. It mirrors `make break`'s "watermark advanced without applying", which the check does catch.
- **Example**: On a copy of the warehouse I deleted 646 Transactions inserted after change-tracking version 2. I then set the Transactions watermark to 2 and `synced_at` to `now()`.
  - The check printed `Transactions 138,943 138,943 0 0 0 0 0 821 PASS` and `VERDICT: PASS … (821 newer source changes not loaded yet)`.
  - The displayed source and warehouse counts are equal because both exclude the in-flight keys.
- **Fix**: Measure staleness from the source, as the age of the oldest unloaded change, and fail beyond `MAX_STALENESS_HOURS`. For change-tracking tables (this query was tested on this server):
  ```sql
  SELECT MIN(tc.commit_time) FROM CHANGETABLE(CHANGES dbo.Transactions, @watermark) c
  JOIN sys.dm_tran_commit_table tc ON tc.commit_ts = c.SYS_CHANGE_VERSION
  ```
  For `AdvanceStatusHistory`, `MIN(ChangedAt)` of the in-flight rows is a close-enough proxy.
- **Verification**: Repeat the experiment with a limit below the age of the oldest in-flight commit. The check should fail with "N changes older than the limit not loaded", even though `synced_at` is fresh.
- **Prevention**: Every exclusion in a reconciliation check needs an upper bound that does not depend on the system being checked.

### S3. The filtered unique index makes every write to `dbo.Customers` fail for sessions with legacy SET options

- **Location**: `fundo/dedupe.py:341-343`; `fundo/db.py:16`, whose comment shows the author knows about the requirement; not mentioned in SOLUTION.md's "Recurrence" or "Production"
- **Severity**: MINOR (verified). It matters for the production rollout, not for the demo.
- **Problem**: Once a filtered index exists, SQL Server rejects every INSERT, UPDATE, DELETE or MERGE on the table, with error 1934, from any session where `QUOTED_IDENTIFIER` or `ANSI_NULLS` (among others) is OFF. It does not matter whether the statement touches `Ssn`. Those settings are OFF:
  - by default in classic `sqlcmd` without `-I`;
  - in SQL Agent T-SQL job steps;
  - in any stored procedure or trigger *created* under them, because the setting is frozen at CREATE time.

  `--apply` creates the index on the operational database, in the same transaction as the merge. In a SQL Server shop with older procedures, every write to Customers (signups, profile edits, KYC updates) would start failing. That is broader than M11's "returning customers can't sign up".
- **Example**: Verified on the demo database, in rolled-back transactions.
  - A plain pymssql session (all options ON) updates `dbo.Customers` without error.
  - After `SET QUOTED_IDENTIFIER OFF`, the same `UPDATE dbo.Customers SET UpdatedAt = UpdatedAt WHERE CustomerId = 1` fails: `(1934) UPDATE failed because the following SET options have incorrect settings: 'QUOTED_IDENTIFIER' … filtered indexes`.
  - `SET ANSI_NULLS OFF` gives the same error.
- **Fix**:
  1. Before creating the index, `--apply` runs this query and refuses, listing the offenders, if it returns rows:
     ```sql
     SELECT OBJECT_SCHEMA_NAME(object_id) + '.' + OBJECT_NAME(object_id) FROM sys.sql_modules
     WHERE (uses_quoted_identifier = 0 OR uses_ansi_nulls = 0) AND definition LIKE '%Customers%';
     ```
  2. State the requirement in SOLUTION.md's rollout section, next to M11.
  3. Create the index as a separate step (`--add-constraint`) after the app's write paths have been checked.
- **Verification**: The query returns no rows on the target, and a staging smoke test of the app's write paths passes with the index in place.
- **Prevention**: Put filtered indexes, indexed views and indexes on computed columns on the migration checklist as "sensitive to SET options".

### S4. After the dedupe, QA can create only one more account with the KYC sandbox SSN

- **Location**: `fundo/dedupe.py:343` (the index filter), `fundo/identity.py:22` (`QA_SSNS`)
- **Severity**: MINOR (verified)
- **Problem**: The dedupe marks all five sandbox-SSN accounts `DedupeStatus='test'`, which takes them out of the index.
  - QA's next signup with `123456789` is active and is accepted.
  - The one after that is rejected with error 2601.

  The brief's premise is that QA accounts live in the operational database (`test@fundo.com` and similar). Production QA flows therefore break after their first new account. The `IsTest` flag that SOLUTION.md plans is not connected to the index either.
- **Example**: In a rolled-back transaction I inserted two customers with `Ssn='123456789'`. The first was inserted. The second was rejected: `(2601) Cannot insert duplicate key row … UX_Customers_Ssn_Active`.
- **Fix**: Leave the sandbox SSN out of the constraint. A filtered index accepts `<>`. I tested this on a temp table: two sandbox rows were accepted and a real duplicate was still rejected.
  ```sql
  CREATE UNIQUE INDEX UX_Customers_Ssn_Active ON dbo.Customers (Ssn)
  WHERE Ssn IS NOT NULL AND Ssn <> '123456789' AND DedupeStatus IS NULL
  ```
  Better still, QA tooling sets `DedupeStatus='test'` (or the future `IsTest`) when it creates the account, and the filter excludes it.
- **Verification**: The two-insert test accepts both sandbox rows, and `make demo` still prints the two re-signup rejections.
- **Prevention**: Generate the index filter from the same constants `classify_test` uses (`QA_SSNS`), so the two cannot drift apart.

### S5. A warehouse table that goes missing is rebuilt from the incremental delta, and the load reports success

- **Location**: `fundo/sync.py:157-161` (`CREATE TABLE … AS SELECT … FROM stg` whenever the table is absent), `fundo/sync.py:189-192` (state is read without checking that the table exists)
- **Severity**: MINOR (verified)
- **Problem**: Suppose `raw.<table>` is absent but `meta.sync_state` still holds its watermark. Dropping one table to force a reload is a natural operator move. The next load then runs incrementally and creates the table from that run's delta:
  - only the changed rows are loaded;
  - deleted keys are *inserted* as rows full of NULLs, because the CTAS ignores `_deleted`;
  - the watermark advances and the run prints `committed`.

  This breaks the module's own claim that a run "lands completely or not at all". The check catches it, and `repair` fixes it.
- **Example**: On a warehouse copy I ran `DROP TABLE raw."Cards"`, then `load`.
  - The load printed `Cards incremental 6->6 0 …` and `committed: 0 rows`, and `raw.Cards` now held 0 rows.
  - The check reported `Cards 5,090 0 5,090 … FAIL`.
- **Fix**: Treat a missing target as a first load, for example in `load_state()`:
  ```python
  wh.execute("DELETE FROM meta.sync_state WHERE table_name NOT IN "
             "(SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw')")
  ```
  Alternatively, make `apply()` refuse a CTAS from a batch that is not a full load.
- **Verification**: Drop a raw table and run `load`. That table loads as `full` (first load), and the check passes.
- **Prevention**: Assert in `apply()` that the CTAS branch only ever sees `mode == "full"`.

### S6. The check warns about SQL Server's own change-tracking history table

- **Location**: `fundo/db.py:61-66` (`table_rows` lists every table in `dbo`, including Microsoft-shipped ones), `fundo/checks.py:185-189`, `fundo/stats.py:10-12`
- **Severity**: MINOR (verified)
- **Problem**: About 30 minutes after Change Tracking is enabled, SQL Server 2022's auto-cleanup creates `dbo.MSchange_tracking_history` (`is_ms_shipped = 1`). From then on:
  - every check prints `WARN MSchange_tracking_history.unreplicated: source table with 40 rows is not replicated and not on the exclusion list: decide`;
  - `stats` lists it as a source table.

  The demo finishes in about a minute, so it never shows this. A reviewer who runs `make check` later does see it, and it appears in this environment's `make check` right now. The warning exists so that "a new table cannot go unnoticed"; a permanent false positive teaches people to ignore it.
- **Example**: `sys.tables` returns `('MSchange_tracking_history', is_ms_shipped=True, created 17:18)`, and the current `make check` output contains the WARN line.
- **Fix**: Add `AND t.is_ms_shipped = 0` to `db.table_rows`, and the same filter to the query in `stats.run`.
- **Verification**: `make check` prints no `unreplicated` line, and a new user table still triggers one.
- **Prevention**: Exclude `is_ms_shipped` objects from every catalogue scan.

### S7. The "proves" rule tells keying errors from duplicates, not stolen identities from duplicates

- **Location**: `fundo/identity.py:114-118` (`corroborated`: any one of first name, last name, email or phone), `fundo/dedupe.py:110-128`; the claim is at `fundo/identity.py:3-6` and `SOLUTION.md:36`
- **Severity**: MINOR (judgement; measured)
- **Problem**: SOLUTION.md holds a bare SSN+DOB match because it "is also what a keying error or a stolen identity looks like". That implies corroboration screens out stolen identities, and it cannot:
  - To pass KYC, someone using a stolen SSN and DOB must also give the victim's *name*. Their account is corroborated by first and last name, and it is merged.
  - The impostor's cards and unfunded advances then move onto the victim's account. SOLUTION.md:66 says this "debit a stranger's card" outcome is why only proof merges.

  What tells a re-signup from a stolen identity is contact and device data. On the applied run, 40 of 129 merges shared no email and no phone:
  - 32 were corroborated by first and last name only;
  - 5 by first name only;
  - 3 by last name only.

  In the seed these are all true duplicates, because the seed never plants an impostor who passed KYC.
- **Example**:
  ```python
  def corroborated(a, b):
      return bool((a["first"] and a["first"] == b["first"]) or (a["last"] and a["last"] == b["last"])
                  or (a["email"] and a["email"] == b["email"]) or (a["phone"] and a["phone"] == b["phone"]))
  ```
- **Fix**:
  1. At minimum, correct the claim: corroboration rules out keying errors; ruling out stolen identities needs contact or device evidence.
  2. Then choose one of:
     - Require a shared contact detail for an automatic merge: email, phone, card fingerprint, or the address the seed already plants. Hold name-only matches. That means 40 more holds on this data.
     - Keep merging name-only matches, but don't move the loser's cards and open advances until a human confirms.
  3. Plant a "KYC-passing impostor" case in the seed so the score covers it.
- **Verification**: If the rule is adopted, a unit test with the same SSN, DOB and name but a different email, phone and card expects `hold`. The seed score gains a line for impostors.
- **Prevention**: For every "proves" rule, write down the adversarial case it must reject, and plant that case in the seed.

### S8. `ISNULL` keeps the column's type, so a one-character column's NULL marker is truncated

- **Location**: `fundo/checks.py:52`
- **Severity**: NIT (verified; latent)
- **Problem**: `ISNULL(col, N'\N')` returns the type of `col`. On this server, `ISNULL(CAST(NULL AS CHAR(1)), N'\N')` returns `'\'`, while DuckDB renders `\N`. Any future nullable `CHAR(1)` or `VARCHAR(1)` column, such as a flag, would report every NULL as "different". No such column exists today; `State CHAR(2)` fits exactly.
- **Example**: `return f"ISNULL({c}, N'{NULL}')"`
- **Fix**: Use `COALESCE(...)`, which returns the higher-precedence NVARCHAR type, or cast the column to `NVARCHAR(MAX)` first.
- **Verification**: Add a nullable `CHAR(1)` column with NULLs; the check still passes.
- **Prevention**: Don't use `ISNULL` when building canonical text.

### S9. Two SOLUTION.md statements don't match the code

- **Location**: `SOLUTION.md:43`, `SOLUTION.md:20`; `sql/dedupe_migration.sql:4`; `fundo/sync.py:136-138`
- **Severity**: NIT (from code reading)
- **Problem**:
  - SOLUTION.md:43 says both accounts of a held double-funded pair "stay live". The non-primary account gets `DedupeStatus='hold'`, which the migration defines as not active (`NULL = active`). The unique index ignores it, and `simulate` treats it as inactive.
  - SOLUTION.md:20 lists "when the source schema changes" as a reason for a full snapshot. For the append-only table, a schema change is a deliberate hard stop instead.
- **Fix**: Change "stay live" to "neither is merged; one is marked hold". Note the append-only exception to schema-change refreshes.
- **Verification**: Re-read SOLUTION.md against `plan()` and `read_source()`.
- **Prevention**: n/a.

### S10. A wrong password hangs for two minutes

- **Location**: `fundo/db.py:24-42`
- **Severity**: NIT (verified)
- **Problem**: A login failure raises `pymssql.OperationalError`, the same class the startup retry loop catches. So a wrong `MSSQL_SA_PASSWORD` retries silently for 120 s before the error appears.
- **Example**: `except pymssql.OperationalError: … time.sleep(2)`, with no check of the error number.
- **Fix**: Fail fast on SQL Server error 18456 (login failed); keep retrying only while the server is not accepting connections.
- **Verification**: `docker compose run --rm -e MSSQL_SA_PASSWORD=wrong app check` fails within seconds.
- **Prevention**: Retry loops should name the errors they retry.

### S11. `load --full-refresh` silently ignores unknown table names

- **Location**: `fundo/__main__.py:13-14`, `fundo/sync.py:131`
- **Severity**: NIT (verified)
- **Problem**: `load --full-refresh customers` (wrong case) printed `Customers incremental 6->6` with no warning, so an operator believes the table was refreshed. `--crash-after` behaves the same way.
- **Example**: `load.add_argument("--full-refresh", nargs="*", default=[], metavar="TABLE")`
- **Fix**: Add `choices=[t.name for t in TABLES]` to both arguments.
- **Verification**: `load --full-refresh customers` exits with an argparse error.
- **Prevention**: Validate every table-name argument against `TABLES`.

## Areas Re-examined With No New Findings

- **Loader ordering.** The version bounds (`CHANGE_TRACKING_CURRENT_VERSION()`, `MIN_ACTIVE_ROWVERSION()`) are read inside the SNAPSHOT transaction before any table data. A commit racing that read can only be read twice (and re-applied by the next run), never skipped. Also checked and correct:
  - the Change Tracking min-valid check;
  - delete detection through the LEFT JOIN;
  - half-open rowversion windows;
  - crash atomicity;
  - DuckDB's single-writer lock, which prevents concurrent loads.
- **Replay.** It is safe from *any* older Change Tracking version, because net changes are joined to current rows. Replaying after a repair is therefore safe too.
- **Stale DuckDB WAL.** `os._exit` in the middle of a transaction leaves `warehouse.duckdb.wal`, and `make seed`/`demo` delete only the `.duckdb` file. Verified: DuckDB 1.4.5 opens a fresh, empty database and ignores the leftover WAL.
- **Check hashing.** The canonical text is the same on both engines for DECIMAL, BIT, DATETIME2 at ms precision, UTF-8 (via the `_UTF8` collation) and padded `NVARCHAR(MAX)` owners. Bucket arithmetic matches too (see N3).
- **Dedupe apply and undo.**
  - Each (table, key, column) changes at most once per run, so undo order doesn't matter.
  - The survivor's active card fingerprints are re-read for each loser, so a three-account group can't add the same card twice.
  - Undo's conflict check round-trips BIT, INT and text values correctly.
  - Re-running `--apply` is idempotent: decided customers are skipped, and the index prevents new SSN groups.
- **Injection surface.** Every interpolated identifier is a code constant; values are parameterised or cast with `int()`.
- **Docs against measurements.** README and SOLUTION numbers match `meta.load_log` and a re-count:
  - 200,808 rows / 13.6 MB for the full load;
  - 1,317 rows / 95.3 KB (0.68%) for the incremental load;
  - repair `+300 −25 ~1 −1`;
  - 32 tests;
  - email 50 / 41 / 18;
  - phone 79 / 30 / 25 / 15;
  - 129 merged, 14 test, 5 held.

  `.env.arm64` exists, and `data/` and `reports/` are gitignored.

## Summary (second pass)

**Verdict on the first review: Mostly Aligned.**
- Its mechanisms are right, and it found the defects that matter most. H1, H2 and H6 hold at HIGH; I re-measured H2 and verified H6 by emulation.
- It over-weighted three code-reading findings (H3, H4, H5). Each needs a race in a one-off window, a customer the data doesn't contain, or a deliberate run outside Docker. Each is also reversible or blocked by the compose setup.
- Its evidence had small factual slips: H2's SOLUTION quote and population, M4's "never", and N3's DuckDB semantics.

**Fix first**:
1. **H2** (default card): a verified defect, deterministic on the planted data, in the area the brief asks about. The realistic trigger is an expired default card.
2. **H6**: Linux reviewers can't run documented commands.
3. **H1**: a one-line fix.
4. **S1 and S2**: the check is what SOLUTION.md says to deliver first. S1 makes its own repair advice loop; S2 is its blind spot.
5. **S3, S4 and M11**: one rollout note and filter change for the unique index.
6. **S7**: one honest sentence in SOLUTION.md. The brief: "An honest gap costs less than a confident guess."

**Methodological gaps in the first review**:
- `repair` was exercised only on change-tracking tables, through `make break`.
- In-flight exclusion was praised but not tested against a loader bug.
- The unique index was reviewed only for duplicate signups, not for its side effects on other writers and on QA.
- The check was never run long after seeding, which is what exposes S6.
- N3 was asserted without testing DuckDB's division semantics.

**Combined severity after both passes**:

| Severity | Count | Findings |
|---|---|---|
| High | 3 | H1, H2, H6 |
| Minor | 18 | H3, H4, H5, M3, M4, M5, M7, M8, M9, M10, M11, S1–S7 |
| Nit | 13 | M1, M2, M6, N1, N2, N4–N7, S8–S11 |
| Disputed | 1 | N3 |

## Resolution (2026-10-07, after both passes)

Fixes were verified on SQL Server 2022 with `make test` (34 passed), `make demo` (all 9 steps OK, including a new assertion for H2) and `make check` (PASS), plus the targeted reproductions noted below.

| Finding | Status | How it was verified |
|---|---|---|
| H1 exposed `sa` | Fixed: published on `127.0.0.1` only | `docker port` shows `127.0.0.1:14330` only |
| H2 survivor left without a default card | Fixed: `move_cards()` promotes the loser's active card when the survivor has no active default, and demotes an expired default | New demo assertion: survivors without a default card = 0 |
| H3 plan/apply race | Fixed: `still_valid()` re-checks each decision under `UPDLOCK, HOLDLOCK`; `change()` reads with `UPDLOCK` | Code; the concurrent race itself was not reproduced |
| H4 reserved-domain email excluding a KYC'd customer | Fixed: a valid SSN downgrades the email rules to `review` | Unit test; seed score unchanged (14/14) |
| H5 no target guard | Fixed: `seed`/`demo`/`break` require `FUNDO_ALLOW_RESET=1` (set only in compose) | Without it, `seed` refuses and exits 1 |
| H6 root-owned files on Linux | Fixed: the app runs as `HOST_UID:HOST_GID` (exported by the Makefile); the warehouse reset moved into `seed` | `id` in the container prints uid 501; CI now runs `make seed && make down` on Linux (not yet run) |
| M1, N5, S9 doc slips | Fixed in README.md and SOLUTION.md | Re-read against the code |
| M2 backup after migration | Fixed: backup first; output says the volume holds it | Demo output |
| M3 no FK on `DuplicateOfCustomerId` | Fixed: FK in the migration; simulated purge skips referenced customers | Demo passes with the FK |
| M4 local-time freshness | Fixed: timestamps written from Python in UTC | Code |
| M6 undo error handling | Fixed: try/rollback, like apply | Code |
| M7 hold reason | Fixed | Code |
| M10 tests | Partly: demo assertion for H2, CI cleanup step | |
| M11, S3, S4 index rollout | S4 fixed (sandbox SSN excluded from the index; two sandbox inserts accepted); M11 and S3 documented in SOLUTION.md's rollout note | Rolled-back insert test |
| S1 repair loops on history duplicates | Fixed: append-only full refresh collapses duplicate keys | Planted a duplicate `HistoryId`; repair removed it (`-del 1`) and the check passes |
| S2 lag never fails | Fixed: the age of the oldest unloaded change (from `sys.dm_tran_commit_table` / `ChangedAt`) fails beyond `MAX_STALENESS_HOURS` (now configurable) | With `MAX_STALENESS_HOURS=0.001` after a simulated day, every table with lag FAILs; PASS after a load |
| S5 missing table rebuilt from a delta | Fixed: state rows without a table are dropped, so the table reloads in full; the check no longer crashes when a table is missing | Dropped `raw.Cards`: the check reports it; repair reloads it in full |
| S6 system table warning | Fixed: `is_ms_shipped = 0` | `make check` no longer warns |
| S7 corroboration by name | Fixed: proof needs a shared email, phone, card or address; 2 stolen-identity cases planted and scored (2/2 held) | Unit test plus seed score |
| S8 `ISNULL` truncation | Fixed: `COALESCE` | The check still passes (hashes unchanged) |
| S10 wrong password hangs | Fixed: fails fast on 18456 | A wrong password fails in about 1 s |
| S11 unknown table names | Fixed: argparse `choices` | `--full-refresh customers` errors |
| N4, N6 | Fixed | Unit tests, argparse |

**Left as documented trade-offs:**
- **M5:** column denylist.
- **M8:** check scheduling and unbounded drill-down.
- **M9:** names in demo output (synthetic data).
- **N1, N2, N7.**
- **`--apply` re-plans instead of applying the reviewed dry run** (listed in SOLUTION.md's gaps).
