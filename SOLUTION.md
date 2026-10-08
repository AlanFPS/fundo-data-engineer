# Solution

I took on all three problems and kept each small:
- an incremental loader (`sync.py`);
- a source-vs-warehouse check (`checks.py`);
- duplicate resolution (`identity.py`, `dedupe.py`).

Numbers are **[measured]** by `make demo` on SQL Server 2022 (about 200k synthetic rows) or **[estimated]**.

## 1. Move only what changed

| Table | Strategy | Why, and the trade-off |
|---|---|---|
| Transactions (64% of bytes), Customers, Advances, Cards | **Change Tracking**: net changes since the last version, deletes included | Rows change late (pending→settled, chargebacks) and get deleted, and Transactions has no `UpdatedAt`, so a timestamp watermark sees neither. The cost: a synchronous write to a side table per change (about 1% of the Transactions table after three simulated days), and only each row's latest state, not every intermediate version. |
| AdvanceStatusHistory | **ROWVERSION watermark**; deletes not propagated | Append-only, so it costs nothing at the source. The upper bound `MIN_ACTIVE_ROWVERSION()` stops uncommitted rows from being skipped forever. Not propagating deletes makes the warehouse the archive, so the source can stop keeping history forever. |
| tmp_CustomerExport_2021, Customers.Ssn | **Not replicated** | A scratch table with no owner, key or readers, and PII the warehouse doesn't need. Replication is an allowlist, and the check warns about any new source table. |

A full snapshot happens only in these cases:
- the first load, including a warehouse table that has gone missing;
- Change Tracking history has expired;
- a mirrored table's schema has changed;
- a repair.

A schema change on the append-only table stops the load instead, because a reload would drop the archive.

Each run reads every table in one SNAPSHOT transaction. It then applies them, together with the new watermarks, in one warehouse transaction of keyed MERGEs. As a result:
- a second run moves nothing;
- replaying an already-applied window leaves the warehouse fingerprint unchanged;
- killing the load after 3 of 5 tables lands nothing.

**[measured]** A simulated day changes 0.3–1.1% of each table. The incremental load moved **1,307 rows / 95 KB instead of 203,193 rows / 13.8 MB (0.67% of the bytes)**. Its per-table counts are identical to what the simulator wrote.

## 2. Duplicate customers

| | Fields | Why |
|---|---|---|
| **Proves** | Same valid **SSN + date of birth**, plus **a shared email, phone, card or postal address** | SSN+DOB is what KYC verified. A name doesn't count as the extra detail: someone using a stolen identity passes KYC with the victim's name, but rarely with the victim's inbox, phone, card or address. Without one, the match is held (keying error or stolen identity). |
| **Suggests** | Email, phone, name + DOB, card fingerprint | Families share phones and inboxes, carriers recycle numbers, names repeat. These only queue a pair for review. |
| **Disproves** | Two different valid SSNs | Different people sharing a contact. They aren't even queued (33 such pairs in the demo). |

**Survivor:** a funded or paid-off customer first, then any customer with an advance, then the oldest account. Nothing is deleted: the loser stays as `DedupeStatus='merged'`, pointing at the survivor, and its advances and cards move to the survivor.

**Funded customers are untouchable.** They always survive and only ever gain records. When two funded accounts prove to be one person, I don't merge them. Both keep their records, one is flagged `hold`, and the two are linked so underwriting sees the combined exposure. Ops and compliance decide; the merge waits for collections and credit reporting, because a merge re-ages debt.

**Test data is excluded, not merged,** and only on proof:
- an RFC 2606 reserved domain (it cannot receive mail);
- a QA mailbox on our own domain, such as `test1@` or `qa+x@fundo.com`;
- the KYC sandbox SSN.

A real SSN that passed KYC overrides the email rules. Weaker signals go to a human: `test+kyc@gmail.com`, mailinator addresses, `loadtest42@fundo.com`. Substrings never count, so the Testermans and six staff on `@fundo.com` stay real customers. Exclusion runs first, because the QA accounts share a fake SSN and include funded advances.

**Malformed contacts are flagged, never rewritten.** Fixing `gmial.com` is a guess, and a wrong guess can mail someone's finances to a stranger. Phones are normalised to E.164 for matching only. Placeholders, and values shared by more than 5 customers, aren't used as evidence. **[measured]**

| Field | Counts |
|---|---|
| Email | 44 invalid, 44 suspect typo, 19 placeholder |
| Phone | 71 invalid, 27 placeholder, 34 missing, 18 non-US |

**Cards** move to the survivor, which keeps its default card. If the survivor has no usable default (often because it expired), the loser's active card becomes the default. A card the survivor already holds (same fingerprint) is marked removed. The owner column is `NVARCHAR(MAX)` text, so matching parses it: string equality would miss 144 owners stored as `' 1234'`. Getting this wrong means a card that is debited but invisible to the person, or charged twice, or, after a false merge, a stranger being debited. That last case is why only proof merges.

**Recurrence** is stopped by a filtered unique index on SSN among active customers, with QA's sandbox SSN excluded. In the demo, two re-signups are rejected. Before the production rollout:
- the app must turn error 2601 into "you already have an account";
- every writer must use the ANSI SET options that a filtered index requires.

Pre-KYC duplicates have no SSN yet, so they need a prompt at signup instead.

**Safety.**
- Dry run by default.
- `--apply` backs up first. In one transaction, it re-checks each decision under lock and logs every changed value.
- `--undo` reverses the last run, unless a value has been edited since.

**[measured]** 128 accounts merged into 110 survivors (93 of them funded). Against the planted truth:
- 106/106 duplicate groups merged.
- 3/3 double-funded groups, 2/2 bare SSN+DOB matches and 2/2 stolen identities held.
- 34/34 lookalike pairs left unmerged.
- 14/14 test accounts excluded, 3/3 ambiguous ones sent to a human, 10/10 lookalikes kept.

That proves the code follows the rules, not that the rules fit real data.

## 3. Prove the data is correct

`make check` prints one line per table, one line per problem and a verdict. It exits non-zero on failure and appends every result to `meta.check_results`. It finds:
- missing rows, as key ranges;
- rows the source has deleted;
- changed values, with the column and both values;
- duplicate keys;
- orphans.

- **Content, not counts.** Both engines compute the same MD5 over a canonical UTF-8 form of each row. Only `count + hash` per key range crosses the wire, and only ranges that disagree are fetched row by row.
- **Behind is not wrong, up to a limit.** Changes newer than the last load are excluded and reported as lag. Once the oldest of them is 26 hours old, the check fails. That age comes from the source's commit log, not from the loader's bookkeeping.
- **The deliberate breakage includes a loader bug:** it advances its watermark without applying deletes. The pipeline believes it is up to date; the check still finds the 25 rows.

**[measured]** All five tables are content-checked in 1.3–2.4 s.

## Measured vs estimated, and where data changed my mind

- **Measured:** everything above, plus the full load (1.2–1.7 s), incremental loads (0.1–0.3 s) and the whole demo (about 60 s).
- **Estimated:** everything about production scale and cost.

Where the data changed my mind:
- **Per-row hashing was too slow.** `HASHBYTES` per row measured 0.45 ms per call on Azure SQL Edge, 62 s for Transactions alone. One hash per key range brought the whole check to 1.5 s.
- **The scorer caught a bad label.** Scoring against the planted truth flagged a double-funded group as merged; the dedupe was right and the seed was wrong.
- **The code audit changed three rules** (`docs/code-review/`). Names no longer count as proof, a real SSN now outweighs a test-looking email, and a merge can no longer leave a survivor without a default card (it had left 5).

## Cost impact [estimated]

**Today's bill is driven by:**
- moving the whole database every day: egress at about $0.05–0.09/GB if SQL Server is outside GCP, plus a daily full scan of the operational database;
- rewriting every table daily: with physical storage billing, replaced data is kept for about two weeks;
- rebuilding downstream models from scratch, at $6.25/TiB scanned.

**Incremental loading** cuts the data moved by 100–150×. A 200 GB database goes from about 6 TB a month to about 50 GB, and batch loads into BigQuery are free.

**The trap:** a BigQuery `MERGE` bills the bytes it scans in the target. Merging 1% of changes into an unpartitioned 1 TB table still costs about $6.25 per run. Partition Transactions by month and limit the MERGE to the partitions it touches.

**Managed CDC isn't free either.** Datastream charges $2/GiB processed, and Google says it processes 2–5× the source bytes: about $250–600 a month for the 200 GB example.

## Production

- **Tools.**
  - Replication: Datastream into BigQuery. It reads SQL Server CDC or the transaction log, not Change Tracking, so production would turn on CDC.
  - Fallback: this loader as a Cloud Run job, landing Parquet in GCS for a free batch load and a pruned MERGE.
  - Checks: this check, or Google's Data Validation Tool, after every load with an alert. It stays independent of whatever does the replication.
  - dbt for the resolved-customer and test-excluded models; Cloud Scheduler rather than Composer.
- **One-time:**
  - the backfill;
  - the historical dedupe, with ops working through the holds and the review queue;
  - retiring the scratch table (rename it, watch usage for 30 days, back it up, drop it);
  - trimming source history once the check proves the warehouse holds it.
- **Permanent:**
  - replication, with the check and its alert;
  - the SSN constraint and the "account exists" flow;
  - the review queue;
  - an `IsTest` flag set by QA tooling;
  - contact validation at input.
- **Delivered first: the check.** It's the cheapest, it says straight away how wrong today's warehouse is, and it makes the switch safe: run full and incremental side by side until the check proves them identical. The dedupe comes last, because it changes customer data and needs compliance sign-off.

## Gaps

- **DuckDB stands in for BigQuery.** Partitioning is described, not built.
- **Extraction is in memory.** That's fine for daily deltas; a large backfill would need key-range chunking.
- **Smaller pieces not built:**
  - no history-retention job;
  - the review queue is a CSV;
  - names, emails and phones replicate unmasked.
- **`--apply` re-plans instead of executing the reviewed dry run.** It also locks a range of Cards, because the owner column can't be indexed, so it belongs in a maintenance window.
