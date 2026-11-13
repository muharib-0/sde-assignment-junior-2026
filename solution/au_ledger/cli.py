"""Command line:

    python solution/main.py ingest data/drop_1 [data/drop_2 ...]
    python solution/main.py report --out reports/

Options: --db PATH (default: au_ledger.db, or $AU_LEDGER_DB)
         --schemas DIR (default: assignment-a-data/schemas in this repo)
PII secret: $AU_PII_SECRET (a development default is used if unset).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import db, ingest, report
from .crypto import PiiVault
from .schema import load_dispositions, load_schemas

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="au_ledger", description="Interaction ledger & reporting pipeline")
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("AU_LEDGER_DB", "au_ledger.db")))
    ap.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMAS)
    sub = ap.add_subparsers(dest="command", required=True)
    p_ing = sub.add_parser("ingest", help="ingest one or more drop directories, in the order given")
    p_ing.add_argument("drops", nargs="+", type=Path)
    p_rep = sub.add_parser("report", help="write the five report files")
    p_rep.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    vault = PiiVault()
    conn = db.connect(args.db, vault.fingerprint)
    started = time.perf_counter()
    try:
        if args.command == "ingest":
            schemas = load_schemas(args.schemas)
            for drop in args.drops:
                s = ingest.ingest_drop(conn, vault, schemas, drop)
                for note in s.notes:
                    print(f"warning: {note}", file=sys.stderr)
                print(f"{drop}: {s.files_ingested} file(s) ingested, {s.files_skipped} already ingested"
                      f" (skipped); {s.lead_rows} lead rows, {s.events} event lines")
        else:
            report.write_reports(conn, load_dispositions(args.schemas / "dispositions.csv"), args.out)
            print(f"reports written to {args.out}")
    except ingest.IngestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"done in {time.perf_counter() - started:.2f}s", file=sys.stderr)
    return 0
