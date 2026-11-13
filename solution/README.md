# Assignment A solution: interaction ledger & reporting pipeline

See [`../SUBMISSION.md`](../SUBMISSION.md) for design, decisions and results.

```
solution/
├── main.py              entry point: python solution/main.py {ingest,report} ...
├── conftest.py          puts solution/ on sys.path for pytest
├── au_ledger/
│   ├── cli.py           argument parsing
│   ├── normalize.py     phone / date / decimal / enum / header / timestamp rules (pure)
│   ├── schema.py        schemas, disposition catalogue, row validation
│   ├── crypto.py        PII encryption + HMAC blind index
│   ├── db.py            SQLite DDL (append-only triggers), pragmas, key check
│   ├── ingest.py        per-file transactional ingest, lead merge, event dedupe
│   └── report.py        linking, effective disposition, buckets, the five files
└── tests/               pytest (run `pytest` from the repo root)
```
