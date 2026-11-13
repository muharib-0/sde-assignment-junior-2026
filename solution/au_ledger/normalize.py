"""Pure value-normalisation rules from the spec. No I/O, no database.

Every function returns the normalised value, or None when the input is invalid.
Callers decide whether "invalid" means a rejection or a field warning.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# ---------------------------------------------------------------- headers
_SEP_RUN = re.compile(r"[\s_]+")


def normalize_header(header: str) -> str:
    """lower-case, trim, collapse every run of '_' / whitespace into one space.

    We strip once more at the end so that a stray trailing '_' ("Name_") still
    matches. The spec doesn't say what to do there; being lenient is safer.
    """
    return _SEP_RUN.sub(" ", header.lower().strip()).strip()


# ---------------------------------------------------------------- phone
_NON_DIGIT = re.compile(r"\D")


def normalize_phone(raw: str) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid."""
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# ---------------------------------------------------------------- date
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_NUM = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})$")       # DD/MM/YYYY or DD-MM-YYYY
_DMY_MON = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")          # DD-Mon-YYYY


def parse_date(raw: str) -> str | None:
    """Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Returns ISO 'YYYY-MM-DD'."""
    s = raw.strip()
    try:
        if m := _ISO_DATE.match(s):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif m := _DMY_NUM.match(s):
            d, mo, y = int(m[1]), int(m[3]), int(m[4])
        elif m := _DMY_MON.match(s):
            mo = _MONTHS.get(m[2].lower())
            if mo is None:
                return None
            d, y = int(m[1]), int(m[3])
        else:
            return None
        return date(y, mo, d).isoformat()      # raises ValueError for 31/02, month 13, ...
    except ValueError:
        return None


# ---------------------------------------------------------------- decimal
_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)$")
_CENTS = Decimal("0.01")


def parse_decimal(raw: str) -> str | None:
    """'₹27,554' / 'Rs. 500' / 'INR 18678.3' -> '27554.00'. Must be >= 0.

    Commas are simply removed, so Indian grouping (1,00,000) works too.
    The result is a string, so no float rounding error gets in.
    """
    s = _CURRENCY_PREFIX.sub("", raw.strip(), count=1).replace(",", "")
    if not _PLAIN_NUMBER.match(s):          # rejects '-', 'N/A', negatives, '1e5', ''
        return None
    try:
        return str(Decimal(s).quantize(_CENTS, rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- enum
def parse_enum(raw: str, allowed: list[str]) -> str | None:
    v = raw.strip().lower()
    return v if v in {a.lower() for a in allowed} else None


# ---------------------------------------------------------------- timestamps
def parse_timestamp(raw: str) -> datetime | None:
    """'...Z' (UTC), '...+05:30' (offset), or naive '2026-10-03 10:01:00' (= IST).

    Returns an aware datetime in UTC, or None if it can't be parsed.
    """
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)            # spec: naive means IST
    return dt.astimezone(UTC)


def to_storage_ts(dt: datetime) -> str:
    """Fixed-width UTC ISO string: sorts correctly as plain text in SQL."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def ist_date(dt: datetime) -> str:
    return dt.astimezone(IST).date().isoformat()


def ist_iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="seconds")
