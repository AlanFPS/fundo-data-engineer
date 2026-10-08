"""Incremental load: SQL Server -> DuckDB.

A run reads every table inside one SNAPSHOT transaction on the source, so all
tables describe the same instant. It then applies everything, together with
the new watermarks, in one DuckDB transaction. A run therefore lands completely
or not at all: a crash anywhere leaves the previous state intact and the next
run picks up from there. Every write is a keyed MERGE, so applying the same
window twice gives the same result.

Strategies (see tables.py):
  change_tracking  SQL Server Change Tracking returns the keys changed since the
                   last version, deletes included; we join back for current values.
  append_only      rows with ROWVERSION in [last watermark, MIN_ACTIVE_ROWVERSION()).
                   The upper bound skips rows of still-open transactions, which a
                   naive "id > max(id)" watermark would lose forever. Deletes are
                   not propagated: the warehouse keeps the full history.
A full snapshot is used for the first load, when Change Tracking history has
expired, when the source schema changed, and on request (--full-refresh).
"""
import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pyarrow as pa

from . import db
from .tables import TABLES

ARROW_TYPES = {
    "int": pa.int32(), "bigint": pa.int64(), "smallint": pa.int16(), "tinyint": pa.int16(),
    "bit": pa.bool_(), "date": pa.date32(), "datetime2": pa.timestamp("us"),
    "char": pa.string(), "nchar": pa.string(), "varchar": pa.string(), "nvarchar": pa.string(),
}

META_DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.sync_state (
    table_name VARCHAR PRIMARY KEY, strategy VARCHAR, watermark BIGINT,
    schema_hash VARCHAR, synced_at TIMESTAMP, run_id VARCHAR);
CREATE TABLE IF NOT EXISTS meta.load_log (
    run_id VARCHAR, table_name VARCHAR, mode VARCHAR, reason VARCHAR,
    from_watermark BIGINT, to_watermark BIGINT, rows_read BIGINT, bytes_read BIGINT,
    inserts BIGINT, updates BIGINT, deletes BIGINT, seconds DOUBLE, loaded_at TIMESTAMP);
"""


@dataclass
class Batch:
    table: object
    columns: list
    data: pa.Table
    mode: str          # full | incremental | replay
    reason: str
    from_wm: int
    to_wm: int
    schema_hash: str
    seconds: float


def source_columns(cur, table):
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (table.name,))
    cols = []
    for name, dtype, precision, scale in cur.fetchall():
        if name in table.exclude:
            continue
        if dtype in ("decimal", "numeric"):
            arrow = pa.decimal128(precision, scale)
        elif dtype in ARROW_TYPES:
            arrow = ARROW_TYPES[dtype]
        else:
            raise SystemExit(f"{table.name}.{name}: no mapping for SQL Server type {dtype!r} (add it to ARROW_TYPES)")
        cols.append((name, dtype, arrow))
    return cols


def schema_hash(cols):
    return hashlib.sha1("|".join(f"{n}:{d}:{a}" for n, d, a in cols).encode()).hexdigest()[:12]


def extract(cur, table, cols, mode, since, upto):
    """Return rows as (col values..., is_deleted)."""
    pk, name = table.pk, table.name
    select = ", ".join(f"t.[{n}]" for n, _, _ in cols)
    if table.strategy == "change_tracking" and mode == "full":
        cur.execute(f"SELECT {select}, 0 FROM dbo.[{name}] t")
    elif table.strategy == "change_tracking":
        # Deleted rows come back with NULL columns from the join; take the key from CT.
        select = ", ".join(f"ct.[{n}]" if n == pk else f"t.[{n}]" for n, _, _ in cols)
        cur.execute(f"SELECT {select}, CASE WHEN t.[{pk}] IS NULL THEN 1 ELSE 0 END "
                    f"FROM CHANGETABLE(CHANGES dbo.[{name}], %s) ct "
                    f"LEFT JOIN dbo.[{name}] t ON t.[{pk}] = ct.[{pk}]", (since,))
    else:
        lower = "" if mode == "full" else "t.RowVer >= CONVERT(BINARY(8), CAST(%s AS BIGINT)) AND "
        params = (upto,) if mode == "full" else (since, upto)
        cur.execute(f"SELECT {select}, 0 FROM dbo.[{name}] t "
                    f"WHERE {lower}t.RowVer < CONVERT(BINARY(8), CAST(%s AS BIGINT))", params)
    return cur.fetchall()


def to_arrow(rows, cols):
    arrays = [pa.array([r[i] for r in rows], type=arrow) for i, (_, _, arrow) in enumerate(cols)]
    arrays.append(pa.array([bool(r[-1]) for r in rows], type=pa.bool_()))
    return pa.table(arrays, names=[n for n, _, _ in cols] + ["_deleted"])


def read_source(state, full_refresh, replay_last):
    """Everything we need from the source, read from one consistent snapshot."""
    src = db.source(autocommit=True)
    cur = src.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
    cur.execute("BEGIN TRANSACTION")
    cur.execute("SELECT CHANGE_TRACKING_CURRENT_VERSION(), CAST(MIN_ACTIVE_ROWVERSION() AS BIGINT)")
    ct_version, rv_high = cur.fetchone()
    batches = []
    for table in TABLES:
        t0 = time.time()
        cols = source_columns(cur, table)
        shash = schema_hash(cols)
        prev = state.get(table.name)
        since = replay_last.get(table.name, prev and prev["watermark"])
        if table.strategy == "change_tracking":
            upto = ct_version
            min_valid = db.scalar(cur, "SELECT CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID(%s))", (f"dbo.{table.name}",))
            reason = ("first load" if prev is None else
                      "requested" if table.name in full_refresh else
                      "source schema changed" if prev["schema_hash"] != shash else
                      "change tracking history expired" if since < min_valid else None)
        else:
            upto = rv_high
            if prev and prev["schema_hash"] != shash:
                raise SystemExit(f"{table.name}: schema changed on an append-only (archive) table; "
                                 "migrate the warehouse copy deliberately instead of reloading it")
            reason = "first load" if prev is None else "requested" if table.name in full_refresh else None
        mode = "full" if reason else "incremental"
        rows = extract(cur, table, cols, mode, since, upto)
        if mode == "incremental" and table.name in replay_last:
            mode = "replay"
        batches.append(Batch(table, cols, to_arrow(rows, cols), mode, reason or "", None if reason else since,
                             upto, shash, time.time() - t0))
    cur.execute("COMMIT")
    src.close()
    return batches


def apply(wh, b):
    """Apply one batch inside the caller's transaction. Returns (inserts, updates, deletes)."""
    t, names = b.table, [f'"{n}"' for n, _, _ in b.columns]
    target, pk = f'raw."{t.name}"', f'"{t.pk}"'
    cols = ", ".join(names)
    wh.register("stg", b.data)
    exists = wh.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'raw' "
                        "AND table_name = ?", [t.name]).fetchone()[0]
    if not exists:
        assert b.mode == "full", f"{t.name}: refusing to build a table from an incremental delta"
        wh.execute(f"CREATE TABLE {target} AS SELECT {cols} FROM stg")
        counts = (len(b.data), 0, 0)
    elif b.mode == "full" and t.strategy == "change_tracking":
        # Mirror tables are replaced wholesale (atomic: we are inside the run's transaction).
        # Count what the replacement changes, so a repair shows exactly what it fixed.
        old = {r[0] for r in wh.execute("SELECT column_name FROM information_schema.columns "
                                        "WHERE table_schema = 'raw' AND table_name = ?", [t.name]).fetchall()}
        changed = " OR ".join(f"s.{n} IS DISTINCT FROM t.{n}" for n in names if n.strip('"') in old) or "true"
        ins, upd, before = wh.execute(
            f"SELECT (SELECT count(*) FROM stg s WHERE NOT EXISTS (SELECT 1 FROM {target} t WHERE t.{pk} = s.{pk})), "
            f"(SELECT count(*) FROM stg s JOIN {target} t ON t.{pk} = s.{pk} WHERE {changed}), "
            f"(SELECT count(*) FROM {target})").fetchone()
        wh.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT {cols} FROM stg")
        counts = (ins, upd, before - (len(b.data) - ins))  # deletes include duplicate copies
    else:
        dups = 0
        if b.mode == "full":  # append-only full refresh keeps archived rows; only extra copies of a key go
            dups = wh.execute(f"SELECT count(*) - count(DISTINCT {pk}) FROM {target}").fetchone()[0]
            if dups:
                wh.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT DISTINCT ON ({pk}) * FROM {target}")
        counts = wh.execute(
            f"SELECT count(*) FILTER (WHERE NOT s._deleted AND t.{pk} IS NULL), "
            f"       count(*) FILTER (WHERE NOT s._deleted AND t.{pk} IS NOT NULL), "
            f"       count(*) FILTER (WHERE s._deleted AND t.{pk} IS NOT NULL) "
            f"FROM stg s LEFT JOIN {target} t ON t.{pk} = s.{pk}").fetchone()
        wh.execute(
            f"MERGE INTO {target} AS t USING stg AS s ON t.{pk} = s.{pk} "
            f"WHEN MATCHED AND s._deleted THEN DELETE "
            f"WHEN MATCHED THEN UPDATE SET {', '.join(f'{n} = s.{n}' for n in names)} "
            f"WHEN NOT MATCHED AND NOT s._deleted THEN INSERT ({cols}) VALUES ({', '.join(f's.{n}' for n in names)})")
        counts = (counts[0], counts[1], counts[2] + dups)
    wh.unregister("stg")
    return counts


def load_state(wh):
    wh.execute(META_DDL)
    # A watermark without its table (e.g. one dropped by hand) must mean "load from scratch".
    wh.execute("DELETE FROM meta.sync_state WHERE table_name NOT IN "
               "(SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw')")
    rows = wh.execute("SELECT table_name, watermark, schema_hash FROM meta.sync_state").fetchall()
    return {name: {"watermark": wm, "schema_hash": h} for name, wm, h in rows}


def run(full_refresh=(), crash_after=None, replay=False):
    started = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    wh = db.warehouse()
    state = load_state(wh)
    replay_last = {}
    if replay:  # re-read the last window that moved rows, although it was already applied
        replay_last = dict(wh.execute(
            "SELECT table_name, arg_max(from_watermark, loaded_at) FROM meta.load_log "
            "WHERE mode = 'incremental' AND rows_read > 0 GROUP BY table_name").fetchall())
    batches = read_source(state, set(full_refresh), replay_last)

    print(f"load {run_id}")
    print(f"  {'table':<22}{'mode':<13}{'watermark':>21}{'read':>9}{'bytes':>11}{'+ins':>8}{'~upd':>7}{'-del':>7}")
    wh.execute("BEGIN TRANSACTION")
    totals = [0, 0]
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # UTC, whatever the process time zone is
    for b in batches:
        t0 = time.time()
        ins, upd, dele = apply(wh, b)
        secs = b.seconds + time.time() - t0
        wh.execute("INSERT OR REPLACE INTO meta.sync_state VALUES (?, ?, ?, ?, ?, ?)",
                   [b.table.name, b.table.strategy, b.to_wm, b.schema_hash, now, run_id])
        wh.execute("INSERT INTO meta.load_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [run_id, b.table.name, b.mode, b.reason, b.from_wm, b.to_wm, len(b.data), b.data.nbytes,
                    ins, upd, dele, secs, now])
        wm = f"{b.from_wm}->{b.to_wm}" if b.from_wm is not None else f"->{b.to_wm}"
        note = f"  ({b.reason})" if b.reason and b.reason != "first load" else ""
        print(f"  {b.table.name:<22}{b.mode:<13}{wm:>21}{len(b.data):>9,}{fmt_bytes(b.data.nbytes):>11}"
              f"{ins:>8,}{upd:>7,}{dele:>7,}{note}")
        totals[0] += len(b.data)
        totals[1] += b.data.nbytes
        if crash_after == b.table.name:
            print(f"  !! simulated crash: process killed after applying {b.table.name}, before COMMIT")
            os._exit(137)
    wh.execute("COMMIT")
    print(f"  committed: {totals[0]:,} rows, {fmt_bytes(totals[1])} read from source in {time.time() - started:.1f}s")
    wh.close()
    return totals


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fingerprint(wh=None):
    """One hash over every replicated table's content, to show a replay changed nothing."""
    own = wh is None
    wh = wh or db.warehouse()
    parts = [wh.execute(f'SELECT md5(string_agg(md5(CAST(t AS VARCHAR)), \',\' ORDER BY "{t.pk}")) '
                        f'FROM raw."{t.name}" t').fetchone()[0] for t in TABLES]
    if own:
        wh.close()
    return hashlib.md5("".join(parts).encode()).hexdigest()
