"""One simulated business day on the source: about 1% of rows change.

Inserts, updates and deletes touch disjoint rows (nothing is inserted and then
updated on the same day), so the counts printed here are exactly what the next
incremental load should report. The whole day commits as one transaction.
"""
import random
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

import pymssql

from . import db
from .seed import CUSTOMER_COLS, TXN_COLS, Seeder, fmt_phone


def ids(cur, sql, n, r):
    cur.execute(sql)
    rows = [row[0] for row in cur.fetchall()]
    return r.sample(rows, min(n, len(rows)))


def in_list(values):
    return ", ".join(str(int(v)) for v in values) or "NULL"


def run():
    conn = db.source()
    cur = conn.cursor()
    r = random.Random(db.scalar(cur, "SELECT MAX(TransactionId) FROM dbo.Transactions"))
    now = db.scalar(cur, "SELECT CAST(SYSUTCDATETIME() AS DATETIME2(3))")
    merged = db.scalar(cur, "SELECT COL_LENGTH('dbo.Customers', 'DedupeStatus')") is not None
    active = "c.DedupeStatus IS NULL" if merged else "1 = 1"
    n = defaultdict(lambda: [0, 0, 0])  # table -> [inserts, updates, deletes]
    notes = []

    # ---- Customers --------------------------------------------------------
    gen = Seeder(seed=r.randrange(10**9))
    new = []
    for _ in range(15):
        p = gen.person(kyc=r.random() < 0.7)
        new.append((p["first"], p["last"], p["email"], fmt_phone(p["phone"], r.randrange(4)), p["dob"], p["ssn"],
                    p["address"], p["city"], p["state"], p["zip"], now, now))
    db.insert_rows(cur, "dbo.Customers", CUSTOMER_COLS[1:], new)
    n["Customers"][0] += len(new)
    # Two existing customers sign up again with a new email: the duplicate problem, live.
    cur.execute(f"SELECT TOP 2 c.CustomerId, c.FirstName, c.LastName, c.DateOfBirth, c.Ssn, c.Phone FROM dbo.Customers c "
                f"WHERE c.Ssn IS NOT NULL AND {active} ORDER BY CHECKSUM(c.CustomerId, {r.randrange(1, 10**6)})")
    for cid, first, last, dob, ssn, phone in cur.fetchall():
        row = (first, last, gen.new_email(first, last), phone, dob, ssn, None, None, None, None, now, now)
        try:
            db.insert_rows(cur, "dbo.Customers", CUSTOMER_COLS[1:], [row])
            n["Customers"][0] += 1
            notes.append(f"customer {cid} signed up again as a new account (duplicate created)")
        except pymssql.IntegrityError:
            notes.append(f"customer {cid} tried to sign up again: rejected by UX_Customers_Ssn_Active")
    upd = ids(cur, f"SELECT c.CustomerId FROM dbo.Customers c WHERE {active} AND c.CreatedAt < '{now}'", 40, r)
    for cid in upd:
        p = gen.person()
        col, val = r.choice([("Phone", fmt_phone(p["phone"], r.randrange(4))), ("Email", p["email"]),
                             ("AddressLine1", p["address"])])
        cur.execute(f"UPDATE dbo.Customers SET {col} = %s, UpdatedAt = %s WHERE CustomerId = %s", (val, now, cid))
    n["Customers"][1] += len(upd)
    # Abandoned signups with nothing attached get purged.
    gone = ids(cur, f"SELECT c.CustomerId FROM dbo.Customers c WHERE {active} AND c.CreatedAt < '{now}' "
                    "AND NOT EXISTS (SELECT 1 FROM dbo.Advances a WHERE a.CustomerId = c.CustomerId) "
                    "AND NOT EXISTS (SELECT 1 FROM dbo.Cards k WHERE TRY_CAST(k.CustomerId AS INT) = c.CustomerId) "
                    + ("AND NOT EXISTS (SELECT 1 FROM dbo.Customers d WHERE d.DuplicateOfCustomerId = c.CustomerId) "
                       if merged else "")  # a merge survivor is not abandoned
                    + f"AND c.CustomerId NOT IN ({in_list(upd)})", 3, r)
    cur.execute(f"DELETE FROM dbo.Customers WHERE CustomerId IN ({in_list(gone)})")
    n["Customers"][2] += len(gone)

    # ---- Advances, their history, and the money they move ----------------
    txns = []

    def txn(aid, card, kind, amount, status, settled=None):
        txns.append((aid, card, kind, amount, status, now, settled))

    def transition(aids, old, new, **extra):
        for aid in aids:
            sets = ", ".join(f"{k} = %s" for k in extra)
            cur.execute(f"UPDATE dbo.Advances SET Status = %s, UpdatedAt = %s{', ' + sets if sets else ''} "
                        f"WHERE AdvanceId = %s", (new, now, *extra.values(), aid))
            cur.execute("INSERT INTO dbo.AdvanceStatusHistory (AdvanceId, FromStatus, ToStatus, ChangedAt, ChangedBy) "
                        "VALUES (%s, %s, %s, %s, %s)", (aid, old, new, now, "simulator"))
        n["Advances"][1] += len(aids)
        n["AdvanceStatusHistory"][0] += len(aids)

    def pick(status, k):
        return ids(cur, f"SELECT AdvanceId FROM dbo.Advances WHERE Status = '{status}' AND UpdatedAt < '{now}'", k, r)

    def card_of(aid):
        return db.scalar(cur, "SELECT TOP 1 CardId FROM dbo.Transactions WHERE AdvanceId = %s AND CardId IS NOT NULL", (aid,))

    transition(pick("requested", 25), "requested", "approved")
    transition(pick("requested", 5), "requested", "declined")
    funded = pick("approved", 20)
    transition(funded, "approved", "funded", FundedAt=now)
    cur.execute(f"SELECT AdvanceId, Amount, Fee FROM dbo.Advances WHERE AdvanceId IN ({in_list(funded)})")
    for aid, amount, fee in cur.fetchall():
        txn(aid, None, "disbursement", amount, "settled", now)
        txn(aid, None, "fee", fee, "settled", now)
    paid = pick("funded", 40)
    transition(paid, "funded", "paid_off", PaidOffAt=now)
    for aid in paid:
        txn(aid, card_of(aid), "repayment", Decimal(r.choice(["10.00", "25.00", "40.00"])), "settled", now)
    for aid in pick("funded", 300):  # today's auto-debit attempts on open advances
        for _ in range(r.choice([1, 2, 3])):
            txn(aid, card_of(aid), "repayment", Decimal(r.choice(["10.00", "15.00", "25.00"])),
                r.choices(["settled", "failed", "pending"], [60, 30, 10])[0])
    cur.execute(f"SELECT TOP 30 c.CustomerId FROM dbo.Customers c WHERE {active} AND c.Ssn IS NOT NULL "
                f"AND EXISTS (SELECT 1 FROM dbo.Advances a WHERE a.CustomerId = c.CustomerId AND a.Status = 'paid_off') "
                f"ORDER BY CHECKSUM(c.CustomerId, {r.randrange(1, 10**6)})")
    for (cid,) in cur.fetchall():
        cur.execute("INSERT INTO dbo.Advances (CustomerId, Amount, Fee, Status, RequestedAt, UpdatedAt) "
                    "OUTPUT INSERTED.AdvanceId VALUES (%s, %s, %s, 'requested', %s, %s)",
                    (cid, Decimal(r.choice([100, 150, 200, 300])), Decimal("4.99"), now, now))
        aid = cur.fetchone()[0]
        cur.execute("INSERT INTO dbo.AdvanceStatusHistory (AdvanceId, FromStatus, ToStatus, ChangedAt, ChangedBy) "
                    "VALUES (%s, NULL, 'requested', %s, 'app')", (aid, now))
        n["Advances"][0] += 1
        n["AdvanceStatusHistory"][0] += 1
    gone = ids(cur, f"SELECT AdvanceId FROM dbo.Advances a WHERE Status IN ('cancelled', 'declined') AND UpdatedAt < '{now}' "
                    "AND NOT EXISTS (SELECT 1 FROM dbo.Transactions t WHERE t.AdvanceId = a.AdvanceId)", 2, r)
    cur.execute(f"DELETE FROM dbo.Advances WHERE AdvanceId IN ({in_list(gone)})")
    n["Advances"][2] += len(gone)

    # ---- Transactions: mostly inserts, a few late changes to old rows -----
    settled = ids(cur, "SELECT TransactionId FROM dbo.Transactions WHERE Status = 'pending'", 350, r)
    cur.execute(f"UPDATE dbo.Transactions SET Status = 'settled', SettledAt = %s "
                f"WHERE TransactionId IN ({in_list(settled)})", (now,))
    reversed_ = ids(cur, "SELECT TransactionId FROM dbo.Transactions WHERE Status = 'settled' AND Type = 'repayment' "
                         f"AND CreatedAt < '{now - timedelta(days=30)}'", 20, r)
    cur.execute(f"UPDATE dbo.Transactions SET Status = 'failed', SettledAt = NULL "
                f"WHERE TransactionId IN ({in_list(reversed_)})")  # chargebacks: no timestamp moves
    voided = ids(cur, "SELECT TransactionId FROM dbo.Transactions WHERE Status = 'failed' "
                      f"AND TransactionId NOT IN ({in_list(reversed_)})", 50, r)
    cur.execute(f"DELETE FROM dbo.Transactions WHERE TransactionId IN ({in_list(voided)})")
    db.insert_rows(cur, "dbo.Transactions", TXN_COLS[1:], txns)
    n["Transactions"] = [len(txns), len(settled) + len(reversed_), len(voided)]

    # ---- Cards ------------------------------------------------------------
    cur.execute(f"SELECT TOP 15 c.CustomerId FROM dbo.Customers c WHERE {active} AND c.Ssn IS NOT NULL "
                f"ORDER BY CHECKSUM(c.CustomerId, {r.randrange(1, 10**6)})")
    for (cid,) in cur.fetchall():
        cur.execute("INSERT INTO dbo.Cards (CustomerId, Brand, Last4, Fingerprint, ExpMonth, ExpYear, IsDefault, "
                    "Status, CreatedAt, UpdatedAt) VALUES (%s, 'visa', %s, %s, %s, %s, 0, 'active', %s, %s)",
                    (str(cid), f"{r.randrange(10000):04d}", f"{r.getrandbits(64):016x}", r.randrange(1, 13),
                     now.year + r.randrange(1, 5), now, now))
        n["Cards"][0] += 1
    removed = ids(cur, f"SELECT CardId FROM dbo.Cards WHERE Status = 'active' AND IsDefault = 0 AND CreatedAt < '{now}'", 20, r)
    cur.execute(f"UPDATE dbo.Cards SET Status = 'removed', UpdatedAt = %s WHERE CardId IN ({in_list(removed)})", (now,))
    n["Cards"][1] += len(removed)
    gone = ids(cur, f"SELECT CardId FROM dbo.Cards k WHERE CreatedAt < '{now}' AND CardId NOT IN ({in_list(removed)}) "
                    "AND NOT EXISTS (SELECT 1 FROM dbo.Transactions t WHERE t.CardId = k.CardId)", 5, r)
    cur.execute(f"DELETE FROM dbo.Cards WHERE CardId IN ({in_list(gone)})")
    n["Cards"][2] += len(gone)

    conn.commit()
    sizes = dict(db.table_rows(cur))
    print(f"simulated day at {now:%Y-%m-%d %H:%M}Z")
    print(f"  {'table':<22}{'+ins':>7}{'~upd':>7}{'-del':>7}{'rows':>10}{'changed':>9}")
    for table in ("Customers", "Advances", "Cards", "Transactions", "AdvanceStatusHistory"):
        i, u, d = n[table]
        print(f"  {table:<22}{i:>7,}{u:>7,}{d:>7,}{sizes[table]:>10,}{(i + u + d) / sizes[table]:>9.2%}")
    for note in notes:
        print(f"  note: {note}")
    conn.close()
