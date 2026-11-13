"""Ingest a drop directory: lead CSVs and event JSONL files.

Safety properties:
* Each file is processed in ONE transaction, together with the row that marks it
  as ingested. A crash halfway rolls the whole file back, and a re-run redoes it.
* A file already ingested with the same SHA-256 is skipped, so re-running a drop
  changes nothing. The same file name with *different* content is refused
  loudly instead of being half-merged.
* Lead files go in filename order and rows in file order, so lead merging is
  deterministic. Events are never "linked" at ingest time (see report.py), so the
  order of event files doesn't affect results.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import normalize as n
from .crypto import PiiVault
from .schema import WorkflowSchema, validate_row


class IngestError(Exception):
    pass


@dataclass
class IngestStats:
    files_ingested: int = 0
    files_skipped: int = 0
    lead_rows: int = 0
    events: int = 0
    notes: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_ingested(conn: sqlite3.Connection, file_key: str, sha: str) -> bool:
    row = conn.execute("SELECT sha256, source_path FROM ingested_files WHERE file_key = ?",
                       (file_key,)).fetchone()
    if row is None:
        return False
    if row[0] == sha:
        return True
    raise IngestError(f"{file_key} was already ingested from {row[1]} with different content; "
                      "refusing to ingest a changed copy")


# ======================================================================== drops
def ingest_drop(conn: sqlite3.Connection, vault: PiiVault,
                schemas: dict[str, WorkflowSchema], drop: Path) -> IngestStats:
    if not drop.is_dir():
        raise IngestError(f"{drop} is not a directory")
    stats = IngestStats()

    leads_dir = drop / "leads"
    if leads_dir.is_dir():
        for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
            schema = schemas.get(wf_dir.name)
            if schema is None:
                # Not recorded as ingested, so it is picked up once a schema exists.
                stats.notes.append(f"skipped {wf_dir}: no schema for workflow {wf_dir.name!r}")
                continue
            for path in sorted(wf_dir.glob("*.csv")):
                _run_file(conn, stats, path, f"leads/{schema.workflow_id}/{path.stem}",
                          lambda p=path, s=schema: ingest_lead_file(conn, vault, s, p))

    events_dir = drop / "events"
    if events_dir.is_dir():
        for path in sorted(events_dir.glob("*.jsonl")):
            _run_file(conn, stats, path, f"events/{path.stem}",
                      lambda p=path: ingest_event_file(conn, vault, p))
    return stats


def _run_file(conn, stats: IngestStats, path: Path, file_key: str, work) -> None:
    sha = _sha256(path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _already_ingested(conn, file_key, sha):
            conn.execute("ROLLBACK")
            stats.files_skipped += 1
            return
        count = work()
        kind, _, rest = file_key.partition("/")
        wf, batch = (rest.split("/") if kind == "leads" else (None, None))
        conn.execute(
            "INSERT INTO ingested_files (file_key, kind, workflow_id, batch_id, sha256, source_path,"
            " ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_key, kind, wf, batch, sha, str(path), _now()))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    stats.files_ingested += 1
    if kind == "leads":
        stats.lead_rows += count
    else:
        stats.events += count


# ======================================================================== leads
@dataclass
class _LeadState:
    lead_id: int | None              # None => not in the DB yet
    batch_id: str
    values: dict | None = None       # merged field values; loaded lazily for existing leads
    dirty: bool = False


def ingest_lead_file(conn: sqlite3.Connection, vault: PiiVault,
                     schema: WorkflowSchema, path: Path) -> int:
    """Returns the number of non-blank data rows processed."""
    wf, batch_id = schema.workflow_id, path.stem
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        mapping = schema.map_headers(header)
        rows = list(reader)

    # phone_hash -> lead_id for this workflow. Cheap: no decryption needed.
    known = {h: _LeadState(lid, b) for lid, h, b in conn.execute(
        "SELECT lead_id, phone_hash, batch_id FROM leads WHERE workflow_id = ?", (wf,))}

    log = []          # (row_number, outcome, reason, phone_hash, warnings)
    row_number = 0
    for cells in rows:
        if all(not c.strip() for c in cells):
            continue                                   # blank row: skipped, not counted
        row_number += 1
        raw = {f: (cells[i].strip() if i < len(cells) else "") for f, i in mapping.items()}
        res = validate_row(schema, raw)
        if res.reason is not None:
            log.append((row_number, "rejected", res.reason, None, 0))
            continue

        phone_hash = vault.blind_index(res.values[schema.phone_field])
        state = known.get(phone_hash)
        if state is None:
            known[phone_hash] = _LeadState(None, batch_id, dict(res.values), dirty=True)
            outcome = "created"
        else:
            if state.values is None:
                state.values = _load_values(conn, vault, state.lead_id)
            # last write wins, but only for fields where the new row has a value
            state.values.update({k: v for k, v in res.values.items() if v is not None})
            state.dirty = True
            outcome = "merged"
        log.append((row_number, outcome, None, phone_hash, res.warnings))

    _save_leads(conn, vault, schema, known)
    conn.executemany(
        "INSERT INTO lead_rows (workflow_id, batch_id, row_number, outcome, reason, lead_id,"
        " field_warnings) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(wf, batch_id, rn, outcome, reason, known[h].lead_id if h else None, w)
         for rn, outcome, reason, h, w in log])
    return row_number


def _load_values(conn, vault: PiiVault, lead_id: int) -> dict:
    pii_enc, attributes = conn.execute(
        "SELECT pii_enc, attributes FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    return {**json.loads(attributes), **vault.decrypt(pii_enc)}


def _save_leads(conn, vault: PiiVault, schema: WorkflowSchema, known: dict[str, _LeadState]) -> None:
    """Write every lead this file created or changed, then fill in new lead_ids."""
    wf, now = schema.workflow_id, _now()
    inserts, updates = [], []
    for phone_hash, state in known.items():
        if not state.dirty:
            continue
        values = dict(state.values)
        for f in schema.fields:             # defaults apply after merging, to what is still null
            if values.get(f.name) is None and f.default is not None:
                values[f.name] = f.default
        pii = vault.encrypt({f.name: values.get(f.name) for f in schema.fields if f.pii})
        attrs = json.dumps({f.name: values.get(f.name) for f in schema.fields if not f.pii}, sort_keys=True)
        ref = values.get(schema.reference_field)
        if state.lead_id is None:
            inserts.append((wf, phone_hash, values[schema.phone_field][-4:], state.batch_id, ref,
                            pii, attrs, now, now))
        else:
            updates.append((ref, pii, attrs, now, state.lead_id))
    conn.executemany(
        "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref,"
        " pii_enc, attributes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", inserts)
    conn.executemany(
        "UPDATE leads SET external_ref = ?, pii_enc = ?, attributes = ?, updated_at = ? WHERE lead_id = ?",
        updates)
    if inserts:
        for phone_hash, lead_id in conn.execute(
                "SELECT phone_hash, lead_id FROM leads WHERE workflow_id = ?", (wf,)):
            if phone_hash in known:
                known[phone_hash].lead_id = lead_id


# ======================================================================== events
def ingest_event_file(conn: sqlite3.Connection, vault: PiiVault, path: Path) -> int:
    """Returns the number of non-empty lines read."""
    source = path.name
    lines_read = 0
    rejects: list[tuple] = []           # (source_file, line_no, kind, event_id)
    candidates: list[tuple[int, dict]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            lines_read += 1
            ev = _parse_event_line(line)
            if ev is None:
                rejects.append((source, line_no, "malformed", None))
            else:
                candidates.append((line_no, ev))

    # Duplicate = event_id already in the ledger (earlier file/drop) or earlier in this file.
    seen = _existing_event_ids(conn, {ev["event_id"] for _, ev in candidates})
    inserts = []
    for line_no, ev in candidates:
        if ev["event_id"] in seen:
            rejects.append((source, line_no, "duplicate", ev["event_id"]))
            continue
        seen.add(ev["event_id"])
        phone = ev.get("phone")
        canonical = n.normalize_phone(str(phone)) if isinstance(phone, (str, int)) else None
        inserts.append((
            ev["event_id"], ev["event_type"], _str_or_none(ev.get("workflow_id")),
            _str_or_none(ev.get("call_id")), n.to_storage_ts(ev["_ts"]),
            vault.blind_index(canonical) if canonical else None,
            json.dumps(ev["payload"], sort_keys=True, ensure_ascii=False), source, line_no))

    conn.executemany(
        "INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at, phone_hash,"
        " payload, source_file, line_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", inserts)
    conn.executemany(
        "INSERT INTO event_rejects (source_file, line_no, kind, event_id) VALUES (?, ?, ?, ?)", rejects)
    return lines_read


def _existing_event_ids(conn: sqlite3.Connection, ids: set[str]) -> set[str]:
    found: set[str] = set()
    ids_list = list(ids)
    for i in range(0, len(ids_list), 500):
        chunk = ids_list[i:i + 500]
        found.update(r[0] for r in conn.execute(
            f"SELECT event_id FROM events WHERE event_id IN ({','.join('?' * len(chunk))})", chunk))
    return found


def _str_or_none(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _parse_event_line(line: str) -> dict | None:
    """The parsed event, or None if malformed.

    Spec: malformed = not valid JSON, or no event_id. We also count lines with no
    event_type or an unparseable occurred_at as malformed: they can't be placed
    in any report, and keeping them would just hide the problem.
    """
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(ev, dict):
        return None
    if not isinstance(ev.get("event_id"), str) or not ev["event_id"].strip():
        return None
    if not isinstance(ev.get("event_type"), str) or not ev["event_type"]:
        return None
    ts = n.parse_timestamp(ev.get("occurred_at"))
    if ts is None:
        return None
    ev["_ts"] = ts
    if not isinstance(ev.get("payload"), dict):
        ev["payload"] = {}
    return ev
