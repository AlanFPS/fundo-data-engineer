"""Connections to the source (SQL Server) and the warehouse (DuckDB)."""
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pymssql

SOURCE_DB = "fundo"
WAREHOUSE_PATH = os.environ.get("WAREHOUSE_PATH", "data/warehouse.duckdb")
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# Filtered and unique indexes require these; FreeTDS does not turn them all on.
_SESSION_OPTIONS = (
    "SET ANSI_NULLS ON; SET QUOTED_IDENTIFIER ON; SET ANSI_WARNINGS ON; "
    "SET ANSI_PADDING ON; SET CONCAT_NULL_YIELDS_NULL ON; SET ARITHABORT ON; "
    "SET NUMERIC_ROUNDABORT OFF;"
)


def source(database=SOURCE_DB, autocommit=False, wait_seconds=120):
    deadline = time.time() + wait_seconds
    while True:  # the port opens before SQL Server accepts logins, so retry for a while
        try:
            conn = pymssql.connect(
                server=os.environ.get("MSSQL_HOST", "localhost"),
                port=os.environ.get("MSSQL_PORT", "1433"),
                user="sa",
                password=os.environ["MSSQL_SA_PASSWORD"],
                database=database,
                tds_version="7.4",  # 7.3+ is needed to receive DATETIME2 as datetime, not text
                autocommit=autocommit,
                login_timeout=10,
            )
            break
        except pymssql.OperationalError as e:
            if time.time() > deadline or "18456" in str(e):  # login failed: retrying won't help
                raise
            time.sleep(2)
    cur = conn.cursor()
    cur.execute(_SESSION_OPTIONS)
    if not autocommit:
        conn.commit()
    return conn


def require_reset_allowed(what):
    """Destructive commands only run where the compose stack says the data is disposable."""
    if os.environ.get("FUNDO_ALLOW_RESET") != "1":
        raise SystemExit(f"refusing to {what}: FUNDO_ALLOW_RESET=1 is set only for the local compose stack "
                         f"(MSSQL_HOST={os.environ.get('MSSQL_HOST', 'localhost')})")


def warehouse(path=WAREHOUSE_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(path)


def scalar(cur, sql, params=None):
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def table_rows(cur, schema="dbo"):
    """(table, rows) for every table in a schema, from partition metadata (no scans)."""
    cur.execute("SELECT t.name, SUM(p.row_count) FROM sys.tables t JOIN sys.dm_db_partition_stats p "
                "ON p.object_id = t.object_id AND p.index_id IN (0, 1) WHERE t.schema_id = SCHEMA_ID(%s) "
                "AND t.is_ms_shipped = 0 GROUP BY t.name", (schema,))  # skip e.g. MSchange_tracking_history
    return cur.fetchall()


def run_script(conn, path):
    """Run a T-SQL file one ';'-terminated statement at a time."""
    text = Path(path).read_text()
    text = re.sub(r"--[^\n]*", "", text)
    cur = conn.cursor()
    for stmt in [s.strip() for s in text.split(";")]:
        if stmt:
            cur.execute(stmt)


def sql_literal(value):
    """Render a Python value as a T-SQL literal, for multi-row INSERT batches."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return "'" + value.isoformat(timespec="milliseconds") + "'"
    if isinstance(value, date):
        return "'" + value.isoformat() + "'"
    return "N'" + str(value).replace("'", "''") + "'"


def insert_rows(cur, table, columns, rows, identity_insert=False, batch=1000):
    """Insert rows with multi-row VALUES statements (SQL Server caps a VALUES list at 1000)."""
    cols = ", ".join(f"[{c}]" for c in columns)
    if identity_insert:
        cur.execute(f"SET IDENTITY_INSERT {table} ON")
    for i in range(0, len(rows), batch):
        values = ",\n".join(
            "(" + ", ".join(sql_literal(v) for v in row) + ")" for row in rows[i : i + batch]
        )
        cur.execute(f"INSERT INTO {table} ({cols}) VALUES {values}")
    if identity_insert:
        cur.execute(f"SET IDENTITY_INSERT {table} OFF")
