"""Identity rules: what proves two records are one person, what only suggests it.

PROVES     SSN + date of birth, both captured at KYC and verified by the KYC vendor,
           plus one shared contact detail: email, phone, card or postal address.
           Names don't count as corroboration: someone using a stolen SSN and DOB
           passes KYC with the victim's name too, but rarely with the victim's
           inbox, phone, card or address. Without a shared contact detail the
           match is held for a human (keying error or stolen identity).
SUGGESTS   email, phone, name + date of birth, card fingerprint. Families share
           phone plans and inboxes, carriers recycle numbers, names repeat, and
           households share cards. These never merge anything on their own.
DISPROVES  two different valid SSNs. Records sharing a phone or inbox but carrying
           different verified SSNs are different people; they are not even queued.

Contacts are normalised only where it is lossless ("(305) 555-0134" ->
"+13055550134", case and whitespace in emails). Anything that would be a guess
("gmial.com" -> "gmail.com") is flagged, never rewritten: a wrong guess sends
someone's financial notices to a stranger.
"""
import re
import unicodedata

PLACEHOLDER_SSNS = {"123456789", "987654321", "012345678"}
QA_SSNS = {"123456789"}  # the KYC sandbox's test SSN: confirm the list with QA
OUR_DOMAINS = {"fundo.com"}
TEST_WORDS = {"test", "qa", "demo", "dummy", "fake"}
DISPOSABLE_DOMAINS = {"mailinator.com", "guerrillamail.com", "yopmail.com", "10minutemail.com"}
RESERVED_DOMAIN = re.compile(r"(^|\.)(example\.(com|net|org)|example|test|invalid|localhost)$")  # RFC 2606 / 6761
TYPO_DOMAINS = {"gmial.com": "gmail.com", "gmail.con": "gmail.com", "gmal.com": "gmail.com",
                "hotmial.com": "hotmail.com", "hotmail.con": "hotmail.com", "yahooo.com": "yahoo.com",
                "yaho.com": "yahoo.com", "outlok.com": "outlook.com", "iclod.com": "icloud.com"}
PLACEHOLDER_EMAILS = {"none@none.com", "n/a", "na@na.com", "noemail@noemail.com", "no@email.com"}
EMAIL = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")


def valid_ssn(ssn):
    """Structurally valid SSN or ITIN (ITINs start with 9 and are real identifiers)."""
    if not ssn or not re.fullmatch(r"\d{9}", ssn):
        return False
    if len(set(ssn)) == 1 or ssn in PLACEHOLDER_SSNS:
        return False
    return ssn[:3] not in ("000", "666") and ssn[3:5] != "00" and ssn[5:] != "0000"


def fold(name):
    """'José ' -> 'jose': case, accents and punctuation do not distinguish people."""
    s = unicodedata.normalize("NFKD", name or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def email_status(raw):
    """-> (normalised email or None, status). Usable for matching: valid, suspect_typo."""
    e = (raw or "").strip().lower()
    if not e:
        return None, "missing"
    if e in PLACEHOLDER_EMAILS:
        return None, "placeholder"
    if not EMAIL.match(e):
        return None, "invalid"
    if e.split("@")[1] in TYPO_DOMAINS:
        return e, "suspect_typo"
    return e, "valid" if e == raw else "valid_normalised"


def phone_status(raw):
    """-> (E.164 or None, status). Usable for matching: valid, valid_normalised."""
    p = (raw or "").strip()
    if not p or p.lower() in ("n/a", "na", "none"):
        return None, "missing"
    if re.search(r"[a-z]", p, re.I):
        return None, "invalid"
    digits = re.sub(r"\D", "", p)
    if p.startswith("+") and not p.startswith("+1"):
        return "+" + digits, "non_us"
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return None, "invalid"
    if len(set(digits)) == 1 or digits[3:6] == "555" and digits[6:8] == "01":
        return None, "placeholder"
    if digits[0] in "01" or digits[3] in "01":
        return None, "invalid"
    e164 = "+1" + digits
    return e164, "valid" if p == e164 else "valid_normalised"


def classify_test(first, last, email, ssn):
    """-> ('test' | 'review' | 'real', reason).

    'test' needs evidence the account is ours or cannot be a person: a reserved
    domain that cannot receive mail, our own domain with a QA mailbox name, or the
    KYC sandbox SSN. A real SSN that passed KYC outweighs the email rules: real
    people type throwaway addresses, so those go to a human instead. Substrings
    never count: 'Testerman' is a surname, and staff with @fundo.com addresses run
    genuine transactions.
    """
    e = (email or "").strip().lower()
    local, _, domain = e.partition("@")
    first_token = re.split(r"[._+-]", local)[0] if local else ""
    qa_mailbox = re.fullmatch(r"(%s)\d*" % "|".join(TEST_WORDS), first_token) is not None
    verdict = "review" if valid_ssn(ssn) else "test"
    if domain and RESERVED_DOMAIN.search(domain):
        return verdict, f"reserved domain, cannot receive mail ({domain})"
    if domain in OUR_DOMAINS and qa_mailbox:
        return verdict, f"QA mailbox on our domain ({first_token}@{domain})"
    if ssn in QA_SSNS:
        return "test", "KYC sandbox SSN"
    if qa_mailbox:
        return "review", f"test-like mailbox on a public domain ({e})"
    if domain in OUR_DOMAINS and "test" in local:
        return "review", f"our domain, mailbox mentions test ({e})"
    if domain in DISPOSABLE_DOMAINS:
        return "review", f"disposable inbox ({domain})"
    if fold(first) in TEST_WORDS and fold(last) in TEST_WORDS | {"user", "account", "customer"}:
        return "review", "placeholder-like name"
    return "real", ""


def address_key(line, postal_code):
    """'1234 Main St.', '33101-0001' -> '1234mainst|33101'; None when incomplete."""
    street = re.sub(r"[^a-z0-9]", "", fold_keep_digits(line))
    zip5 = re.sub(r"\D", "", postal_code or "")[:5]
    return f"{street}|{zip5}" if street and len(zip5) == 5 else None


def fold_keep_digits(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def corroborated(a, b):
    """Within an SSN+DOB match: is a contact detail shared? (Names deliberately don't count.)"""
    return bool((a["email"] and a["email"] == b["email"]) or (a["phone"] and a["phone"] == b["phone"])
                or (a["address"] and a["address"] == b["address"]) or (a["fingerprints"] & b["fingerprints"]))
