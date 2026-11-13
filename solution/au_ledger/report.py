"""Derive the five report files from the database.

Why events are linked here and not at ingest time: an event can arrive before
its lead (in an earlier drop). If we linked at ingest, we'd have to go back and
"fix" old events when the lead shows up. Linking at report time against the
leads we have *now* means reports always reflect everything ingested so far,
the ledger never has to be updated, and drop_1 + drop_2 gives the same answer
as both at once.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import normalize as n
from .schema import Disposition

BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")

# (occurred_at, event_id, code): tuples sort by time, then event_id as a deterministic tie-break
DispEvent = tuple[str, str, str]


# ======================================================================== pure rules
def effective_disposition(sets: list[DispEvent], overrides: list[DispEvent]) -> str | None:
    """Latest override wins; otherwise the latest disposition.set; otherwise None."""
    if overrides:
        return max(overrides)[2]
    if sets:
        return max(sets)[2]
    return None


@dataclass
class Call:
    workflow_id: str
    call_id: str
    lead_id: int
    initiated_at: datetime
    connected: bool = False
    telephony_failed: bool = False
    sets: list[DispEvent] = field(default_factory=list)
    overrides: list[DispEvent] = field(default_factory=list)

    @property
    def effective(self) -> str | None:
        return effective_disposition(self.sets, self.overrides)


def lead_bucket(calls: list[Call], catalogue: dict[tuple[str, str], Disposition]) -> str:
    """The first matching bucket, in spec order."""
    if not calls:
        return "yet_to_reach_out"
    if any(c.effective and catalogue[(c.workflow_id, c.effective)].is_terminal for c in calls):
        return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def connectivity_rate(connected: int, total: int) -> str:
    """Exactly 4 decimals, computed in Decimal and rounded half-up (0.03125 -> 0.0313)."""
    return str((Decimal(connected) / Decimal(total)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# ======================================================================== linking
@dataclass
class Linked:
    calls: list[Call]
    orphan_events: int
    unknown_disposition_events: int


def link_events(conn: sqlite3.Connection, catalogue: dict[tuple[str, str], Disposition],
                lead_index: dict[tuple[str, str], int]) -> Linked:
    rows = conn.execute(
        "SELECT event_id, event_type, workflow_id, call_id, occurred_at, phone_hash, payload"
        " FROM events ORDER BY seq").fetchall()

    # 1. events that carry a phone link via (workflow_id, phone hash)
    event_lead: dict[str, int | None] = {}
    call_lead: dict[tuple, tuple[str, str, int]] = {}   # (wf, call_id) -> earliest linked phone event
    for event_id, etype, wf, call_id, at, phone_hash, _ in rows:
        if etype == "disposition.override":
            continue
        lead = lead_index.get((wf, phone_hash)) if phone_hash else None
        event_lead[event_id] = lead
        if lead is not None and call_id:
            cand = (at, event_id, lead)
            key = (wf, call_id)
            if key not in call_lead or cand < call_lead[key]:
                call_lead[key] = cand

    # 2. overrides have no phone: they link through their call to that call's lead
    for event_id, etype, wf, call_id, *_ in rows:
        if etype == "disposition.override":
            hit = call_lead.get((wf, call_id)) if call_id else None
            event_lead[event_id] = hit[2] if hit else None

    # 3. build calls from linked events
    orphans = unknown = 0
    agg: dict[tuple, dict] = defaultdict(lambda: {"initiated": [], "connected": False, "failed": False,
                                                  "sets": [], "overrides": []})
    for event_id, etype, wf, call_id, at, _, payload in rows:
        if event_lead[event_id] is None:
            orphans += 1
            continue
        if etype.startswith("disposition."):
            code = json.loads(payload).get("disposition_code")
            if not isinstance(code, str) or (wf, code) not in catalogue:
                unknown += 1
                continue
        if not call_id:
            continue
        a = agg[(wf, call_id)]
        if etype == "call.initiated":
            a["initiated"].append((at, event_id, event_lead[event_id]))
        elif etype == "call.connected":
            a["connected"] = True
        elif etype == "call.telephony_failed":
            a["failed"] = True
        elif etype == "disposition.set":
            a["sets"].append((at, event_id, code))
        elif etype == "disposition.override":
            a["overrides"].append((at, event_id, code))
        # call.ended and unknown event types don't affect any report

    calls = []
    for (wf, call_id), a in agg.items():
        if not a["initiated"]:
            continue            # a call only counts if its call.initiated is present and linked
        at, _, lead = min(a["initiated"])
        calls.append(Call(wf, call_id, lead, datetime.fromisoformat(at), a["connected"],
                          a["failed"], a["sets"], a["overrides"]))
    return Linked(calls, orphans, unknown)


# ======================================================================== reports
def write_reports(conn: sqlite3.Connection, catalogue: dict[tuple[str, str], Disposition],
                  out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    leads = {lid: {"workflow_id": wf, "batch_id": b, "external_ref": ref or "", "last4": l4}
             for lid, wf, b, ref, l4 in conn.execute(
                 "SELECT lead_id, workflow_id, batch_id, external_ref, phone_last4 FROM leads")}
    lead_index = {(wf, h): lid for lid, wf, h in
                  conn.execute("SELECT lead_id, workflow_id, phone_hash FROM leads")}
    linked = link_events(conn, catalogue, lead_index)

    def is_success(c: Call) -> bool:
        return bool(c.effective) and catalogue[(c.workflow_id, c.effective)].is_success

    calls_by_lead: dict[int, list[Call]] = defaultdict(list)
    for c in linked.calls:
        calls_by_lead[c.lead_id].append(c)

    # ---- 3. lead_report.csv (computed first; batch_summary reuses the buckets)
    lead_rows = []
    for lid, lead in leads.items():
        calls = sorted(calls_by_lead.get(lid, []), key=lambda c: (c.initiated_at, c.call_id))
        with_disp = [c for c in calls if c.effective]
        lead["bucket"] = lead_bucket(calls, catalogue)
        lead_rows.append((
            lead["workflow_id"], lead["external_ref"], lead["batch_id"], "+91******" + lead["last4"],
            lead["bucket"], len(calls), sum(c.connected for c in calls),
            n.ist_iso(calls[-1].initiated_at) if calls else "",
            with_disp[-1].effective if with_disp else "",
            _bool(any(is_success(c) for c in calls)),
            lid))                                  # tie-break only, dropped before writing
    lead_rows.sort(key=lambda r: (r[0], r[1], r[2], r[10]))
    _write_csv(out_dir / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"],
               [r[:10] for r in lead_rows])

    # ---- 1. batch_summary.csv: every ingested batch, even one where no row became a lead
    summary = {(wf, b): dict.fromkeys(BUCKETS, 0) for wf, b in conn.execute(
        "SELECT workflow_id, batch_id FROM ingested_files WHERE kind = 'leads'")}
    for lead in leads.values():
        summary.setdefault((lead["workflow_id"], lead["batch_id"]), dict.fromkeys(BUCKETS, 0))
        summary[(lead["workflow_id"], lead["batch_id"])][lead["bucket"]] += 1
    _write_csv(out_dir / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [(wf, b, sum(c.values()), *(c[k] for k in BUCKETS)) for (wf, b), c in sorted(summary.items())])

    # ---- 2. daily_overview.csv
    daily: dict[tuple[str, str], dict] = defaultdict(lambda: {"total": 0, "connected": 0, "success": set()})
    for c in linked.calls:
        d = daily[(c.workflow_id, n.ist_date(c.initiated_at))]
        d["total"] += 1
        d["connected"] += c.connected
        if is_success(c):
            d["success"].add(c.lead_id)
    _write_csv(out_dir / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
               [(wf, day, d["total"], d["connected"], connectivity_rate(d["connected"], d["total"]),
                 len(d["success"])) for (wf, day), d in sorted(daily.items())])

    # ---- 4. rejections.csv (no PII: only identifiers and the reason)
    _write_csv(out_dir / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               conn.execute("SELECT workflow_id, batch_id, row_number, reason FROM lead_rows"
                            " WHERE outcome = 'rejected' ORDER BY workflow_id, batch_id, row_number"))

    # ---- 5. data_quality.json
    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()[0] or 0

    quality = {
        "lead_rows_total": scalar("SELECT COUNT(*) FROM lead_rows"),
        "lead_rows_rejected": scalar("SELECT COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"),
        "rejections_by_reason": dict(conn.execute(
            "SELECT reason, COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"
            " GROUP BY reason ORDER BY reason")),
        "duplicate_lead_rows_merged": scalar("SELECT COUNT(*) FROM lead_rows WHERE outcome = 'merged'"),
        "field_warnings": scalar("SELECT SUM(field_warnings) FROM lead_rows WHERE outcome != 'rejected'"),
        "events_total": scalar("SELECT (SELECT COUNT(*) FROM events) + (SELECT COUNT(*) FROM event_rejects)"),
        "malformed_events": scalar("SELECT COUNT(*) FROM event_rejects WHERE kind = 'malformed'"),
        "duplicate_events_dropped": scalar("SELECT COUNT(*) FROM event_rejects WHERE kind = 'duplicate'"),
        "orphan_events": linked.orphan_events,
        "unknown_disposition_events": linked.unknown_disposition_events,
    }
    (out_dir / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")


def _bool(v: bool) -> str:
    return "true" if v else "false"


def _write_csv(path: Path, header: list[str], rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
