"""Identity rules and merge planning. Pure Python: no database needed (`make test`)."""
from datetime import date, datetime, timedelta

import pytest

from fundo.checks import key_ranges
from fundo.dedupe import Customer, plan
from fundo.identity import classify_test, email_status, phone_status, valid_ssn


@pytest.mark.parametrize("first,last,email,ssn,verdict", [
    ("Test", "User", "test@fundo.com", None, "test"),           # QA mailbox on our domain
    ("Maria", "Prueba", "qa+signup1@fundo.com", None, "test"),
    ("John", "Doe", "john.doe@example.com", None, "test"),      # RFC 2606: cannot receive mail
    ("Pedro", "Pruebas", "pp1@fundo.com", "123456789", "test"),  # KYC sandbox SSN
    ("Karen", "Testerman", "karen.testerman@gmail.com", "212345678", "real"),
    ("Luis", "Contreras", "contestwinner88@gmail.com", None, "real"),
    ("Ana", "Rodriguez", "ana.rodriguez@fundo.com", "212345679", "real"),  # staff are real customers
    ("Test", "Test", "test+kyc@gmail.com", None, "review"),     # someone else's inbox: a human decides
    ("Load", "Test", "loadtest42@fundo.com", None, "review"),
    ("Luis", "Ortiz", "luis.ortiz@mailinator.com", "312345678", "review"),
    ("Maria", "Lopez", "maria@example.com", "212345678", "review"),  # a real SSN outweighs a throwaway email
])
def test_test_account_classification(first, last, email, ssn, verdict):
    assert classify_test(first, last, email, ssn)[0] == verdict


@pytest.mark.parametrize("raw,expected", [
    ("(305) 412-0134", ("+13054120134", "valid_normalised")),
    ("+13054120134", ("+13054120134", "valid")),
    ("1 305 412 0134", ("+13054120134", "valid_normalised")),
    ("(305) 555-0134", (None, "placeholder")),  # 555-01xx is reserved for fiction
    ("305-555-013", (None, "invalid")),
    ("305555O134", (None, "invalid")),
    ("000-000-0000", (None, "placeholder")),
    ("+52 55 1234 5678", ("+525512345678", "non_us")),
    ("", (None, "missing")),
])
def test_phone(raw, expected):
    assert phone_status(raw) == expected


@pytest.mark.parametrize("raw,status", [
    ("maria@gmail.com", "valid"), ("Maria@Gmail.com", "valid_normalised"), ("maria@gmial.com", "suspect_typo"),
    ("maria@gmail", "invalid"), ("maria@@gmail.com", "invalid"), ("none@none.com", "placeholder"),
])
def test_email(raw, status):
    assert email_status(raw)[1] == status


def test_ssn_validity():
    assert valid_ssn("212345678")
    assert valid_ssn("912781234")  # ITIN: real identifier, not junk
    assert not valid_ssn("123456789") and not valid_ssn("000123456") and not valid_ssn("111111111")


def customer(cid, first, last, ssn=None, dob=None, email=None, phone=None, address=None, funded=False):
    return Customer(cid, first.lower(), last.lower(), f"{first} {last}", email, email, "valid", phone, "valid",
                    address, dob, ssn, datetime(2025, 1, 1) + timedelta(days=cid), None, funded, int(funded),
                    frozenset())


DOB = date(1990, 5, 17)


def test_proven_duplicate_merges_into_funded_customer():
    p = plan([customer(1, "Jose", "Garcia", "212345678", DOB, email="jg@gmail.com", address="12mainst|33101"),
              customer(2, "Jose", "Garcia", "212345678", DOB, email="jose.g@yahoo.com", address="12mainst|33101",
                       funded=True)])
    assert p["decisions"] == {1: ("merge_into", 2, p["decisions"][1][2])}


def test_same_ssn_dob_and_name_without_shared_contact_is_held():
    # What a stolen identity looks like: the impostor must give the victim's name to pass KYC.
    p = plan([customer(1, "Ana", "Diaz", "212345678", DOB, email="ana@gmail.com", phone="+13054120101", funded=True),
              customer(2, "Ana", "Diaz", "212345678", DOB, email="x@yahoo.com", phone="+17134120102")])
    assert p["decisions"][2][0] == "hold" and not p["merges"]


def test_two_funded_customers_are_held_not_merged():
    p = plan([customer(1, "Ana", "Diaz", "212345678", DOB, phone="+13054120101", funded=True),
              customer(2, "Ana", "Diaz", "212345678", DOB, phone="+13054120101", funded=True)])
    assert [a for a, _, _ in p["decisions"].values()] == ["hold"]


def test_ssn_and_dob_alone_are_held():
    p = plan([customer(1, "Ana", "Diaz", "212345678", DOB, email="a@x.com"),
              customer(2, "Bob", "Lee", "212345678", DOB, email="b@y.com")])
    assert p["decisions"][2][0] == "hold" and not p["merges"]


def test_shared_phone_with_different_ssns_is_not_a_duplicate():
    p = plan([customer(1, "Ana", "Diaz", "212345678", DOB, phone="+13055550101"),
              customer(2, "Luis", "Diaz", "212345679", date(1988, 1, 2), phone="+13055550101")])
    assert not p["decisions"] and not p["review"] and (1, 2) in p["distinct"]


def test_shared_email_without_ssn_goes_to_review():
    p = plan([customer(1, "Ana", "Diaz", email="ana@gmail.com"), customer(2, "Ana", "Diaz", email="ana@gmail.com")])
    assert not p["decisions"] and p["review"][(1, 2)] == {"same email"}


def test_key_ranges():
    assert key_ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"
