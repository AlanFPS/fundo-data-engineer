"""Is the warehouse complete and correct right now?

For every replicated table the source is compared with the warehouse:

  missing      rows the source has and the warehouse does not (gaps)
  unexpected   rows the warehouse has and the source no longer does (missed deletes)
  different    same key, different content
  dup keys     one key loaded twice (BigQuery does not enforce primary keys)
  orphans      child rows whose parent is absent; the source has the foreign key,
               so any orphan in the warehouse was introduced by loading

Rows changed at the source after the last load are excluded from the comparison
and reported as lag: a warehouse that is behind is stale, not wrong, and the check
should not fail during business hours because the source kept working.

Content is compared through a row hash both engines compute identically: MD5 over
a canonical UTF-8 text form of the row. Only per-bucket sums (count, hash sum per
key range) leave the databases unless a bucket disagrees; then only that bucket's
(key, hash) pairs are fetched to name the exact rows.
"""
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import pyarrow as pa

from . import db
from .sync import source_columns
from .tables import NOT_REPLICATED, TABLES

MAX_STALENESS_HOURS = float(os.environ.get("MAX_STALENESS_HOURS", 26))  # daily load plus slack
NULL = r"\N"


@dataclass
class Result:
    table: str
    check: str
    status: str   # PASS | FAIL | WARN | INFO
    value: int
    detail: str = ""


def src_text(name, dtype):
    c = f"t.[{name}]"
    if dtype in ("int", "bigint", "smallint", "tinyint", "bit", "decimal", "numeric"):
        c = f"CONVERT(VARCHAR(50), {c})"
    elif dtype == "date":
        c = f"CONVERT(VARCHAR(10), {c}, 23)"
    elif dtype == "datetime2":
        c = f"CONVERT(VARCHAR(20), DATEDIFF_BIG(MILLISECOND, '1970-01-01', {c}))"
    return f"COALESCE({c}, N'{NULL}')"  # not ISNULL: it would truncate the marker to the column's length


def wh_text(name, dtype):
    c = f'"{name}"'
    if dtype == "bit":
        c = f"CAST({c} AS INTEGER)"
    elif dtype == "datetime2":
        c = f"epoch_ms({c})"
    return f"coalesce(CAST({c} AS VARCHAR), '{NULL}')"


def key_ranges(keys, limit=4):
    """[1,2,3,7] -> '1-3, 7' : gaps read better as ranges."""
    out, start, prev = [], None, None
    for k in keys:
        if start is None:
            start = prev = k
        elif k == prev + 1:
            prev = k
        else:
            out.append(f"{start}-{prev}" if prev != start else f"{start}")
            start = prev = k
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else f"{start}")
    return ", ".join(out[:limit]) + (f" (+{len(out) - limit} more ranges)" if len(out) > limit else "")


def compare(cur, wh, table, watermark):
    name, pk, b = table.name, table.pk, table.bucket
    cols = source_columns(cur, table)
    wh_cols = {r[0] for r in wh.execute("SELECT column_name FROM information_schema.columns "
                                        "WHERE table_schema = 'raw' AND table_name = ?", [name]).fetchall()}
    if not wh_cols:
        return [Result(name, "loaded", "FAIL", 0, "table is not in the warehouse; run the load")]
    drift = {c for c, _, _ in cols} ^ wh_cols
    if drift:
        return [Result(name, "schema", "FAIL", len(drift), f"columns differ: {sorted(drift)}; run the load")]

    if table.strategy == "change_tracking":
        min_valid = db.scalar(cur, "SELECT CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID(%s))", (f"dbo.{name}",))
        if watermark < min_valid:
            return [Result(name, "lag", "FAIL", 0, "change tracking history expired; run a full refresh")]
        in_flight = f"SELECT c.[{pk}] FROM CHANGETABLE(CHANGES dbo.[{name}], {watermark}) c"
        # Age of the oldest unloaded change, from the source's own commit log: lag is allowed, but
        # not unbounded, and a loader that runs without advancing its watermark must not pass forever.
        oldest = db.scalar(cur, f"SELECT DATEDIFF(SECOND, MIN(tc.commit_time), GETDATE()) "
                                f"FROM CHANGETABLE(CHANGES dbo.[{name}], {watermark}) c "
                                f"JOIN sys.dm_tran_commit_table tc ON tc.commit_ts = c.SYS_CHANGE_VERSION")
    else:
        in_flight = f"SELECT t.[{pk}] FROM dbo.[{name}] t WHERE t.RowVer >= CONVERT(BINARY(8), CAST({watermark} AS BIGINT))"
        oldest = db.scalar(cur, f"SELECT DATEDIFF(SECOND, MIN(t.[{table.changed_at}]), SYSUTCDATETIME()) "
                                f"FROM dbo.[{name}] t WHERE t.RowVer >= CONVERT(BINARY(8), CAST({watermark} AS BIGINT))")
    oldest_h = (oldest or 0) / 3600
    cur.execute(in_flight)
    lag = [r[0] for r in cur.fetchall()]
    wh.register("in_flight", pa.table({"k": pa.array(lag, pa.int64())}))

    names = [n for n, _, _ in cols]
    src_rows = (f"SELECT t.[{pk}] AS k, CONCAT(" + ", N'|', ".join(src_text(n, d) for n, d, _ in cols)
                + f") AS r FROM dbo.[{name}] t WHERE t.[{pk}] NOT IN ({in_flight})")
    wh_rows = (f'SELECT "{pk}" AS k, concat(' + ", '|', ".join(wh_text(n, d) for n, d, _ in cols)
               + f') AS r FROM raw."{name}" WHERE "{pk}" NOT IN (SELECT k FROM in_flight)')

    # One hash per bucket: MD5 of the bucket's rows (canonical text, key order, UTF-8).
    cur.execute(f"SELECT k / {b}, COUNT_BIG(*), CONVERT(CHAR(32), HASHBYTES('MD5', CONVERT(VARCHAR(MAX), "
                f"STRING_AGG(CONVERT(NVARCHAR(MAX), r), NCHAR(10)) WITHIN GROUP (ORDER BY k) "
                f"COLLATE Latin1_General_100_BIN2_UTF8)), 2) FROM ({src_rows}) x GROUP BY k / {b}")
    src_buckets = {k: (n, h) for k, n, h in cur.fetchall()}
    wh_buckets = {k: (n, h) for k, n, h in wh.execute(
        f"SELECT k // {b}, count(*), upper(md5(string_agg(r, chr(10) ORDER BY k))) FROM ({wh_rows}) GROUP BY 1").fetchall()}
    bad = sorted(k for k in src_buckets.keys() | wh_buckets.keys() if src_buckets.get(k) != wh_buckets.get(k))
    missing = unexpected = different = []
    example = ""
    if bad:  # drill into the disagreeing key ranges only
        keys = ", ".join(str(k) for k in bad)
        cur.execute(f"SELECT k, r FROM ({src_rows}) x WHERE k / {b} IN ({keys})")
        src = dict(cur.fetchall())
        whr = dict(wh.execute(f"SELECT k, r FROM ({wh_rows}) WHERE k // {b} IN ({keys})").fetchall())
        missing = sorted(src.keys() - whr.keys())
        unexpected = sorted(whr.keys() - src.keys())
        different = sorted(k for k in src.keys() & whr.keys() if src[k] != whr[k])
        if different:
            k = different[0]
            diffs = [f"{n} {a!r} in source vs {w!r} in warehouse"
                     for n, a, w in zip(names, src[k].split("|"), whr[k].split("|")) if a != w]
            example = f"; e.g. {pk} {k}: " + ", ".join(diffs[:2])
    wh.unregister("in_flight")
    dups, dup_keys = wh.execute(f'SELECT count(*) - count(DISTINCT "{pk}"), (SELECT list(k) FROM (SELECT "{pk}" AS k '
                                f'FROM raw."{name}" GROUP BY 1 HAVING count(*) > 1 ORDER BY 1 LIMIT 5)) '
                                f'FROM raw."{name}"').fetchone()
    n_src = sum(n for n, _ in src_buckets.values())
    n_wh = sum(n for n, _ in wh_buckets.values())

    archive = table.strategy == "append_only"
    return [
        Result(name, "rows", "INFO", n_src, f"{n_wh}"),
        Result(name, "missing", "FAIL" if missing else "PASS", len(missing),
               f"in source, not in warehouse: keys {key_ranges(missing)}" if missing else ""),
        Result(name, "unexpected", ("WARN" if archive else "FAIL") if unexpected else "PASS", len(unexpected),
               (f"deleted at source, kept by design (archive): keys {key_ranges(unexpected)}" if archive else
                f"in warehouse, no longer in source: keys {key_ranges(unexpected)}") if unexpected else ""),
        Result(name, "different", "FAIL" if different else "PASS", len(different),
               f"content differs: keys {key_ranges(different)}{example}" if different else ""),
        Result(name, "dup_keys", "FAIL" if dups else "PASS", dups,
               f"{dups} extra copies of keys {dup_keys}" if dups else ""),
        Result(name, "lag", "FAIL" if oldest_h > MAX_STALENESS_HOURS else "INFO", len(lag),
               f"{len(lag)} source changes not loaded yet, the oldest {oldest_h:.1f}h old "
               f"(limit {MAX_STALENESS_HOURS}h)" if lag else ""),
    ]


def referential(wh):
    out = []
    for child, parent, key in [("Transactions", "Advances", "AdvanceId"), ("Advances", "Customers", "CustomerId")]:
        n, sample = wh.execute(
            f'SELECT count(*), list(c."{key}" ORDER BY c."{key}")[:5] FROM raw."{child}" c '
            f'WHERE NOT EXISTS (SELECT 1 FROM raw."{parent}" p WHERE p."{key}" = c."{key}")').fetchone()
        out.append(Result(child, "orphans", "FAIL" if n else "PASS", n,
                          f"{n} rows point at a {parent[:-1]} the warehouse does not have, e.g. {key} {sample}" if n else ""))
    return out


def data_quality(wh, cur):
    """Problems in the source data itself. They inform; they do not fail the check."""
    out = []
    owner = 'TRY_CAST(trim(k."CustomerId") AS INTEGER)'
    n, sample = wh.execute(f'SELECT count(*), list(k."CustomerId")[:5] FROM raw."Cards" k '
                           f'WHERE NOT EXISTS (SELECT 1 FROM raw."Customers" c WHERE c."CustomerId" = {owner})').fetchone()
    out.append(Result("Cards", "owner_unresolvable", "WARN" if n else "PASS", n,
                      f"{n} cards' CustomerId text matches no customer, e.g. {sample}"))
    n = wh.execute(f'SELECT count(*) FROM raw."Cards" k WHERE {owner} IS NOT NULL '
                   f'AND k."CustomerId" <> CAST({owner} AS VARCHAR)').fetchone()[0]
    out.append(Result("Cards", "owner_not_canonical", "WARN" if n else "PASS", n,
                      f"{n} cards store their owner as padded or zero-prefixed text (NVARCHAR(MAX) column)"))
    has_merge = wh.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'raw' "
                           "AND table_name = 'Customers' AND column_name = 'DedupeStatus'").fetchone()[0]
    if has_merge:
        n = wh.execute(f'SELECT (SELECT count(*) FROM raw."Cards" k JOIN raw."Customers" c ON c."CustomerId" = {owner} '
                       f'WHERE c."DedupeStatus" = \'merged\') + (SELECT count(*) FROM raw."Advances" a '
                       f'JOIN raw."Customers" c USING ("CustomerId") WHERE c."DedupeStatus" = \'merged\')').fetchone()[0]
        out.append(Result("Customers", "merged_with_children", "WARN" if n else "PASS", n,
                          f"{n} cards/advances still belong to a merged-away customer"))
    known = {t.name for t in TABLES} | set(NOT_REPLICATED)
    for table, rows in db.table_rows(cur):
        if table not in known:
            out.append(Result(table, "unreplicated", "WARN", rows, f"source table with {rows:,} rows is not replicated "
                              "and not on the exclusion list: decide"))
    return out


def run(quiet=False):
    t0 = time.time()
    wh = db.warehouse()
    wh.execute("CREATE SCHEMA IF NOT EXISTS meta")
    wh.execute("CREATE TABLE IF NOT EXISTS meta.check_results (checked_at TIMESTAMP, table_name VARCHAR, "
               "check_name VARCHAR, status VARCHAR, value BIGINT, detail VARCHAR)")
    has_state = wh.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'meta' "
                           "AND table_name = 'sync_state'").fetchone()[0]
    state = {r[0]: r[1:] for r in wh.execute(
        "SELECT table_name, watermark, synced_at FROM meta.sync_state").fetchall()} if has_state else {}

    src = db.source(autocommit=True)
    cur = src.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
    cur.execute("BEGIN TRANSACTION")  # one consistent source view for every comparison
    results = []
    for table in TABLES:
        if table.name not in state:
            results.append(Result(table.name, "loaded", "FAIL", 0, "never loaded; run the load"))
            continue
        watermark, synced_at = state[table.name]
        age_h = (datetime.now(timezone.utc).replace(tzinfo=None) - synced_at).total_seconds() / 3600
        results.append(Result(table.name, "freshness", "FAIL" if age_h > MAX_STALENESS_HOURS else "PASS", round(age_h * 60),
                              f"last loaded {age_h:.1f}h ago (limit {MAX_STALENESS_HOURS}h)"))
        results += compare(cur, wh, table, watermark)
    present = {r[0] for r in wh.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw'").fetchall()}
    if all(t.name in state and t.name in present for t in TABLES):  # cross-table checks need every table
        results += referential(wh)
        quality = data_quality(wh, cur)
    else:
        quality = []
    cur.execute("COMMIT")
    src.close()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    wh.executemany("INSERT INTO meta.check_results VALUES (?, ?, ?, ?, ?, ?)",
                   [[now, r.table, r.check, r.status, r.value, r.detail] for r in results + quality])
    wh.close()
    if not quiet:
        report(results, quality, time.time() - t0)
    return [r for r in results if r.status == "FAIL"]


def report(results, quality, seconds):
    by = {(r.table, r.check): r for r in results}
    print(f"\nwarehouse check  {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z")
    print(f"  {'table':<22}{'source':>9}{'warehouse':>11}{'missing':>9}{'unexpect':>9}{'differ':>8}"
          f"{'dupkey':>8}{'orphan':>8}{'lag':>6}  result")
    for t in TABLES:
        rows = [r for r in results if r.table == t.name]
        get = lambda c: by.get((t.name, c))
        if get("rows") is None:
            print(f"  {t.name:<22}{'':>65}  FAIL")
            continue
        status = "FAIL" if any(r.status == "FAIL" for r in rows) else "PASS"
        fmt = lambda c: f"{get(c).value:,}" if get(c) else "-"
        print(f"  {t.name:<22}{get('rows').value:>9,}{int(get('rows').detail):>11,}{fmt('missing'):>9}"
              f"{fmt('unexpected'):>9}{fmt('different'):>8}{fmt('dup_keys'):>8}{fmt('orphans'):>8}"
              f"{get('lag').value:>6,}  {status}")
    problems = [r for r in results if r.status in ("FAIL", "WARN")]
    for r in problems:
        print(f"  {r.status}  {r.table}.{r.check}: {r.detail}")
    if quality:
        print("  data quality (source-side problems; reported, not failed):")
        for r in quality:
            print(f"    {r.status:<5}{r.table}.{r.check}: {r.detail if r.value else 'none'}")
    failed = [r for r in results if r.status == "FAIL"]
    tables = sorted({r.table for r in failed})
    lag = sum(r.value for r in results if r.check == "lag")
    if failed:
        print(f"VERDICT: FAIL - {len(failed)} problem(s) in {', '.join(tables)}. "
              f"Repair with `make repair` (full refresh of the failing tables).")
    else:
        print(f"VERDICT: PASS - warehouse matches source for all {len(TABLES)} tables"
              + (f" ({lag:,} newer source changes not loaded yet)" if lag else "") + f"  [{seconds:.1f}s]")
