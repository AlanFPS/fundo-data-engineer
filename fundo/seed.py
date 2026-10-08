"""Deterministic synthetic source data.

Every case the dedupe and the checks care about is planted on purpose. The
planted ground truth is written to data/seed_truth.json so the dedupe can be
scored against it instead of eyeballed.
"""
import json
import random
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import db

ANCHOR = datetime(2026, 10, 1)  # "now" for the seeded history
TRUTH_PATH = Path(db.WAREHOUSE_PATH).parent / "seed_truth.json"

FIRST = ["María", "José", "Juan", "Ana", "Luis", "Carlos", "Rosa", "Jorge", "Carmen", "Miguel",
         "Sofía", "Pedro", "Lucía", "Diego", "Elena", "Javier", "Isabel", "Alejandro", "Gabriela",
         "Fernando", "Daniela", "Ricardo", "Patricia", "Manuel", "Adriana", "Roberto", "Valeria",
         "Andrés", "Mónica", "Héctor", "James", "Mary", "Robert", "Jennifer", "Michael", "Linda",
         "David", "Sarah", "William", "Jessica", "Kevin", "Ashley", "Brian", "Emily", "Jason",
         "Nicole", "Anthony", "Michelle", "Eric", "Stephanie", "Tyler", "Brittany", "Marcus",
         "Tiffany", "Andre", "Keisha", "Darnell", "Latoya", "Minh", "Priya"]
LAST = ["García", "Rodríguez", "Martínez", "Hernández", "López", "González", "Pérez", "Sánchez",
        "Ramírez", "Torres", "Flores", "Rivera", "Gómez", "Díaz", "Cruz", "Morales", "Reyes",
        "Gutiérrez", "Ortiz", "Ramos", "Castillo", "Mendoza", "Vargas", "Romero", "Herrera",
        "Medina", "Aguilar", "Jiménez", "Muñoz", "Peña", "Núñez", "Ibáñez", "Castro", "Vega",
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Anderson",
        "Thomas", "Taylor", "Moore", "Jackson", "White", "Harris", "Clark", "Lewis", "Walker",
        "Young", "Nguyen", "Tran", "Patel", "Kim"]
NICKNAMES = {"Robert": "Bob", "William": "Bill", "Michael": "Mike", "Jennifer": "Jen",
             "Alejandro": "Alex", "José": "Pepe", "Jessica": "Jess", "Anthony": "Tony",
             "Daniela": "Dani", "Patricia": "Paty", "Javier": "Javi", "Fernando": "Nando"}
CITIES = [("Miami", "FL", "331", ["305", "786"]), ("Hialeah", "FL", "330", ["305"]),
          ("Houston", "TX", "770", ["713", "832"]), ("Dallas", "TX", "752", ["214", "469"]),
          ("San Antonio", "TX", "782", ["210"]), ("Los Angeles", "CA", "900", ["213", "323"]),
          ("Phoenix", "AZ", "850", ["602"]), ("Chicago", "IL", "606", ["312", "773"]),
          ("Bronx", "NY", "104", ["718", "347"]), ("Orlando", "FL", "328", ["407"])]
STREETS = ["Main St", "Oak Ave", "Palm Dr", "Flagler St", "Bissonnet St", "Calle Ocho", "Elm St",
           "Cedar Ln", "Sunset Blvd", "Grand Ave", "Bird Rd", "Westheimer Rd"]
DOMAINS = ["gmail.com"] * 5 + ["yahoo.com", "hotmail.com", "outlook.com", "icloud.com"]

CUSTOMER_COLS = ["CustomerId", "FirstName", "LastName", "Email", "Phone", "DateOfBirth", "Ssn",
                 "AddressLine1", "City", "State", "PostalCode", "CreatedAt", "UpdatedAt"]
ADVANCE_COLS = ["AdvanceId", "CustomerId", "Amount", "Fee", "Status", "RequestedAt", "FundedAt",
                "PaidOffAt", "UpdatedAt"]
CARD_COLS = ["CardId", "CustomerId", "Brand", "Last4", "Fingerprint", "ExpMonth", "ExpYear",
             "IsDefault", "Status", "CreatedAt", "UpdatedAt"]
TXN_COLS = ["TransactionId", "AdvanceId", "CardId", "Type", "Amount", "Status", "CreatedAt", "SettledAt"]
HISTORY_COLS = ["AdvanceId", "FromStatus", "ToStatus", "ChangedAt", "ChangedBy"]


def ascii_fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def fmt_phone(digits, style):
    a, b, c = digits[:3], digits[3:6], digits[6:]
    return [f"({a}) {b}-{c}", f"{a}-{b}-{c}", digits, f"+1 {a} {b} {c}", f"{a}.{b}.{c}", f"1{digits}"][style]


class Seeder:
    def __init__(self, seed=42):
        self.r = random.Random(seed)
        self.specs = []  # customer specs; ids are assigned after sorting by CreatedAt
        self.ssns, self.emails, self.phones, self.fps = set(), set(), set(), {}
        self.truth = defaultdict(list)
        self.advances, self.cards, self.txns, self.history = [], [], [], []

    # ---- identities -------------------------------------------------------
    def new_ssn(self):
        r = self.r
        while True:
            if r.random() < 0.05:  # ITINs start with 9 and are real identifiers, not junk
                ssn = f"9{r.randrange(100):02d}{r.choice([70, 75, 78, 80, 84, 88])}{r.randrange(1, 10000):04d}"
            else:
                area = r.randrange(1, 900)
                if area == 666:
                    continue
                ssn = f"{area:03d}{r.randrange(1, 100):02d}{r.randrange(1, 10000):04d}"
            if ssn not in self.ssns:
                self.ssns.add(ssn)
                return ssn

    def new_email(self, first, last):
        r = self.r
        f, l = ascii_fold(first).lower(), ascii_fold(last).lower()
        while True:
            local = r.choice([f"{f}.{l}", f"{f}{l}", f"{f}_{l}", f"{f[0]}{l}", f"{f}.{l}{r.randrange(10, 99)}",
                              f"{f}{r.randrange(1970, 2005)}"])
            email = f"{local}@{r.choice(DOMAINS)}"
            if email not in self.emails:
                self.emails.add(email)
                return email

    def new_phone(self, areas):
        r = self.r
        while True:
            exch = r.randrange(201, 999)
            if exch % 100 == 11 or exch == 555:
                continue
            digits = f"{r.choice(areas)}{exch}{r.randrange(10000):04d}"
            if digits not in self.phones:
                self.phones.add(digits)
                return digits

    def person(self, first=None, last=None, kyc=True):
        r = self.r
        city, state, zip3, areas = r.choice(CITIES)
        first, last = first or r.choice(FIRST), last or r.choice(LAST)
        return dict(first=first, last=last,
                    dob=date(1958, 1, 1) + timedelta(days=r.randrange(365 * 47)) if kyc else None,
                    ssn=self.new_ssn() if kyc else None,
                    email=self.new_email(first, last), phone=self.new_phone(areas),
                    address=f"{r.randrange(100, 9999)} {r.choice(STREETS)}",
                    city=city, state=state, zip=zip3 + f"{r.randrange(100):02d}")

    def spec(self, p, profile, created=None, phone_style=None, **extra):
        r = self.r
        created = created or ANCHOR - timedelta(days=r.uniform(2, 730))
        s = dict(p, profile=profile, created=created,
                 phone_style=r.choices(range(6), [40, 30, 15, 8, 5, 2])[0] if phone_style is None else phone_style,
                 **extra)
        self.specs.append(s)
        return s

    # ---- scenarios --------------------------------------------------------
    def build_specs(self):
        r = self.r
        base = []
        for _ in range(5000):
            kyc = r.random() < 0.9
            profile = r.choices(["borrower", "kyc_only"], [91, 9])[0] if kyc else "none"
            base.append(self.spec(self.person(kyc=kyc), profile))
        kyc_base = [s for s in base if s["ssn"]]
        r.shuffle(kyc_base)
        pool = iter(kyc_base)

        # True duplicates: same person signed up again. SSN and date of birth repeat;
        # contact details and spelling drift the way they do on real re-signups.
        for i in range(110):
            if i < 3:  # both accounts borrowed: the business rule says do not guess
                original = next(o for o in pool if o["profile"] == "borrower" and o["created"] < ANCHOR - timedelta(days=300))
                original["profile"] = "borrower_paid"
            else:
                original = next(pool)
            group = [original]
            for _ in range(2 if r.random() < 0.15 else 1):
                dup = {k: original[k] for k in ("first", "last", "dob", "ssn", "address", "city", "state", "zip")}
                roll = r.random()
                if roll < 0.2:
                    dup["first"] = ascii_fold(dup["first"]) if dup["first"] != ascii_fold(dup["first"]) else dup["first"].upper()
                elif roll < 0.3:
                    dup["first"] = NICKNAMES.get(dup["first"], dup["first"])
                elif roll < 0.4:
                    dup["last"] = r.choice(LAST)  # married name: first name and identity stay
                dup["email"] = original["email"].upper() if r.random() < 0.3 else self.new_email(dup["first"], dup["last"])
                dup["phone"] = original["phone"] if r.random() < 0.6 else self.new_phone(["305", "713", "214"])
                created = original["created"] + timedelta(days=r.uniform(1, 400))
                created = min(created, ANCHOR - timedelta(hours=r.uniform(1, 48)))
                if i < 3:
                    created = original["created"] + timedelta(days=r.uniform(20, 60))
                    profile, extra = "borrower_paid", {}
                else:
                    profile = r.choices(["none", "declined", "card_only"], [50, 30, 20])[0]
                    extra = {"share_card_of": original} if profile == "card_only" and r.random() < 0.4 else {}
                group.append(self.spec(dup, profile, created=created, **extra))
            self.truth["double_funded" if i < 3 else "true_duplicate"].append(group)

        # Duplicates that never finished KYC: only contact details repeat.
        for _ in range(25):
            p = self.person(kyc=False)
            a = self.spec(p, "none")
            b = self.spec(dict(p, email=p["email"].capitalize()), "none",
                          created=a["created"] + timedelta(days=r.uniform(0.01, 30)), phone_style=(a["phone_style"] + 1) % 6)
            self.truth["pre_kyc_duplicate"].append([a, b])

        # Traps: share a field that only *suggests* identity. None of these may merge.
        for _ in range(12):  # family on one phone plan, same address
            a = self.spec(self.person(), "borrower")
            p = self.person(last=a["last"])
            p.update(phone=a["phone"], address=a["address"], city=a["city"], state=a["state"], zip=a["zip"])
            self.truth["trap_shared_phone_family"].append([a, self.spec(p, r.choice(["borrower", "kyc_only"]))])
        for _ in range(6):  # couple sharing one inbox
            a = self.spec(self.person(), "borrower")
            p = self.person(last=a["last"] if r.random() < 0.5 else None)
            p["email"] = a["email"]
            self.truth["trap_shared_email_couple"].append([a, self.spec(p, "borrower")])
        for i, (f, l) in enumerate([("María", "García"), ("José", "Hernández"), ("Juan", "Rodríguez"),
                                    ("Ana", "Martínez"), ("Luis", "González"), ("Carlos", "López"),
                                    ("Rosa", "Pérez"), ("Jorge", "Sánchez"), ("Carmen", "Ramírez"),
                                    ("Miguel", "Torres")]):
            a = self.spec(self.person(f, l), "borrower")
            p = self.person(f, l)
            if i < 2:
                p["dob"] = a["dob"]  # same name AND same birthday, still two people
            self.truth["trap_common_name"].append([a, self.spec(p, "borrower")])
        for _ in range(6):  # carrier recycled the number to a new subscriber
            a = self.spec(self.person(), "borrower", created=ANCHOR - timedelta(days=r.uniform(600, 720)))
            p = self.person()
            p["phone"] = a["phone"]
            self.truth["trap_recycled_phone"].append(
                [a, self.spec(p, "borrower", created=ANCHOR - timedelta(days=r.uniform(30, 200)))])
        for _ in range(2):  # same SSN + DOB, nothing else in common: keying error or identity fraud
            a = self.spec(self.person(), "borrower")
            p = self.person()
            p.update(ssn=a["ssn"], dob=a["dob"])
            self.truth["ssn_dob_without_corroboration"].append([a, self.spec(p, "kyc_only")])
        for _ in range(2):  # stolen identity: the victim's SSN, DOB and name, the impostor's own contacts
            a = self.spec(self.person(), "borrower")
            p = self.person(first=a["first"], last=a["last"])
            p.update(ssn=a["ssn"], dob=a["dob"])
            self.truth["stolen_identity"].append([a, self.spec(p, "declined")])

        # Test accounts, including a "duplicate group" of QA accounts sharing a fake SSN.
        qa_ssn, qa_dob = "123456789", date(1990, 1, 1)
        for first, last, email, ssn, profile in [
            ("Test", "User", "test@fundo.com", qa_ssn, "borrower"),
            ("Test", "Account", "test1@fundo.com", qa_ssn, "borrower"),
            ("Test", "Account", "test2@fundo.com", qa_ssn, "kyc_only"),
            ("QA", "Automation", "qa.automation@fundo.com", None, "none"),
            ("María", "Prueba", "qa+signup1@fundo.com", None, "none"),
            ("María", "Prueba", "qa+signup2@fundo.com", None, "none"),
            ("Demo", "User", "demo@fundo.com", None, "card_only"),
            ("Dummy", "Customer", "dummy3@fundo.com", None, "none"),
            ("John", "Doe", "john.doe@example.com", qa_ssn, "borrower"),
            ("Jane", "Doe", "jane.doe@example.com", None, "none"),
            ("Test", "Card", "test.card@example.org", None, "card_only"),
            ("Carlos", "López", "carlos@test.example", None, "none"),
            ("Pedro", "Pruebas", "pp1@fundo.com", qa_ssn, "kyc_only"),
            ("Ana", "Torres", "ana.torres@example.net", None, "none"),
        ]:
            p = self.person(first, last, kyc=False)
            p.update(email=email, ssn=ssn, dob=qa_dob if ssn else None)
            self.truth["test_account"].append([self.spec(p, profile)])
        # Ambiguous: test-looking, but not provably ours. These go to a human, not the bin.
        for first, last, email, kyc, profile in [
            ("Test", "Test", "test+kyc@gmail.com", False, "none"),
            ("Luis", "Ortiz", "luis.ortiz@mailinator.com", True, "borrower"),
            ("Load", "Test", "loadtest42@fundo.com", False, "none"),
        ]:
            p = self.person(first, last, kyc=kyc)
            p["email"] = email
            self.truth["test_ambiguous"].append([self.spec(p, profile)])
        # Real people who trip a naive LIKE '%test%' or "@fundo.com means test" rule.
        for first, last, email in [("Karen", "Testerman", "karen.testerman@gmail.com"),
                                   ("Dale", "Testerman", "dtesterman@yahoo.com"),
                                   ("Marco", "Testa", "marco.testa@hotmail.com"),
                                   ("Luis", "Contreras", "contestwinner88@gmail.com"),
                                   ("Ana", "Rodríguez", "ana.rodriguez@fundo.com"),
                                   ("Kevin", "Walker", "kevin.walker@fundo.com"),
                                   ("Priya", "Patel", "ppatel@fundo.com"),
                                   ("Jorge", "Medina", "jorge.medina@fundo.com"),
                                   ("Tiffany", "Young", "tyoung@fundo.com"),
                                   ("Héctor", "Ramos", "hector.ramos@fundo.com")]:
            p = self.person(first, last)
            p["email"] = email
            self.truth["real_lookalike"].append([self.spec(p, "borrower")])

        # Malformed contacts on ordinary customers (identity scenarios stay clean).
        bad_phones = ["305-555-013", "1234", "000-000-0000", "(305) 555-01345", "N/A", "", "305555O134",
                      "9999999999", "+52 55 1234 5678"]
        bad_emails = ["{n}@gmail", "{n}@@gmail.com", "{f} {l}@gmail.com", "{n}.gmail.com", "{n}@gmial.com",
                      "{n}@gmail.con", "{n}@hotmial.com", "{n}@yahooo.com", "none@none.com", "n/a"]
        planted = {id(m) for groups in self.truth.values() for grp in groups for m in grp}
        clean = [s for s in base if id(s) not in planted]
        for s in r.sample(clean, 150):
            s["phone_raw"] = r.choice(bad_phones)
        for s in r.sample(clean, 110):
            f, l = ascii_fold(s["first"]).lower(), ascii_fold(s["last"]).lower()
            s["email"] = r.choice(bad_emails).format(f=f, l=l, n=f"{f}.{l}")

    # ---- activity ---------------------------------------------------------
    def assign_ids(self):
        self.specs.sort(key=lambda s: s["created"])
        for i, s in enumerate(self.specs, start=1):
            s["id"] = i

    def new_fingerprint(self):
        fp = "".join(self.r.choice("0123456789abcdef") for _ in range(16))
        self.fps[fp] = f"{self.r.randrange(10000):04d}"
        return fp

    def add_card(self, s, created, fingerprint=None, is_default=True):
        r = self.r
        fp = fingerprint or self.new_fingerprint()
        exp_year, exp_month = created.year + r.randrange(1, 6), r.randrange(1, 13)
        status = "expired" if date(exp_year, exp_month, 1) < ANCHOR.date() else "active"
        ref = str(s["id"])
        if r.random() < 0.03:  # legacy import formats in the NVARCHAR(MAX) column
            ref = r.choice([f" {ref}", f"{int(ref):06d}", f"{ref} "])
        card_id = len(self.cards) + 1
        self.cards.append((card_id, ref, r.choice(["visa", "visa", "mastercard", "discover"]), self.fps[fp], fp,
                           exp_month, exp_year, is_default, status, created, created))
        s.setdefault("card_ids", []).append(card_id)
        s.setdefault("fps", []).append(fp)
        return card_id

    def add_advance(self, s, requested, status):
        r = self.r
        aid = len(self.advances) + 1
        amount = Decimal(r.choice([50, 75, 100, 150, 200, 250, 300, 400, 500]))
        fee = Decimal(r.choice(["1.99", "2.99", "4.99", "5.99", "8.99"]))
        card_id = s.get("card_ids", [None])[0]
        hist, t = [(None, "requested", requested, "app")], requested
        funded = paid = None
        if status in ("declined", "cancelled"):
            t += timedelta(minutes=r.uniform(1, 600))
            hist.append(("requested", status, t, "underwriting-svc" if status == "declined" else "app"))
        if status in ("approved", "funded", "paid_off"):
            t += timedelta(minutes=r.uniform(1, 300))
            hist.append(("requested", "approved", t, "underwriting-svc"))
        if status in ("funded", "paid_off"):
            t = funded = t + timedelta(minutes=r.uniform(1, 120))
            hist.append(("approved", "funded", t, "payments-svc"))
        if status == "paid_off":
            t = paid = min(funded + timedelta(days=r.uniform(7, 60)), ANCHOR - timedelta(hours=1))
            hist.append(("funded", "paid_off", t, "payments-svc"))
        self.advances.append((aid, s["id"], amount, fee, status, requested, funded, paid, t))
        self.history.extend((aid, *h) for h in hist)
        if funded:
            self.add_transactions(aid, card_id, amount, fee, funded, paid)
        return t

    def add_transactions(self, aid, card_id, amount, fee, funded, paid):
        r = self.r

        def txn(kind, amt, status, created, settled=None):
            self.txns.append((len(self.txns) + 1, aid, card_id, kind, amt, status, created, settled))

        txn("disbursement", amount, "settled", funded, funded + timedelta(hours=r.uniform(0.1, 30)))
        txn("fee", fee, "settled", funded, funded + timedelta(hours=r.uniform(0.1, 30)))
        total, k = amount + fee, r.randrange(4, 13)
        part = (total / k).quantize(Decimal("0.01"))
        end = paid or max(funded + timedelta(days=r.uniform(14, 60)), ANCHOR + timedelta(days=r.uniform(3, 30)))
        for i in range(k):
            due = funded + (end - funded) * (i + 1) / k
            if due > ANCHOR:
                break
            for _ in range(r.randrange(1, 4) if r.random() < 0.3 else 0):  # auto-debit retries
                txn("repayment", part, "failed", due - timedelta(hours=r.uniform(1, 48)))
            amt = part if i < k - 1 else total - part * (k - 1)
            if paid or due < ANCHOR - timedelta(days=3):
                txn("repayment", amt, "settled", due, due + timedelta(hours=r.uniform(1, 72)))
            else:
                txn("repayment", amt, "pending", due)
        if r.random() < 0.02:
            txn("refund", -fee, "settled", end, end + timedelta(hours=2))

    def build_activity(self):
        r = self.r
        for s in self.specs:
            prof, created = s["profile"], s["created"]
            if prof in ("borrower", "borrower_paid"):
                self.add_card(s, created + timedelta(minutes=r.uniform(5, 600)))
                if r.random() < 0.15:
                    self.add_card(s, created + timedelta(days=r.uniform(1, 200)), is_default=False)
                t = created + timedelta(days=r.uniform(0.1, 10))
                for n in range(r.choice([1, 1, 2, 2, 3, 3, 4, 5, 6])):
                    if t > ANCHOR - timedelta(days=1):
                        break
                    last = t + timedelta(days=60) > ANCHOR
                    if prof == "borrower_paid":
                        status = "paid_off"
                    elif last or n == 5:
                        status = r.choices(["funded", "paid_off", "requested", "approved", "declined"], [35, 40, 10, 5, 10])[0]
                    else:
                        status = r.choices(["paid_off", "declined", "cancelled"], [85, 10, 5])[0]
                    t = self.add_advance(s, t, status) + timedelta(days=r.uniform(3, 40))
            elif prof == "kyc_only" and r.random() < 0.4:
                self.add_card(s, created + timedelta(days=r.uniform(0, 5)))
            elif prof == "declined":
                if r.random() < 0.5:
                    self.add_card(s, created + timedelta(minutes=r.uniform(5, 60)))
                self.add_advance(s, created + timedelta(hours=r.uniform(1, 72)), r.choice(["declined", "cancelled"]))
            elif prof == "card_only":
                shared = s.get("share_card_of", {}).get("fps")
                self.add_card(s, created + timedelta(minutes=r.uniform(5, 600)), fingerprint=shared[0] if shared else None)
        for i, ref in enumerate(["99999", "N/A", "", "-1", "12a4"]):  # cards whose owner cannot be resolved
            self.cards.append((len(self.cards) + 1, ref, "visa", "0000", f"{i:016d}", 1, 2027, True, "active",
                               ANCHOR - timedelta(days=400), ANCHOR - timedelta(days=400)))

    def customer_rows(self):
        r = self.r
        rows = []
        for s in self.specs:
            phone = s.get("phone_raw", fmt_phone(s["phone"], s["phone_style"]))
            updated = s["created"] + (ANCHOR - s["created"]) * r.random() * 0.5
            rows.append((s["id"], s["first"], s["last"], s["email"], phone, s["dob"], s["ssn"], s["address"],
                         s["city"], s["state"], s["zip"], s["created"], updated))
        return rows


def create_database():
    master = db.source(database="master", autocommit=True)
    cur = master.cursor()
    cur.execute(f"IF DB_ID('{db.SOURCE_DB}') IS NOT NULL BEGIN "
                f"ALTER DATABASE {db.SOURCE_DB} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                f"DROP DATABASE {db.SOURCE_DB}; END")
    cur.execute(f"CREATE DATABASE {db.SOURCE_DB}")
    master.close()


def run():
    db.require_reset_allowed(f"drop and re-create database {db.SOURCE_DB}")
    t0 = time.time()
    for stale in (Path(db.WAREHOUSE_PATH), Path(db.WAREHOUSE_PATH + ".wal")):  # a new source invalidates the warehouse
        stale.unlink(missing_ok=True)
    s = Seeder()
    s.build_specs()
    s.assign_ids()
    s.build_activity()

    create_database()
    conn = db.source(autocommit=True)
    db.run_script(conn, db.SQL_DIR / "source_schema.sql")
    cur = conn.cursor()
    tables = [("dbo.Customers", CUSTOMER_COLS, s.customer_rows(), True),
              ("dbo.Advances", ADVANCE_COLS, s.advances, True),
              ("dbo.Cards", CARD_COLS, s.cards, True),
              ("dbo.Transactions", TXN_COLS, s.txns, True),
              ("dbo.AdvanceStatusHistory", HISTORY_COLS, sorted(s.history, key=lambda h: h[3]), False)]
    for table, cols, rows, identity in tables:
        db.insert_rows(cur, table, cols, rows, identity_insert=identity)
        print(f"  {table:<28} {len(rows):>8,} rows")
    scratch = [(sp["id"], sp["email"], fmt_phone(sp["phone"], 1), datetime(2021, 6, 14, 9, 30), "promo list v2")
               for sp in s.specs[:2000]]
    db.insert_rows(cur, "dbo.tmp_CustomerExport_2021", ["Id", "Email", "Phone", "ExportedAt", "Notes"], scratch)
    print(f"  {'dbo.tmp_CustomerExport_2021':<28} {len(scratch):>8,} rows (scratch, not replicated)")
    db.run_script(conn, db.SQL_DIR / "enable_change_tracking.sql")
    conn.close()

    truth = {k: [[m["id"] for m in grp] for grp in groups] for k, groups in s.truth.items()}
    TRUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRUTH_PATH.write_text(json.dumps(truth, indent=1))
    print(f"Seeded source database '{db.SOURCE_DB}' in {time.time() - t0:.0f}s; "
          f"planted scenarios written to {TRUTH_PATH}")
