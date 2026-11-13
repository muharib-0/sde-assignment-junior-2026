from datetime import datetime, timezone

import pytest

from au_ledger import normalize as n


@pytest.mark.parametrize("raw", [
    "9876543210",            # plain
    "+91 98765 43210",       # spaced with country code
    "09876543210",           # trunk prefix 0
    "91-98765-43210",        # dashed
    "(+91) 9876543210",      # brackets
    "+919876543210",         # E.164
    " 98765-43210 ",         # stray whitespace
])
def test_phone_formats_all_normalise_to_the_same_value(raw):
    assert n.normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",             # 9 digits
    "5876543210",            # 10 digits but starts with 5
    "022-2345-6789",         # landline: 11 digits, drop 0 -> starts with 2
    "+1 415 555 0134",       # US number: 11 digits, not starting with 0
    "N/A",
    "",
    "919876543210123",       # too long
    "00919876543210",        # 14 digits: no rule applies
])
def test_invalid_phones(raw):
    assert n.normalize_phone(raw) is None


def test_phone_rules_apply_only_at_exact_lengths():
    # 12 digits starting with 91 -> strip; but a 10-digit number starting with 91 is not touched
    assert n.normalize_phone("919123456789") == "+919123456789"
    assert n.normalize_phone("9123456789") == "+919123456789"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),       # day first!
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),      # month name case-insensitive
    ("5/1/2026", "2026-01-05"),
    ("29/02/2028", "2028-02-29"),       # leap year
])
def test_dates(raw, expected):
    assert n.parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "29/02/2026", "soon", "TBD",
                                 "01/12/26", "01-Dex-2026", "2026/12/01", "01.12.2026", "01/12-2026"])
def test_invalid_dates(raw):
    assert n.parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("27554", "27554.00"),
    ("4737.14", "4737.14"),
    ("₹27,554", "27554.00"),
    ("₹ 1,00,000", "100000.00"),        # Indian digit grouping
    ("Rs. 24787", "24787.00"),
    ("rs24787", "24787.00"),
    ("Rs 5", "5.00"),
    ("INR 18678.3", "18678.30"),
    ("inr18678.345", "18678.35"),       # rounds half-up to 2 places
    ("0", "0.00"),
])
def test_decimals(raw, expected):
    assert n.parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-500", "₹", "1e5", "12.5.3", "Rs. -5"])
def test_invalid_decimals(raw):
    assert n.parse_decimal(raw) is None


def test_enum_is_case_insensitive_and_stored_lower():
    assert n.parse_enum("HI", ["en", "hi"]) == "hi"
    assert n.parse_enum(" hi ", ["en", "hi"]) == "hi"
    assert n.parse_enum("Hindi", ["en", "hi"]) is None


@pytest.mark.parametrize("raw", ["Mobile No", "mobile_no", "  MOBILE   NO ", "mobile__no", "Mobile_ No"])
def test_header_normalisation(raw):
    assert n.normalize_header(raw) == "mobile no"


def test_timestamps_three_forms_same_instant():
    utc = n.parse_timestamp("2026-10-05T07:50:21Z")
    offset = n.parse_timestamp("2026-10-05T13:20:21+05:30")
    naive = n.parse_timestamp("2026-10-05 13:20:21")          # naive means IST
    assert utc == offset == naive == datetime(2026, 10, 5, 7, 50, 21, tzinfo=timezone.utc)


def test_ist_date_crosses_midnight():
    # 20:00 UTC is 01:30 the next day in IST: reports must use the IST date
    ts = n.parse_timestamp("2026-10-05T20:00:00Z")
    assert n.ist_date(ts) == "2026-10-06"
    assert n.ist_iso(ts) == "2026-10-06T01:30:00+05:30"


def test_storage_timestamps_sort_as_text():
    a = n.to_storage_ts(n.parse_timestamp("2026-10-05T09:00:00Z"))
    b = n.to_storage_ts(n.parse_timestamp("2026-10-05 15:00:01"))   # IST, = 09:30:01Z
    assert a < b


def test_bad_timestamps():
    assert n.parse_timestamp("yesterday") is None
    assert n.parse_timestamp(None) is None
