"""Break the warehouse on purpose, four realistic ways, so the check has something to catch."""
from . import db


def run():
    db.require_reset_allowed("delete source rows and damage the warehouse")
    wh = db.warehouse()
    src = db.source()
    cur = src.cursor()
    version = db.scalar(cur, "SELECT CHANGE_TRACKING_CURRENT_VERSION()")
    loaded = wh.execute("SELECT watermark FROM meta.sync_state WHERE table_name = 'Transactions'").fetchone()[0]
    if loaded != version:
        raise SystemExit("the warehouse has unloaded source changes; run `make load` first so only the "
                         "planted breakage shows up")

    # 1. Missed deletes: the source deletes rows, and a buggy loader advances its
    #    watermark without applying them. The pipeline's own state now says "up to date".
    cur.execute("SELECT TOP 25 TransactionId FROM dbo.Transactions WHERE Status = 'failed' ORDER BY TransactionId DESC")
    voided = [r[0] for r in cur.fetchall()]
    cur.execute(f"DELETE FROM dbo.Transactions WHERE TransactionId IN ({', '.join(map(str, voided))})")
    src.commit()
    version = db.scalar(cur, "SELECT CHANGE_TRACKING_CURRENT_VERSION()")
    wh.execute("UPDATE meta.sync_state SET watermark = ? WHERE table_name = 'Transactions'", [version])
    print(f"  1. missed deletes: deleted {len(voided)} Transactions at the source, then advanced the warehouse "
          f"watermark to {version} without applying them")

    # 2. Lost batch: a contiguous range of rows never arrived (or was dropped).
    lo = int(wh.execute('SELECT median("TransactionId") FROM raw."Transactions"').fetchone()[0]) // 1000 * 1000
    n = wh.execute('DELETE FROM raw."Transactions" WHERE "TransactionId" BETWEEN ? AND ?', [lo, lo + 299]).fetchone()[0]
    print(f"  2. lost batch: removed {n} Transactions (keys {lo}-{lo + 299}) from the warehouse")

    # 3. Silent corruption: a value changed in the warehouse only.
    aid = wh.execute('SELECT min("AdvanceId") FROM raw."Advances" WHERE "Status" = \'paid_off\'').fetchone()[0]
    wh.execute('UPDATE raw."Advances" SET "Amount" = "Amount" + 100 WHERE "AdvanceId" = ?', [aid])
    print(f"  3. silent corruption: added 100.00 to the Amount of Advance {aid} in the warehouse")

    # 4. Double load: the same row appended twice (BigQuery would not stop it).
    card = wh.execute('SELECT max("CardId") FROM raw."Cards"').fetchone()[0]
    wh.execute('INSERT INTO raw."Cards" SELECT * FROM raw."Cards" WHERE "CardId" = ?', [card])
    print(f"  4. double load: Card {card} now appears twice in the warehouse")
    wh.close()
    src.close()
