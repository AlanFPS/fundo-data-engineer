"""Measurements: source sizes, Change Tracking overhead, and what each load moved."""
from . import db
from .sync import fmt_bytes
from .tables import TABLES


def run():
    src = db.source()
    cur = src.cursor()
    cur.execute("SELECT t.name, SUM(CASE WHEN p.index_id IN (0, 1) THEN p.row_count ELSE 0 END), "
                "SUM(p.used_page_count) * 8192 FROM sys.tables t JOIN sys.dm_db_partition_stats p "
                "ON p.object_id = t.object_id WHERE t.schema_id = SCHEMA_ID('dbo') AND t.is_ms_shipped = 0 "
                "GROUP BY t.name ORDER BY 3 DESC")
    sizes = cur.fetchall()
    cur.execute("SELECT OBJECT_NAME(it.parent_object_id), SUM(p.used_page_count) * 8192, SUM(p.row_count) "
                "FROM sys.internal_tables it JOIN sys.dm_db_partition_stats p ON p.object_id = it.object_id "
                "WHERE it.internal_type_desc = 'CHANGE_TRACKING' GROUP BY it.parent_object_id")
    ct = {name: (b, n) for name, b, n in cur.fetchall()}
    src.close()
    print("source tables (SQL Server, measured)")
    print(f"  {'table':<28}{'rows':>10}{'data+index':>12}   change tracking side table")
    for name, rows, size in sizes:
        side = f"{fmt_bytes(ct[name][0])} ({ct[name][1]:,} rows)" if name in ct else "-"
        print(f"  {name:<28}{rows:>10,}{fmt_bytes(size):>12}   {side}")

    wh = db.warehouse()
    rows = wh.execute("""
        SELECT table_name,
               arg_min(rows_read, loaded_at) FILTER (WHERE mode = 'full' AND reason = 'first load'),
               arg_min(bytes_read, loaded_at) FILTER (WHERE mode = 'full' AND reason = 'first load'),
               avg(rows_read) FILTER (WHERE mode = 'incremental' AND rows_read > 0),
               avg(bytes_read) FILTER (WHERE mode = 'incremental' AND rows_read > 0),
               count(*) FILTER (WHERE mode = 'incremental' AND rows_read > 0)
        FROM meta.load_log GROUP BY table_name""").fetchall()
    wh.close()
    order = {t.name: i for i, t in enumerate(TABLES)}
    print("loads (warehouse meta.load_log, measured; bytes = extracted Arrow size)")
    print(f"  {'table':<22}{'full rows':>11}{'full bytes':>12}{'incr rows':>11}{'incr bytes':>12}{'bytes vs full':>15}")
    tot = [0, 0, 0, 0]
    for name, fr, fb, ir, ib, n in sorted(rows, key=lambda r: order.get(r[0], 99)):
        if fb is None or ib is None:
            continue
        tot = [tot[0] + fr, tot[1] + fb, tot[2] + ir, tot[3] + ib]
        print(f"  {name:<22}{fr:>11,}{fmt_bytes(fb):>12}{ir:>11,.0f}{fmt_bytes(ib):>12}{ib / fb:>15.2%}")
    if tot[1]:
        print(f"  {'all tables':<22}{tot[0]:>11,}{fmt_bytes(tot[1]):>12}{tot[2]:>11,.0f}{fmt_bytes(tot[3]):>12}"
              f"{tot[3] / tot[1]:>15.2%}   (incremental = average of runs that moved rows)")
