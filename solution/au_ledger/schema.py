"""Workflow lead schemas, the disposition catalogue, and row validation."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import normalize as n


@dataclass(frozen=True)
class Field:
    name: str
    type: str                      # string | phone | date | decimal | enum
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()   # enum only
    default: str | None = None

    def parse(self, raw: str) -> str | None:
        """Normalised value, or None if `raw` (non-empty, trimmed) is invalid."""
        if self.type == "string":
            return raw
        if self.type == "phone":
            return n.normalize_phone(raw)
        if self.type == "date":
            return n.parse_date(raw)
        if self.type == "decimal":
            return n.parse_decimal(raw)
        if self.type == "enum":
            return n.parse_enum(raw, list(self.values))
        raise ValueError(f"unknown field type {self.type!r}")


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    fields: tuple[Field, ...]
    phone_field: str = field(init=False)
    reference_field: str = field(init=False)

    def __post_init__(self):
        # The identity rules only make sense with exactly one required phone field
        # and one reference field, so we check that up front instead of guessing.
        phones = [f for f in self.fields if f.type == "phone"]
        refs = [f for f in self.fields if f.reference]
        if len(phones) != 1 or not phones[0].required:
            raise ValueError(f"{self.workflow_id}: expected exactly one required phone field")
        if len(refs) != 1:
            raise ValueError(f"{self.workflow_id}: expected exactly one reference field")
        object.__setattr__(self, "phone_field", phones[0].name)
        object.__setattr__(self, "reference_field", refs[0].name)

    def map_headers(self, headers: list[str]) -> dict[str, int]:
        """field name -> column index. Unknown columns are ignored.
        If two columns match the same field, the first (left-most) one wins."""
        lookup: dict[str, str] = {}
        for f in self.fields:
            for label in (f.name, *f.aliases):
                lookup.setdefault(n.normalize_header(label), f.name)
        mapping: dict[str, int] = {}
        for idx, h in enumerate(headers):
            fname = lookup.get(n.normalize_header(h))
            if fname is not None and fname not in mapping:
                mapping[fname] = idx
        return mapping


@dataclass
class RowResult:
    reason: str | None                  # None means the row is accepted
    values: dict[str, str | None]       # normalised values (only meaningful if accepted)
    warnings: int


def validate_row(schema: WorkflowSchema, raw: dict[str, str]) -> RowResult:
    """Walk the fields in schema order; the first failure is the row's single reason.

    raw: field name -> trimmed cell value ('' if the column is absent).
    """
    values: dict[str, str | None] = {}
    warnings = 0
    for f in schema.fields:
        cell = raw.get(f.name, "")
        if cell == "":
            if f.required:
                return RowResult(f"missing_required:{f.name}", {}, 0)
            values[f.name] = None
            continue
        parsed = f.parse(cell)
        if parsed is None:
            if f.type == "phone":
                return RowResult("invalid_phone", {}, 0)
            if f.required:
                # Not in the spec (no current schema has a required non-phone field
                # that can fail parsing); rejecting is the safe choice.
                return RowResult(f"invalid_required:{f.name}", {}, 0)
            warnings += 1
        values[f.name] = parsed
    return RowResult(None, values, warnings)


# ---------------------------------------------------------------- loading
def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    out = {}
    for p in sorted(schema_dir.glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        fields = tuple(
            Field(name=f["name"], type=f["type"], required=bool(f.get("required")),
                  pii=bool(f.get("pii")), reference=bool(f.get("reference")),
                  aliases=tuple(f.get("aliases", ())), values=tuple(f.get("values", ())),
                  default=f.get("default"))
            for f in doc["fields"])
        out[doc["workflow_id"]] = WorkflowSchema(doc["workflow_id"], fields)
    return out


@dataclass(frozen=True)
class Disposition:
    is_terminal: bool
    is_success: bool


def load_dispositions(path: Path) -> dict[tuple[str, str], Disposition]:
    """(workflow_id, code) -> Disposition."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {
            (r["workflow_id"].strip(), r["code"].strip()): Disposition(
                r["is_terminal"].strip().lower() == "true",
                r["is_success"].strip().lower() == "true")
            for r in csv.DictReader(fh)
        }
