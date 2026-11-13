"""End-to-end: a tiny hand-made dataset where every expected number was worked out by hand.

drop_1 leads (motor_renewal/batch_001):
  row 1  Asha, ...0001          -> created (lead P-001)
  row 2  no name                -> rejected missing_required:customer_name
  row 3  phone 12345            -> rejected invalid_phone
  (blank row: skipped, not counted)
  row 4  Asha again, ...0001    -> merged: name and premium overwritten, language kept
  row 5  Meena, expiry 'soon'   -> created (lead P-004), 1 field warning
drop_2 leads (motor_renewal/batch_002: renamed + re-ordered headers, extra column):
  row 1  ...0005                -> created (lead P-005). Its events arrived in drop_1!
  row 2  ...0001 without name   -> rejected (a rejected row never merges)
  row 3  ...0004 'Meena K'      -> merged
"""
import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from au_ledger.cli import REPO_ROOT, main
from au_ledger.crypto import PiiVault

H = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"]
BATCH_001 = [H,
             ["Asha Rao", "9876500001", "P-001", "01/12/2026", "₹1,000", "HI"],
             ["", "9876500002", "P-002", "", "", ""],
             ["Ravi Kumar", "12345", "P-003", "", "", ""],
             [" ", " ", "", "", "", ""],
             ["Asha R.", "+91 98765 00001", "P-001", "", "1200", ""],
             ["Meena", "9876500004", "P-004", "soon", "", ""]]
BATCH_002 = [["Mobile No", "Insured Name", "Policy No", "agent_notes", "Language"],
             ["9876500005", "Late Lead", "P-005", "VIP", "ta"],
             ["09876500001", "", "P-001", "", "en"],
             ["9876500004", "Meena K", "P-004", "", ""]]


def ev(eid, etype, call, at, phone=None, **payload):
    e = {"event_id": eid, "event_type": etype, "workflow_id": "motor_renewal", "call_id": call,
         "occurred_at": at, "payload": payload}
    if phone:
        e["phone"] = phone
    return json.dumps(e)


EVENTS_1 = [
    ev("e1", "call.initiated", "c1", "2026-10-01T04:00:00Z", "9876500001"),         # 09:30 IST
    ev("e2", "call.connected", "c1", "2026-10-01T04:00:10Z", "+91 98765 00001"),
    ev("e3", "call.ended", "c1", "2026-10-01T04:05:00Z", "09876500001", duration_sec=290),
    ev("e4", "disposition.set", "c1", "2026-10-01T04:06:00Z", "9876500001", disposition_code="renewed"),
    ev("e5", "disposition.override", "c1", "2026-10-03T10:00:00+05:30",            # no phone
       disposition_code="not_interested", overridden_by="sup_1", reason="qa_review"),
    ev("e6", "call.initiated", "c2", "2026-10-02 10:00:00", "9876500001"),          # naive = IST
    ev("e7", "call.telephony_failed", "c2", "2026-10-02 10:00:03", "9876500001", reason="sip_503"),
    ev("e8", "call.initiated", "c3", "2026-10-01T20:00:00Z", "9876500004"),         # 01:30 IST on 10-02
    ev("e9", "call.ended", "c3", "2026-10-01T20:00:40Z", "9876500004", duration_sec=0, end_reason="no_answer"),
    ev("e10", "disposition.set", "c3", "2026-10-01T20:01:00Z", "9876500004", disposition_code="RENEWD"),
    ev("e11", "call.initiated", "c4", "2026-10-02T05:00:00+05:30", "9876500005"),   # lead arrives in drop_2
    ev("e12", "call.connected", "c4", "2026-10-02T05:00:09+05:30", "9876500005"),
    ev("e13", "disposition.set", "c4", "2026-10-02T05:09:00+05:30", "9876500005", disposition_code="renewed"),
    ev("e14", "call.initiated", "c5", "2026-10-02T06:00:00Z", "9999999999"),        # never uploaded
    '{"event_id": "e15", "event_ty',                                                # malformed: bad JSON
    '{"event_type": "call.initiated"}',                                             # malformed: no event_id
    "",                                                                             # empty: not counted
    ev("e1", "call.initiated", "c1", "2026-10-01T04:00:00Z", "9876500001"),         # duplicate
]
EVENTS_2 = [
    ev("e11", "call.initiated", "c4", "2026-10-02T05:00:00+05:30", "9876500005"),   # duplicate across drops
    ev("e16", "disposition.override", "c5", "2026-10-04T10:00:00Z", disposition_code="renewed"),  # orphan call
    # c6 never had a call.initiated: linked (not orphan) events, but not a call, so no success
    ev("e17", "call.connected", "c6", "2026-10-03T05:00:00Z", "9876500004"),
    ev("e18", "disposition.set", "c6", "2026-10-03T05:09:00Z", "9876500004", disposition_code="renewed"),
]


def write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)


def write_lines(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def drops(tmp_path, monkeypatch):
    monkeypatch.setenv("AU_PII_SECRET", "test-secret")
    write_csv(tmp_path / "drop_1/leads/motor_renewal/batch_001.csv", BATCH_001)
    write_lines(tmp_path / "drop_1/events/events_0001.jsonl", EVENTS_1)
    write_csv(tmp_path / "drop_2/leads/motor_renewal/batch_002.csv", BATCH_002)
    write_lines(tmp_path / "drop_2/events/events_1001.jsonl", EVENTS_2)
    return tmp_path


def run(tmp: Path, db: str, *args) -> int:
    return main(["--db", str(tmp / db), *map(str, args)])


def report(tmp: Path, db: str, out: str) -> dict:
    assert run(tmp, db, "report", "--out", tmp / out) == 0
    d = tmp / out
    return {"quality": json.loads((d / "data_quality.json").read_text()),
            **{name: list(csv.DictReader((d / f"{name}.csv").open(encoding="utf-8")))
               for name in ("batch_summary", "daily_overview", "lead_report", "rejections")}}


# ------------------------------------------------------------------ the full expected output
def test_reports_match_hand_computed_values(drops):
    run(drops, "x.db", "ingest", drops / "drop_1")
    run(drops, "x.db", "ingest", drops / "drop_2")
    r = report(drops, "x.db", "out")

    assert r["quality"] == {
        "lead_rows_total": 8, "lead_rows_rejected": 3,
        "rejections_by_reason": {"invalid_phone": 1, "missing_required:customer_name": 2},
        "duplicate_lead_rows_merged": 2, "field_warnings": 1,
        "events_total": 21, "malformed_events": 2, "duplicate_events_dropped": 2,
        "orphan_events": 2, "unknown_disposition_events": 1}

    assert [(x["batch_id"], x["row_number"], x["reason"]) for x in r["rejections"]] == [
        ("batch_001", "2", "missing_required:customer_name"),
        ("batch_001", "3", "invalid_phone"),
        ("batch_002", "2", "missing_required:customer_name")]

    assert [list(x.values()) for x in r["batch_summary"]] == [
        ["motor_renewal", "batch_001", "2", "0", "0", "1", "1"],
        ["motor_renewal", "batch_002", "1", "0", "0", "0", "1"]]

    assert [list(x.values()) for x in r["daily_overview"]] == [
        ["motor_renewal", "2026-10-01", "1", "1", "1.0000", "0"],
        ["motor_renewal", "2026-10-02", "3", "1", "0.3333", "1"]]

    assert [list(x.values()) for x in r["lead_report"]] == [
        # override turned 'renewed' into terminal 'not_interested': completed, never a success
        ["motor_renewal", "P-001", "batch_001", "+91******0001", "completed", "2", "1",
         "2026-10-02T10:00:00+05:30", "not_interested", "false"],
        # only real call ended no_answer; 'RENEWD' is unknown, so ignored; c6 isn't a call
        ["motor_renewal", "P-004", "batch_001", "+91******0004", "in_progress_dialed", "1", "0",
         "2026-10-02T01:30:00+05:30", "", "false"],
        # events came in drop_1, lead in drop_2: counted once the lead exists
        ["motor_renewal", "P-005", "batch_002", "+91******0005", "completed", "1", "1",
         "2026-10-02T05:00:00+05:30", "renewed", "true"]]


def test_events_before_their_lead_are_orphans_until_the_lead_arrives(drops):
    run(drops, "x.db", "ingest", drops / "drop_1")
    q = report(drops, "x.db", "r1")["quality"]
    assert q["orphan_events"] == 4             # e11, e12, e13 (lead not yet uploaded) + e14
    run(drops, "x.db", "ingest", drops / "drop_2")
    assert report(drops, "x.db", "r2")["quality"]["orphan_events"] == 2   # e14 + override on c5


def test_merge_last_write_wins_for_non_null_fields_and_defaults_after(drops):
    run(drops, "x.db", "ingest", drops / "drop_1", drops / "drop_2")
    vault = PiiVault(b"test-secret")
    conn = sqlite3.connect(drops / "x.db")
    leads = {ref: (batch, json.loads(attrs), vault.decrypt(pii)) for ref, batch, attrs, pii in
             conn.execute("SELECT external_ref, batch_id, attributes, pii_enc FROM leads")}
    batch, attrs, pii = leads["P-001"]
    assert batch == "batch_001"
    assert pii == {"customer_name": "Asha R.", "phone": "+919876500001"}       # overwritten
    assert attrs["premium_amount"] == "1200.00"                                # overwritten
    assert attrs["preferred_language"] == "hi"                                 # '' doesn't overwrite
    assert attrs["policy_expiry"] == "2026-12-01"                              # kept
    batch, attrs, pii = leads["P-004"]
    assert batch == "batch_001"                                                # first batch is permanent
    assert pii["customer_name"] == "Meena K"
    assert attrs["policy_expiry"] is None                                      # 'soon' was invalid
    assert attrs["preferred_language"] == "en"                                 # default applied


# ------------------------------------------------------------------ idempotency & incrementality
def test_reingest_changes_nothing(drops):
    run(drops, "x.db", "ingest", drops / "drop_1", drops / "drop_2")
    first = report(drops, "x.db", "a")
    run(drops, "x.db", "ingest", drops / "drop_1", drops / "drop_2", drops / "drop_1")
    assert report(drops, "x.db", "b") == first


def test_incremental_equals_all_at_once(drops):
    run(drops, "inc.db", "ingest", drops / "drop_1")
    run(drops, "inc.db", "ingest", drops / "drop_2")
    run(drops, "all.db", "ingest", drops / "drop_1", drops / "drop_2")
    assert report(drops, "inc.db", "a") == report(drops, "all.db", "b")


def test_changed_file_with_same_name_is_refused(drops):
    run(drops, "x.db", "ingest", drops / "drop_1")
    write_csv(drops / "drop_1/leads/motor_renewal/batch_001.csv", BATCH_001[:2])
    assert run(drops, "x.db", "ingest", drops / "drop_1") == 1


def test_crash_mid_file_rolls_back_cleanly(drops, monkeypatch):
    """Simulate a crash after a file's rows are written but before it's committed."""
    from au_ledger import ingest
    real = ingest.ingest_event_file

    def boom(*a, **kw):
        real(*a, **kw)
        raise KeyboardInterrupt("simulated crash")

    monkeypatch.setattr(ingest, "ingest_event_file", boom)
    with pytest.raises(KeyboardInterrupt):
        run(drops, "x.db", "ingest", drops / "drop_1")
    monkeypatch.setattr(ingest, "ingest_event_file", real)
    run(drops, "x.db", "ingest", drops / "drop_1", drops / "drop_2")        # the re-run
    run(drops, "ok.db", "ingest", drops / "drop_1", drops / "drop_2")       # never crashed
    assert report(drops, "x.db", "a") == report(drops, "ok.db", "b")


# ------------------------------------------------------------------ ledger & PII guarantees
def test_ledger_is_append_only(drops):
    run(drops, "x.db", "ingest", drops / "drop_1")
    conn = sqlite3.connect(drops / "x.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET event_type = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_no_pii_in_plaintext_anywhere_in_the_database(drops):
    run(drops, "x.db", "ingest", drops / "drop_1", drops / "drop_2")
    raw = b"".join(p.read_bytes() for p in drops.glob("x.db*"))
    for secret in ["Asha", "Meena", "Late Lead", "9876500001", "98765 00001", "9876500005"]:
        assert secret.encode() not in raw, secret
    assert b"P-001" in raw          # control: non-PII data *is* findable, so the search works


def test_wrong_key_is_refused(drops, monkeypatch):
    run(drops, "x.db", "ingest", drops / "drop_1")
    monkeypatch.setenv("AU_PII_SECRET", "another-secret")
    with pytest.raises(SystemExit):
        run(drops, "x.db", "ingest", drops / "drop_2")


# ------------------------------------------------------------------ the real generator
def test_generated_dataset_incremental_equals_all_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AU_PII_SECRET", "test-secret")
    subprocess.run([sys.executable, str(REPO_ROOT / "assignment-a-data/tools/generate_data.py"),
                    "--seed", "7", "--scale", "small", "--out", str(tmp_path / "data")],
                   check=True, capture_output=True)
    d1, d2 = tmp_path / "data/drop_1", tmp_path / "data/drop_2"
    run(tmp_path, "inc.db", "ingest", d1)
    run(tmp_path, "inc.db", "ingest", d2)
    run(tmp_path, "inc.db", "ingest", d1)
    run(tmp_path, "all.db", "ingest", d1, d2)
    a, b = report(tmp_path, "inc.db", "a"), report(tmp_path, "all.db", "b")
    assert a == b
    q = a["quality"]
    assert q["lead_rows_total"] == q["lead_rows_rejected"] + q["duplicate_lead_rows_merged"] + len(a["lead_report"])
