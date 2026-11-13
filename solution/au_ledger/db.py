"""SQLite schema and connection handling.

Tables, in the order data flows through them:

  ingested_files  one row per input file we have processed (idempotency guard)
  leads           current state of each lead, one row per (workflow_id, phone)
  lead_rows       every non-blank CSV row and what happened to it (created /
                  merged / rejected). Rejections and lead counters are read from here.
  events          the append-only interaction ledger: every well-formed,
                  first-seen event, *including* orphans (they may link later)
  event_rejects   lines we did not put in the ledger (malformed / duplicate)

Nothing is a running counter. Every number in data_quality.json is a COUNT over
these tables, which is why re-running ingest can't double-count anything.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_files (
    file_key     TEXT PRIMARY KEY,          -- 'leads/<workflow>/<batch>' or 'events/<stem>'
    kind         TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id  TEXT,                      -- leads only
    batch_id     TEXT,                      -- leads only
    sha256       TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    ingested_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id      INTEGER PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,             -- HMAC blind index of canonical phone
    phone_last4  TEXT NOT NULL,             -- enough for the masked report column
    batch_id     TEXT NOT NULL,             -- batch that created the lead; never changes
    external_ref TEXT,
    pii_enc      BLOB NOT NULL,             -- Fernet ciphertext of all pii=true fields
    attributes   TEXT NOT NULL,             -- JSON of the non-PII fields
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS lead_rows (
    workflow_id    TEXT NOT NULL,
    batch_id       TEXT NOT NULL,
    row_number     INTEGER NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('created', 'merged', 'rejected')),
    reason         TEXT,                    -- set iff outcome = 'rejected'
    lead_id        INTEGER REFERENCES leads(lead_id),
    field_warnings INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, batch_id, row_number),
    CHECK ((outcome = 'rejected') = (reason IS NOT NULL)),
    CHECK ((outcome = 'rejected') = (lead_id IS NULL))
);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,       -- ingestion order
    event_id     TEXT NOT NULL UNIQUE,      -- dedupe key
    event_type   TEXT NOT NULL,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  TEXT NOT NULL,             -- UTC ISO-8601, fixed width => sortable as text
    phone_hash   TEXT,                      -- NULL if absent (overrides) or not a valid mobile
    payload      TEXT NOT NULL,             -- JSON; never contains the customer's phone
    source_file  TEXT NOT NULL,
    line_no      INTEGER NOT NULL
);
-- Deliberately no secondary indexes on events: reporting is one sequential scan,
-- and indexes on random keys (phone_hash) made bulk ingest ~2x slower. Add
-- (workflow_id, phone_hash) / (workflow_id, call_id) when something needs point lookups.

CREATE TABLE IF NOT EXISTS event_rejects (
    source_file  TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('malformed', 'duplicate')),
    event_id     TEXT,                      -- for duplicates; malformed lines are not kept (may hold PII)
    PRIMARY KEY (source_file, line_no)
);

-- The ledger is append-only: the database itself refuses edits and deletes.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
"""


def connect(db_path: Path, key_fingerprint: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)   # we manage transactions explicitly
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL + NORMAL is still crash-safe (a committed file is never half-applied); at
    # worst the very last commit is lost on power failure, and re-running ingest redoes it.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -262144")      # 256 MB page cache (default is ~2 MB)
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.executescript(SCHEMA_SQL)
    _check_key(conn, key_fingerprint)
    return conn


def _check_key(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Refuse to mix PII keys in one database: a different key would give different
    phone hashes, so the same customer would silently turn into a 'new' lead."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('pii_key_fingerprint', ?)", (fingerprint,))
    elif row[0] != fingerprint:
        raise SystemExit("error: AU_PII_SECRET does not match the key this database was created with")
