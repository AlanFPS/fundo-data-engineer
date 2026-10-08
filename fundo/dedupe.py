"""Duplicate customers: find them, merge them reversibly, stop new ones.

  python -m fundo dedupe           dry run: writes reports/, changes nothing
  python -m fundo dedupe --apply   backup, then ONE transaction: exclude test accounts,
                                   merge proven duplicates, hold what we must not guess,
                                   log every changed value, add the constraint that
                                   rejects new duplicates
  python -m fundo dedupe --undo    reverse the last applied run from its change log

Merging never deletes. The merged-away customer row stays, marked
DedupeStatus='merged' with DuplicateOfCustomerId pointing at the survivor; its
advances and cards are re-pointed to the survivor. The identity rules live in
identity.py.
"""
import csv
import itertools
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .identity import QA_SSNS, address_key, classify_test, corroborated, email_status, fold, phone_status, valid_ssn
from .seed import TRUTH_PATH

REPORTS = Path(db.WAREHOUSE_PATH).parent.parent / "reports"
INDEX = "UX_Customers_Ssn_Active"
JUNK_SHARE = 5  # a contact value shared by more customers than this is junk, not evidence


@dataclass
class Customer:
    id: int
    first: str
    last: str
    name: str
    raw_email: str
    email: str      # normalised, or None when unusable as evidence
    email_status: str
    phone: str      # E.164, or None when unusable as evidence
    phone_status: str
    address: str    # normalised street + ZIP5, or None
    dob: object
    ssn: str        # raw value as stored
    created: datetime
    status: str     # DedupeStatus: None = active
    funded: bool    # has a funded or paid-off advance: untouchable
    advances: int
    fingerprints: frozenset


def load(cur):
    has_cols = db.scalar(cur, "SELECT COL_LENGTH('dbo.Customers', 'DedupeStatus')") is not None
    cur.execute("SELECT TRY_CAST(CustomerId AS INT), Fingerprint FROM dbo.Cards WHERE Status <> 'removed'")
    fps = defaultdict(set)
    for cid, fp in cur.fetchall():
        fps[cid].add(fp)
    cur.execute(f"""
        SELECT c.CustomerId, c.FirstName, c.LastName, c.Email, c.Phone, c.AddressLine1, c.PostalCode,
               c.DateOfBirth, c.Ssn, c.CreatedAt,
               {'c.DedupeStatus' if has_cols else 'NULL'},
               (SELECT COUNT(*) FROM dbo.Advances a WHERE a.CustomerId = c.CustomerId AND a.Status IN ('funded', 'paid_off')),
               (SELECT COUNT(*) FROM dbo.Advances a WHERE a.CustomerId = c.CustomerId)
        FROM dbo.Customers c ORDER BY c.CustomerId""")
    out = []
    for cid, first, last, email, phone, street, zip_, dob, ssn, created, status, funded, n_adv in cur.fetchall():
        e, es = email_status(email)
        p, ps = phone_status(phone)
        out.append(Customer(cid, fold(first), fold(last), f"{first} {last}", email,
                            e if es in ("valid", "valid_normalised", "suspect_typo") else None, es,
                            p if ps in ("valid", "valid_normalised") else None, ps, address_key(street, zip_),
                            dob, ssn, created, status, funded > 0, n_adv, frozenset(fps.get(cid, ()))))
    return out


def primary(group):
    """Who keeps the account: a funded/paid-off customer, else one with any advance, else the oldest."""
    return min(group, key=lambda c: (not c.funded, c.advances == 0, c.created, c.id))


def components(group):
    """Connected components of a same-SSN+DOB group, linked by shared contact details."""
    parent = {c.id: c.id for c in group}

    def root(x):
        while parent[x] != x:
            x = parent[x]
        return x
    for a, b in itertools.combinations(group, 2):
        if corroborated(vars(a), vars(b)):
            parent[root(a.id)] = root(b.id)
    comps = defaultdict(list)
    for c in group:
        comps[root(c.id)].append(c)
    return list(comps.values())


def plan(customers):
    decisions = {}            # customer id -> (action, target id, reason)
    test_review, active = [], []
    for c in customers:
        if c.status:          # decided by an earlier run (or a human): leave it alone
            continue
        verdict, why = classify_test(c.first, c.last, c.raw_email, c.ssn)
        if verdict == "test":
            decisions[c.id] = ("exclude_test", None, why)
            continue
        if verdict == "review":
            test_review.append((c, why))
        active.append(c)

    # Proof: same valid SSN and date of birth, corroborated by one more detail.
    merges, double_funded = [], []
    proven = defaultdict(list)
    for c in active:
        if valid_ssn(c.ssn) and c.dob:
            proven[(c.ssn, c.dob)].append(c)
    for group in (g for g in proven.values() if len(g) > 1):
        for comp in components(group):
            if len(comp) < 2:
                continue
            if sum(c.funded for c in comp) > 1:
                double_funded.append(comp)  # rule: two untouchable customers -> do not guess
                continue
            survivor = primary(comp)
            rule = "survivor has a funded/paid-off advance" if survivor.funded else "survivor is the oldest account with the most history"
            for c in comp:
                if c is not survivor:
                    decisions[c.id] = ("merge_into", survivor.id, f"same SSN+DOB and a shared contact detail; {rule}")
                    merges.append((survivor, c))

    # Any SSN still shared by two active customers is held: all but one account.
    remaining = defaultdict(list)
    for c in active:
        if c.id not in decisions and c.ssn:
            remaining[c.ssn].append(c)
    twins = {c.id for comp in double_funded for c in comp}  # proven same person as a funded customer
    holds = []
    for group in (g for g in remaining.values() if len(g) > 1):
        keep = primary(group)
        for c in group:
            if c is keep:
                continue
            if c.id in twins and keep.id in twins:
                why = ("proven same person, but both accounts have funded/paid-off advances" if c.funded else
                       "proven same person as two funded accounts: merge after those are resolved")
                link = keep.id
            elif c.dob == keep.dob:
                why, link = "same SSN+DOB but no shared email, phone, card or address: keying error or stolen identity", None
            else:
                why, link = "same SSN, different date of birth", None
            decisions[c.id] = ("hold", link, why)
            holds.append((keep, c, why))

    # Suggestive evidence only: queue for a human, unless different SSNs disprove it.
    same_person = {cid: t for cid, (_, t, _) in decisions.items() if t}  # merged, or held next to a proven twin
    person = lambda c: same_person.get(c.id, c.id)
    pool = [c for c in active if decisions.get(c.id, ("",))[0] != "exclude_test"]
    keys = defaultdict(list)
    for c in pool:
        if c.email:
            keys[("same email", c.email)].append(c)
        if c.phone:
            keys[("same phone", c.phone)].append(c)
        if c.dob:
            keys[("same name and DOB", c.first, c.last, c.dob)].append(c)
        for fp in c.fingerprints:
            keys[("same card", fp)].append(c)
    pairs, distinct, junk = defaultdict(set), defaultdict(set), Counter()
    for key, group in keys.items():
        if len(group) > JUNK_SHARE:
            junk[key[0]] += 1
            continue
        for a, b in itertools.combinations(sorted(group, key=lambda c: c.id), 2):
            if person(a) == person(b):
                continue
            if valid_ssn(a.ssn) and valid_ssn(b.ssn) and a.ssn != b.ssn:
                distinct[(a.id, b.id)].add(key[0])
            else:
                pairs[(a.id, b.id)].add(key[0])
    return dict(decisions=decisions, merges=merges, holds=holds, double_funded=double_funded,
                review=pairs, distinct=distinct, junk=junk, test_review=test_review, considered=len(active))


def score(p, customers):
    """Compare the plan with what the seed planted. Only meaningful on seeded data."""
    if not TRUTH_PATH.exists():
        return []
    exists = {c.id for c in customers}  # simulated days purge some abandoned signups
    truth = {k: [g for g in groups if all(i in exists for i in g)]
             for k, groups in json.loads(TRUTH_PATH.read_text()).items()}
    d = p["decisions"]
    action = lambda cid: d.get(cid, ("keep",))[0]
    target = lambda cid: d[cid][1] if action(cid) == "merge_into" else cid
    pair = lambda a, b: (min(a, b), max(a, b))
    merged_pair = lambda a, b: target(a) == target(b)
    lines = []

    def line(label, ok, total):
        lines.append(f"  {label:<52}{ok:>4}/{total:<4}{'ok' if ok == total else 'CHECK'}")
    groups = truth.get("true_duplicate", [])
    line("true duplicate groups merged into one survivor", sum(len({target(i) for i in g}) == 1 for g in groups), len(groups))
    df = truth.get("double_funded", [])
    line("groups with two funded customers held, not merged",
         sum(all(action(i) in ("keep", "hold") for i in g) and any(action(i) == "hold" for i in g) for g in df), len(df))
    traps = [g for k, v in truth.items() if k.startswith("trap_") for g in v]
    line("trap pairs (shared phone/email/name/number) not merged", sum(not merged_pair(*g) for g in traps), len(traps))
    sd = truth.get("ssn_dob_without_corroboration", [])
    line("SSN+DOB matches with nothing else in common held", sum(any(action(i) == "hold" for i in g) for g in sd), len(sd))
    si = truth.get("stolen_identity", [])
    line("SSN+DOB+name but no shared contact (stolen ID) held", sum(any(action(i) == "hold" for i in g) for g in si), len(si))
    pk = truth.get("pre_kyc_duplicate", [])
    line("pre-KYC duplicates sent to review, not merged", sum(pair(*g) in p["review"] and not merged_pair(*g) for g in pk), len(pk))
    tests = [g[0] for g in truth.get("test_account", [])]
    line("test accounts excluded", sum(action(i) == "exclude_test" for i in tests), len(tests))
    amb = [g[0] for g in truth.get("test_ambiguous", [])]
    review_ids = {c.id for c, _ in p["test_review"]}
    line("ambiguous test-looking accounts sent to a human", sum(i in review_ids for i in amb), len(amb))
    real = [g[0] for g in truth.get("real_lookalike", [])]
    line("real 'Testerman'/staff/@fundo.com customers kept", sum(action(i) == "keep" for i in real), len(real))
    return lines


def contact_counts(customers):
    return Counter(c.email_status for c in customers), Counter(c.phone_status for c in customers)


def write_reports(p, customers, run_id):
    REPORTS.mkdir(exist_ok=True)
    by_id = {c.id: c for c in customers}
    with open(REPORTS / "dedupe_decisions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "customer_id", "name", "action", "target_customer_id", "reason"])
        for cid, (action, target, reason) in sorted(p["decisions"].items()):
            w.writerow([run_id, cid, by_id[cid].name, action, target, reason])
    with open(REPORTS / "review_pairs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["customer_a", "name_a", "customer_b", "name_b", "evidence", "ssn_a_present", "ssn_b_present"])
        for (a, b), why in sorted(p["review"].items()):
            w.writerow([a, by_id[a].name, b, by_id[b].name, "; ".join(sorted(why)), bool(by_id[a].ssn), bool(by_id[b].ssn)])
        for c, why in p["test_review"]:
            w.writerow([c.id, c.name, "", "", f"possible test account: {why}", bool(c.ssn), ""])
    with open(REPORTS / "contact_issues.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["customer_id", "email_status", "phone_status"])
        for c in customers:
            if c.email_status not in ("valid", "valid_normalised") or c.phone_status not in ("valid", "valid_normalised"):
                w.writerow([c.id, c.email_status, c.phone_status])


def summarize(p, customers, header):
    d = p["decisions"]
    acts = Counter(a for a, _, _ in d.values())
    survivors = {s.id: s for s, _ in p["merges"]}
    funded_surv = sum(s.funded for s in survivors.values())
    test_reasons = Counter(r.split(" (")[0] for a, _, r in d.values() if a == "exclude_test")
    hold_reasons = Counter(r for a, _, r in d.values() if a == "hold")
    lookalike = [c for c in customers if not c.status and d.get(c.id, ("",))[0] != "exclude_test"
                 and "test" in (c.name + " " + (c.raw_email or "")).lower()
                 and c.id not in {x.id for x, _ in p["test_review"]}]
    print(header)
    print(f"  active customers considered            {p['considered']:>6,}")
    print(f"  test accounts excluded (not merged)    {acts['exclude_test']:>6,}   " +
          ", ".join(f"{r}: {n}" for r, n in test_reasons.most_common()))
    print(f"  test-looking, sent to a human          {len(p['test_review']):>6,}   " +
          "; ".join(f"{c.name} <{c.raw_email}>" for c, _ in p["test_review"]))
    print(f"  contain 'test' but kept as real        {len(lookalike):>6,}   " +
          "; ".join(f"{c.name} <{c.raw_email}>" for c in lookalike[:6]))
    print(f"  accounts merged                        {acts['merge_into']:>6,}   into {len(survivors)} survivors "
          f"({funded_surv} with a funded/paid-off advance)")
    print(f"  accounts held for a human              {acts['hold']:>6,}   " +
          "; ".join(f"{n}x {r}" for r, n in hold_reasons.most_common()))
    print(f"  pairs for review (suggestive only)     {len(p['review']):>6,}   " +
          ", ".join(f"{k}: {n}" for k, n in Counter(e for v in p["review"].values() for e in v).most_common()))
    print(f"  shared contact, different SSNs         {len(p['distinct']):>6,}   (families, couples, recycled numbers: "
          f"different people, not queued)")
    if p["junk"]:
        print(f"  values shared by >{JUNK_SHARE} customers ignored   " +
              ", ".join(f"{k}: {n}" for k, n in p["junk"].items()))
    emails, phones = contact_counts(customers)
    print("  contacts (all customers; flagged, never rewritten):")
    print("    email  " + "  ".join(f"{k} {v:,}" for k, v in emails.most_common()))
    print("    phone  " + "  ".join(f"{k} {v:,}" for k, v in phones.most_common()))
    lines = score(p, customers)
    if lines:
        print(f"  scored against the planted truth in {TRUTH_PATH.name}:")
        print("\n".join(lines))


def change(cur, log, table, key_col, key, **new):
    """Update one row, logging old and new values of each changed column. UpdatedAt is bumped, not logged."""
    cols = list(new)
    cur.execute(f"SELECT {', '.join(cols)} FROM dbo.{table} WITH (UPDLOCK) WHERE {key_col} = %s", (key,))
    old = dict(zip(cols, cur.fetchone()))
    diff = {c: v for c, v in new.items() if as_text(old[c]) != as_text(v)}
    if diff:
        cur.execute(f"UPDATE dbo.{table} SET {', '.join(f'{c} = %s' for c in diff)}, UpdatedAt = SYSUTCDATETIME() "
                    f"WHERE {key_col} = %s", (*diff.values(), key))
        log.extend((table, key_col, key, c, as_text(old[c]), as_text(v)) for c, v in diff.items())


def as_text(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, datetime):
        return v.isoformat(timespec="milliseconds")
    return str(v)


def still_valid(cur, action, cid, target):
    """The plan was read without locks: re-check it under lock inside the apply transaction."""
    ids = [cid, target] if action == "merge_into" else [cid]
    active = db.scalar(cur, "SELECT COUNT(*) FROM dbo.Customers WITH (UPDLOCK, HOLDLOCK) WHERE DedupeStatus IS NULL "
                            f"AND CustomerId IN ({', '.join(['%s'] * len(ids))})", tuple(ids))
    if active != len(ids):
        return False
    if action == "merge_into":  # funded since the plan? then it is untouchable now
        return not db.scalar(cur, "SELECT COUNT(*) FROM dbo.Advances WITH (UPDLOCK, HOLDLOCK) "
                                  "WHERE CustomerId = %s AND Status IN ('funded', 'paid_off')", (cid,))
    return True


def move_cards(cur, log, cid, target):
    """Cards follow the person. The survivor keeps its default card; if it has no usable one,
    the loser's active card becomes the default, so repayments always have a card to use.
    Owner ids are NVARCHAR(MAX) text with no index, so these reads lock a range of Cards."""
    cur.execute("SELECT CardId, Fingerprint, IsDefault, Status FROM dbo.Cards WITH (UPDLOCK, HOLDLOCK) "
                "WHERE TRY_CAST(CustomerId AS INT) = %s", (target,))
    survivor_cards = cur.fetchall()
    kept = {fp for _, fp, _, status in survivor_cards if status == "active"}
    has_default = any(default and status == "active" for _, _, default, status in survivor_cards)
    cur.execute("SELECT CardId, Fingerprint, Status FROM dbo.Cards WITH (UPDLOCK, HOLDLOCK) "
                "WHERE TRY_CAST(CustomerId AS INT) = %s ORDER BY IsDefault DESC, CreatedAt DESC", (cid,))
    for card_id, fp, status in cur.fetchall():
        dup_card = status == "active" and fp in kept  # same physical card already on the survivor
        promote = not has_default and status == "active" and not dup_card
        if promote:  # an expired default would otherwise compete with the new one
            for old_id, _, default, old_status in survivor_cards:
                if default and old_status != "active":
                    change(cur, log, "Cards", "CardId", old_id, IsDefault=False)
            has_default = True
        change(cur, log, "Cards", "CardId", card_id, CustomerId=str(target), IsDefault=promote,
               Status="removed" if dup_card else status)


def apply(p, run_id):
    conn = db.source(autocommit=True)
    cur = conn.cursor()
    backup = f"/var/opt/mssql/data/{db.SOURCE_DB}_before_dedupe_{run_id}.bak"
    cur.execute(f"BACKUP DATABASE {db.SOURCE_DB} TO DISK = N'{backup}' WITH COPY_ONLY, INIT")
    while cur.nextset():  # BACKUP reports progress as result sets
        pass
    print(f"  backup before any change: {backup} (inside the SQL Server volume; `make down` deletes it)")
    db.run_script(conn, db.SQL_DIR / "dedupe_migration.sql")
    now = db.scalar(cur, "SELECT CAST(SYSUTCDATETIME() AS DATETIME2(3))")
    log, skipped = [], []
    cur.execute("BEGIN TRANSACTION")
    try:
        for cid, (action, target, reason) in sorted(p["decisions"].items()):
            if not still_valid(cur, action, cid, target):
                skipped.append(cid)
                continue
            if action == "exclude_test":
                change(cur, log, "Customers", "CustomerId", cid, DedupeStatus="test")
            elif action == "hold":
                change(cur, log, "Customers", "CustomerId", cid, DedupeStatus="hold", DuplicateOfCustomerId=target)
            elif action == "merge_into":
                move_cards(cur, log, cid, target)
                cur.execute("SELECT AdvanceId FROM dbo.Advances WITH (UPDLOCK, HOLDLOCK) WHERE CustomerId = %s", (cid,))
                for (aid,) in cur.fetchall():
                    change(cur, log, "Advances", "AdvanceId", aid, CustomerId=target)
                change(cur, log, "Customers", "CustomerId", cid, DedupeStatus="merged", DuplicateOfCustomerId=target)
            cur.execute("INSERT INTO ops.DedupeDecision VALUES (%s, %s, %s, %s, %s)", (run_id, cid, action, target, reason))
        for i in range(0, len(log), 500):
            db.insert_rows(cur, "ops.DedupeChange", ["RunId", "TableName", "KeyColumn", "KeyValue", "ColumnName",
                                                     "OldValue", "NewValue"], [(run_id, *row) for row in log[i:i + 500]])
        new_index = db.scalar(cur, "SELECT COUNT(*) FROM sys.indexes WHERE name = %s", (INDEX,)) == 0
        if new_index:  # the identity proof field is unique among active customers from now on
            sandbox = "".join(f" AND Ssn <> '{s}'" for s in sorted(QA_SSNS))  # QA keeps reusing these
            cur.execute(f"CREATE UNIQUE INDEX {INDEX} ON dbo.Customers (Ssn) "
                        f"WHERE Ssn IS NOT NULL{sandbox} AND DedupeStatus IS NULL")
        acts = Counter(a for c, (a, _, _) in p["decisions"].items() if c not in skipped)
        cur.execute("INSERT INTO ops.DedupeRun VALUES (%s, %s, %s, %s, NULL, %s)",
                    (run_id, now, backup, new_index, json.dumps(acts)))
        cur.execute("COMMIT")
    except Exception:
        cur.execute("IF @@TRANCOUNT > 0 ROLLBACK")
        raise
    if skipped:
        print(f"  skipped {len(skipped)} decisions whose customers changed since the plan (re-run to re-plan): {skipped[:10]}")
    print(f"  committed run {run_id}: {len(log):,} column values changed and logged in ops.DedupeChange"
          + (f"; created unique index {INDEX}" if new_index else ""))
    conn.close()


def survivors_without_default_card(cur):
    """Survivors that received an active card in a merge but have no active default card (should be 0)."""
    return db.scalar(cur, """
        SELECT COUNT(DISTINCT TRY_CAST(ch.NewValue AS INT)) FROM ops.DedupeChange ch
        JOIN dbo.Cards k ON k.CardId = ch.KeyValue AND k.Status = 'active'
        WHERE ch.TableName = 'Cards' AND ch.ColumnName = 'CustomerId'
          AND NOT EXISTS (SELECT 1 FROM dbo.Cards d WHERE TRY_CAST(d.CustomerId AS INT) = TRY_CAST(ch.NewValue AS INT)
                          AND d.Status = 'active' AND d.IsDefault = 1)""")


def undo():
    conn = db.source(autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT TOP 1 RunId, CreatedIndex FROM ops.DedupeRun WHERE UndoneAt IS NULL ORDER BY AppliedAt DESC")
    row = cur.fetchone()
    if not row:
        raise SystemExit("nothing to undo")
    run_id, created_index = row
    cur.execute("SELECT TableName, KeyColumn, KeyValue, ColumnName, OldValue, NewValue FROM ops.DedupeChange "
                "WHERE RunId = %s AND ColumnName <> 'UpdatedAt' ORDER BY ChangeId DESC", (run_id,))
    changes = cur.fetchall()
    conflicts, gone = [], 0
    cur.execute("BEGIN TRANSACTION")
    try:
        if created_index:
            cur.execute(f"DROP INDEX {INDEX} ON dbo.Customers")
        for table, key_col, key, col, old, new in changes:
            cur.execute(f"SELECT {col} FROM dbo.{table} WITH (UPDLOCK) WHERE {key_col} = %s", (key,))
            row = cur.fetchone()
            if row is None:  # deleted after the merge (e.g. a purged advance): nothing to restore
                gone += 1
                continue
            if as_text(row[0]) != new:  # someone changed it after the merge: do not clobber their edit
                conflicts.append(f"{table}.{col} {key_col}={key}: expected {new!r}, found {as_text(row[0])!r}")
                continue
            cur.execute(f"UPDATE dbo.{table} SET {col} = %s, UpdatedAt = SYSUTCDATETIME() WHERE {key_col} = %s", (old, key))
        if conflicts:
            raise SystemExit("undo aborted, nothing changed; rows edited since the merge:\n  " + "\n  ".join(conflicts[:20]))
        cur.execute("UPDATE ops.DedupeRun SET UndoneAt = SYSUTCDATETIME() WHERE RunId = %s", (run_id,))
        cur.execute("COMMIT")
    except BaseException:
        cur.execute("IF @@TRANCOUNT > 0 ROLLBACK")
        raise
    print(f"undid dedupe run {run_id}: restored {len(changes) - gone:,} column values"
          + (f" ({gone} belonged to rows deleted since)" if gone else "") + (f", dropped {INDEX}" if created_index else ""))
    conn.close()


def run(apply_changes=False):
    conn = db.source()
    customers = load(conn.cursor())
    conn.close()
    p = plan(customers)
    run_id = datetime.now(timezone.utc).strftime("dedupe-%Y%m%dT%H%M%S")
    write_reports(p, customers, run_id)
    summarize(p, customers, "dedupe " + ("(applying)" if apply_changes else "plan (dry run: nothing changed; --apply to execute)"))
    print(f"  reports: {', '.join(f'reports/{f}' for f in ('dedupe_decisions.csv', 'review_pairs.csv', 'contact_issues.csv'))}")
    if apply_changes:
        if not p["decisions"]:
            print("  nothing to apply")
            return
        apply(p, run_id)
