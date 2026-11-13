"""Row validation, effective disposition and bucket logic, tested as pure functions."""
from datetime import datetime, timezone

import pytest

from au_ledger.cli import DEFAULT_SCHEMAS
from au_ledger.report import Call, connectivity_rate, effective_disposition, lead_bucket
from au_ledger.schema import load_dispositions, load_schemas, validate_row

SCHEMAS = load_schemas(DEFAULT_SCHEMAS)
CATALOGUE = load_dispositions(DEFAULT_SCHEMAS / "dispositions.csv")
MOTOR = SCHEMAS["motor_renewal"]
GOOD = {"customer_name": "A B", "phone": "9876543210", "policy_number": "P1",
        "policy_expiry": "01/12/2026", "premium_amount": "₹1,000", "preferred_language": "HI"}


# ------------------------------------------------------------------ validation
def test_valid_row_is_normalised():
    res = validate_row(MOTOR, GOOD)
    assert res.reason is None and res.warnings == 0
    assert res.values == {"customer_name": "A B", "phone": "+919876543210", "policy_number": "P1",
                          "policy_expiry": "2026-12-01", "premium_amount": "1000.00",
                          "preferred_language": "hi"}


def test_first_failure_in_schema_order_wins():
    # customer_name comes before phone in the schema, so it is the reason
    res = validate_row(MOTOR, {**GOOD, "customer_name": "", "phone": "N/A"})
    assert res.reason == "missing_required:customer_name"
    res = validate_row(MOTOR, {**GOOD, "phone": "N/A", "policy_number": ""})
    assert res.reason == "invalid_phone"


def test_missing_vs_invalid_phone():
    assert validate_row(MOTOR, {**GOOD, "phone": ""}).reason == "missing_required:phone"
    assert validate_row(MOTOR, {**GOOD, "phone": "12345"}).reason == "invalid_phone"


def test_invalid_optional_fields_are_warnings_and_null():
    res = validate_row(MOTOR, {**GOOD, "policy_expiry": "31/02/2026", "premium_amount": "N/A",
                               "preferred_language": "Hindi"})
    assert res.reason is None
    assert res.warnings == 3
    assert res.values["policy_expiry"] is None and res.values["preferred_language"] is None


def test_missing_optional_field_is_not_a_warning():
    res = validate_row(MOTOR, {k: v for k, v in GOOD.items() if k != "premium_amount"})
    assert res.reason is None and res.warnings == 0 and res.values["premium_amount"] is None


def test_header_mapping_aliases_and_extra_columns():
    headers = ["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date", "Premium", "agent_notes"]
    assert MOTOR.map_headers(headers) == {
        "preferred_language": 0, "customer_name": 1, "policy_number": 2, "phone": 3,
        "policy_expiry": 4, "premium_amount": 5}


# ------------------------------------------------------------------ dispositions
def t(s):
    return f"2026-10-0{s}T00:00:00.000000+00:00"


def test_effective_disposition_none():
    assert effective_disposition([], []) is None


def test_latest_set_wins_without_override():
    assert effective_disposition([(t(1), "e1", "callback_requested"), (t(2), "e2", "renewed")], []) == "renewed"


def test_override_beats_a_later_set():
    # the override is older than the second set, but overrides always take precedence
    sets = [(t(1), "e1", "renewed"), (t(3), "e3", "callback_requested")]
    assert effective_disposition(sets, [(t(2), "e2", "not_interested")]) == "not_interested"


def test_latest_override_wins_by_time_not_arrival_order():
    overrides = [(t(5), "e9", "renewed"), (t(4), "e10", "not_interested")]
    assert effective_disposition([(t(1), "e1", "dnd")], overrides) == "renewed"


# ------------------------------------------------------------------ buckets
def call(effective=None, failed=False, connected=False):
    c = Call("motor_renewal", "c", 1, datetime(2026, 10, 1, tzinfo=timezone.utc),
             connected=connected, telephony_failed=failed)
    if effective:
        c.sets.append((t(1), "e", effective))
    return c


def test_bucket_no_calls():
    assert lead_bucket([], CATALOGUE) == "yet_to_reach_out"


def test_bucket_terminal_disposition_beats_everything():
    assert lead_bucket([call(failed=True), call("not_interested")], CATALOGUE) == "completed"


def test_bucket_non_terminal_disposition_is_dialed():
    assert lead_bucket([call("callback_requested")], CATALOGUE) == "in_progress_dialed"


def test_bucket_all_telephony_failed():
    assert lead_bucket([call(failed=True), call(failed=True)], CATALOGUE) == "in_progress_telephony_failed"


def test_bucket_some_telephony_failed_is_dialed():
    assert lead_bucket([call(failed=True), call()], CATALOGUE) == "in_progress_dialed"


def test_bucket_uses_effective_disposition():
    c = call("renewed")                                     # terminal...
    c.overrides.append((t(2), "o", "callback_requested"))  # ...corrected to non-terminal
    assert lead_bucket([c], CATALOGUE) == "in_progress_dialed"


@pytest.mark.parametrize("connected,total,expected", [
    (9, 16, "0.5625"), (1, 3, "0.3333"), (2, 3, "0.6667"), (0, 5, "0.0000"), (5, 5, "1.0000"),
    (1, 32, "0.0313"),   # exactly 0.03125: half-up, not banker's rounding
])
def test_connectivity_rate(connected, total, expected):
    assert connectivity_rate(connected, total) == expected
