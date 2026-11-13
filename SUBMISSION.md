# Submission

## Candidate
- **Name:** Fayaz Hussain
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~3h understanding the spec and the design decisions;
  building, testing and verification were done together with Claude Code (see `AI_USAGE.md`);
  plus ~2h reviewing the code and preparing for the walkthrough

## How to run
```bash
# setup (Python 3.11+)
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# generate data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# pipeline (default DB: ./au_ledger.db; override with --db or $AU_LEDGER_DB)
export AU_PII_SECRET='any-long-random-string'   # optional locally; a dev default is used if unset
python solution/main.py ingest data/drop_1
python solution/main.py ingest data/drop_2      # or both at once: ingest data/drop_1 data/drop_2
python solution/main.py report --out reports/

# tests (94, ~5 s)
pytest
```
To start from scratch, delete `au_ledger.db*`.

## Design

**Flow.** `ingest <drop>` reads lead CSVs (workflow dirs, then files in filename order) and event
JSONL files into SQLite. `report` derives all five files from the database. Code layout
(`solution/au_ledger/`):

| module | responsibility |
|---|---|
| `normalize.py` | pure value rules: headers, phone, date, decimal, enum, timestamps |
| `schema.py` | loads schemas and the disposition catalogue; `validate_row` (first failure in schema order wins) |
| `crypto.py` | PII encryption + blind index |
| `db.py` | DDL, pragmas, key-fingerprint check |
| `ingest.py` | per-file transactional ingestion, lead merging, event dedupe |
| `report.py` | event linking, effective disposition, buckets, the five outputs |

**Schema.**

```
ingested_files  file_key PK ('leads/<wf>/<batch>' | 'events/<stem>'), sha256, ...   idempotency guard
leads           lead_id PK, UNIQUE(workflow_id, phone_hash), phone_last4, batch_id,
                external_ref, pii_enc BLOB, attributes JSON                         current state
lead_rows       PK(workflow_id, batch_id, row_number), outcome created|merged|rejected,
                reason, lead_id, field_warnings                                     per-row history
events          seq PK, event_id UNIQUE, event_type, workflow_id, call_id,
                occurred_at (UTC), phone_hash, payload JSON                         THE LEDGER
event_rejects   PK(source_file, line_no), kind malformed|duplicate, event_id
```

Three ideas carry the design:

1. **The ledger holds facts, not interpretations.** Every well-formed, first-seen event goes into
   `events`, *including orphans* and unknown dispositions. Linking an event to a lead, picking the
   effective disposition, and bucketing all happen **at report time**, against the leads that
   exist *now*. So an event that arrived before its lead counts automatically once the lead shows
   up, and nothing is ever re-processed or updated. The ledger is append-only, and SQLite triggers
   reject `UPDATE`/`DELETE` on `events`. Overrides are just more rows.
2. **No counters.** Every number in `data_quality.json` is a `COUNT` over `lead_rows`, `events`
   and `event_rejects`. Re-running anything can't double-count.
3. **One transaction per file**, which includes the `ingested_files` row. A file is either fully
   applied and recorded, or not at all. Re-ingesting a file with the same name and SHA-256 is a
   no-op. The same name with different content is refused with an error, not silently merged.

**PII.** Fields marked `pii` (`customer_name`, `phone`) are stored only inside `pii_enc`, which
is Fernet ciphertext (AES-128-CBC + HMAC-SHA256). Fernet is randomised, so you can't join on it. Phones
are therefore *also* stored as a **blind index**: `HMAC-SHA256(key, '+91XXXXXXXXXX')`. It's
deterministic, so event phones and lead phones match. It's keyed, so it can't be reversed by
hashing all ~4×10⁹ possible Indian mobiles, which a plain SHA-256 would allow. Event phones are
hashed the same way and never stored raw. Malformed event lines aren't stored at all, since a
truncated line can still contain a phone. Both keys are derived from `AU_PII_SECRET`, and the DB
stores the key's fingerprint, so a different secret is refused. Otherwise every customer would
silently become a "new" lead. The last 4 digits are stored in clear for `phone_masked`.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Link events at report time, not ingest time | Makes late leads, re-runs and drop order trivially correct; the ledger never changes | Link at ingest and re-link orphans when leads arrive: more code, and mutates the ledger |
| Store orphans and unknown-code events in the ledger | They're real facts; an orphan may link later. They're excluded at report time | Quarantine table: would need moving rows out of quarantine later |
| Leads table is mutable (current state); history is in `lead_rows` | The spec's merge rule is "last write wins", so a lead *is* a mutable record. `lead_rows` keeps an auditable trail of every row | Event-sourced leads (rebuild from all rows): purer, slower, not needed |
| Fernet + HMAC blind index for PII | Need both recoverability (to actually call people) and joinability | Hash only: can't recover the number to dial. Encrypt only: can't match |
| `rejections_by_reason` lists only reasons that occurred, sorted by key | A `Counter`; zero-valued keys for every possible field felt arbitrary | Include every `missing_required:<field>` with 0 |
| `connectivity_rate` uses Decimal, rounded **half-up** | Exact, and what a human expects (0.03125 → 0.0313) | `f"{x:.4f}"` on a float does round-half-even on the binary value (→ 0.0312). They only differ on exact ties |
| Override links via `(workflow_id, call_id)` to the lead of the **earliest** linked phone-bearing event of that call | Spec says "the lead of that call's other events"; in practice there's one. The earliest is deterministic | Refuse if a call's events point to different leads |
| A call's time is its earliest `call.initiated`; event ties break by `event_id`, call ties by `call_id` | Deterministic output | — |
| An event is linked independently of its call; `connected`/`telephony_failed`/dispositions of a call count only if that event is itself linked | "Orphans are kept out of every report" applies per event | — |
| Lines with no `event_type` or an unparseable `occurred_at` are counted as **malformed** | They can't be placed in any report. The spec's definition (bad JSON / no `event_id`) is a subset. The generator never produces these | Store them and silently ignore |
| Disposition codes match the catalogue exactly (case-sensitive) | `RENEWD` / `call_back` in the data are clearly not meant to match | Fuzzy or case-folded matching |
| Invalid phone always rejects (`invalid_phone`); a required non-phone field that fails parsing would reject as `invalid_required:<field>` | Phone is the identity; the spec lists `invalid_phone` as a rejection. The second case can't happen with current schemas | — |
| Schema load fails unless there is exactly one required `phone` field and one `reference` field | Identity and `external_ref` depend on it; fail loudly instead of guessing | — |
| Headers: if two columns map to the same field, the left-most wins; trailing separators are trimmed | Deterministic and lenient | Reject the file |
| Commas in decimals are just removed | Also accepts Indian grouping `1,00,000` | Validate Western grouping strictly |
| Defaults are applied when the lead is saved (after merge), only to null fields | Same result as applying at read time, and the stored lead is complete | Apply at read time |
| `batch_summary` includes every ingested batch, even one with 0 leads | A batch that was fully rejected is exactly what ops needs to see | Only batches that have leads (identical on generated data) |
| Lead workflow comes from the directory name; a dir with no schema is skipped with a warning and *not* marked ingested | Adding the schema later and re-running picks it up | Fail the whole drop |
| SQLite, no secondary indexes on `events` | Reporting is one sequential scan. Random-key indexes made bulk ingest ~2× slower (measured) | Postgres. Indexes on `(workflow_id, phone_hash)` / `(workflow_id, call_id)` once we serve point lookups |

## Failure modes considered
- **Re-running a drop / overlapping drops:** file-level SHA-256 guard. Event-level dedupe by
  `event_id` also catches the same event delivered in two different files.
- **Crash mid-ingest:** one transaction per file, so the partial file is rolled back and the
  re-run redoes it. Tested by injecting an exception after a file's rows are written but before
  commit, then checking the reports against a clean run.
- **Changed file under the same name:** refused (exit 1). It needs a new batch name or manual
  intervention.
- **Wrong or missing PII key:** a different key is refused via the stored fingerprint. Missing
  key: a warning plus the dev default, which is fine locally and *not* fine in production.
- **Dirty data:** every class in the README is handled (six phone formats, bad phones, missing
  fields, cross-batch duplicates, renamed/re-ordered/extra columns, blank rows, mixed dates and
  currencies, out-of-order events, duplicate events across drops, truncated lines, unknown
  numbers, early events, phone-less overrides, unknown codes). Also: UTF-8 BOM, short rows,
  non-object JSON lines.
- **Not handled:** concurrent `ingest` processes on one DB. SQLite's `BEGIN IMMEDIATE` serialises
  them per file, which is safe but not designed for. Key rotation (it would need re-encrypting and
  re-indexing every lead). Deleting a customer for a right-to-erasure request. Files that don't
  fit in memory (one file is read fully; the large set's files are 5,000 lines each).

## Testing
`pytest` runs 94 tests in about 5 seconds:
- `test_normalize.py`: every phone format plus invalid ones, all four date formats, impossible
  dates, currencies, enums, header normalisation, the three timestamp forms, and IST dates
  crossing midnight.
- `test_rules.py`: first-failure-wins validation, warnings vs. rejections, effective disposition
  (an override beats a later set; the latest override wins by time, not arrival order), every
  bucket branch, connectivity-rate rounding.
- `test_pipeline.py`: a hand-built two-drop dataset where every expected output row was worked out
  by hand. Also covers: an orphan that gets linked once its lead arrives, merge semantics,
  re-ingest changing nothing, incremental == all at once, a changed file being refused, crash
  rollback, append-only triggers, no plaintext PII in the DB file, a wrong key being refused, and
  incremental == all at once on a real generated seed.
- Mutation-checked: I planted bugs (earliest override wins; naive timestamps as UTC; nulls
  overwrite on merge; calls without `call.initiated` counted) and each one makes a test fail. The
  last one survived at first, which is how I found and filled that gap in the fixture.
- **Outside the repo:** I verified correctness against an independent oracle. It replays the
  generator with the same seed, records the ground truth it injected (which rows were defective,
  which calls happened, the true dispositions), and writes the expected reports *without parsing
  any file*. The output matches exactly on seeds 1, 7, 42, 123, 2026 and 99999 (small) and seed 42
  (large, 132,903 leads). It isn't committed because it depends on the generator's internals.
- Not tested: concurrent ingest processes, and running on Linux/macOS. Everything was run on
  Windows, but the code only uses `pathlib` and the commands are plain Python.

## Results
Timings (Windows laptop, Python 3.13, SQLite, `AU_PII_SECRET` set):

| dataset | ingest drop_1 | ingest drop_2 | report | total |
|---|---|---|---|---|
| small (seed 42: 2,057 lead rows, 8,745 event lines) | 0.6 s | 0.5 s | 0.2 s | ~1.3 s |
| large (seed 42: 143,990 lead rows, 632,602 event lines) | 27.3 s | 27.6 s | 11.2 s | **~67 s** |

The first large run took ~226 s. Profiling showed it wasn't Python but SQLite: one `execute`
per row (~160 µs each), then about 23 ms per `COMMIT` flushing randomly-keyed index pages. The
fixes were `executemany`, a bigger page cache with `synchronous=NORMAL` (still crash-safe under
WAL), and dropping two speculative secondary indexes. Output was byte-identical before and after.

Surprises in the data (seed 42, small):
- Events really do arrive early: after drop_1 there are 1,123 orphan events; after drop_2 only
  462. So 661 events were linked late.
- 19 disposition events have codes not in the catalogue (`RENEWD`, `call_back`, `unknown`), but
  `unknown_disposition_events` is 18, because one is on an orphan and the orphan check comes first.
- 40 of the 57 merged duplicates are inside the **same** batch, not just across batches.
- Every rejected lead's calls become permanent orphans, because the lead never exists.
- The README's examples (`0.5625`, `2026-10-14T17:55:24+05:30`) match seed 42's output exactly.

## If I had another day
1. Report aggregation in SQL (CTEs + window functions for effective disposition) to compare with
   the Python version. The Python version is easier to read and test, which is why I started there.
2. Truly incremental reporting: materialise per-call facts and only recompute calls touched by
   new events or new leads, instead of scanning the whole ledger (11 s on the large set).
3. Key rotation (versioned keys in `meta`, re-encrypt in the background) and an erasure path.
4. Production shape for ~100k leads/day: Postgres; each drop arriving in object storage triggers
   an ingest job (Airflow/Temporal/cron + queue); the per-file transaction and SHA guard already
   make retries and backfills safe (re-run the job). Reports run after ingest and are written
   atomically (temp dir + rename). Data-quality counts get alerts (e.g. orphan rate > 10%).
   Secrets come from KMS.
