# AI Usage Disclosure

## Tools used
Claude Code (Claude Opus), run in my repository.

## What I used them for
I used it heavily. To be direct: the AI wrote essentially all of the code in `solution/`.
- Explaining the two assignments in plain terms, comparing their scope and time cost, and
  helping me choose A.
- Writing the code: normalisation rules, schema/validation, the SQLite schema, ingestion,
  reporting, the CLI, and the test suite (94 tests).
- Profiling and fixing performance on the large dataset (226 s → 67 s).
- Building an independent "oracle" outside the repo: it replays the generator's RNG and writes
  the ground-truth reports, which we diffed against the pipeline on 6 seeds.
- Drafting `SUBMISSION.md`, including the decisions table.
- Git setup: committing and pushing to my repository.

## What I wrote or decided myself
- **Choosing Assignment A.** I chose it after comparing both on scope and risk: A has
  deterministic rules I can check, while B's difficulty is in concurrency and crash recovery.
- **Directing the work.** I set the goal (a correct, explainable core over extra stretch
  features), and asked for the reasoning behind decisions so that I could defend them, not
  just for the code.
- **SQLite vs Postgres.** I questioned the SQLite choice and asked why not Postgres. After
  weighing the trade-offs (the spec allows either, a zero-setup clean-clone run, the same logic
  either way), I decided to keep SQLite and treat Postgres as the production path described in
  `SUBMISSION.md`.
- **Submission handling.** I chose to follow the submission form's requirement for a publicly
  accessible repository (see below).
- The core design decisions (linking events at report time, Fernet + HMAC blind index for PII,
  per-file transactions with a SHA-256 guard, counts derived from tables) came from the AI. I'm
  owning them by understanding them well enough to explain and change them, not by claiming I
  came up with them.

## Where the AI was wrong or unhelpful
- **Repository visibility.** The AI went by the README ("private repository") and stopped
  before pushing because my repo was public. The submission form I'm actually using asks for a
  publicly accessible repo. I pointed this out, and we followed the form.
- **Performance.** The first version did one SQL `execute` per row and kept two secondary
  indexes on the ledger. It was correct, but the large dataset took ~226 s, over the 2-minute
  target. Profiling showed ~23 ms per commit from random index-page writes. The indexes were
  speculative (the report never uses them), so they were removed and the inserts batched.
- **A gap in the tests.** The first test fixture never had a call without a `call.initiated`
  event, so a planted bug ("count calls without initiated") passed every test. Mutation testing
  found this, and the fixture now covers that case.
- **Small slips.** An edit broke a line's indentation in `ingest.py` (caught by reading the file
  back), and a shell heredoc failed on Windows Git Bash.

## How I verified AI-generated code
- 94 pytest tests, including a hand-built dataset where every expected output number was worked
  out by hand.
- Mutation checks: bugs were planted on purpose, and each one makes a test fail.
- An independent oracle built from the generator's ground truth: an exact match on seeds 1, 7, 42,
  123, 2026, 99999 (small) and 42 (large, 132,903 leads).
- Comparisons of re-ingest and incremental vs. all-at-once ingest, and a byte search of the
  database file for plaintext names and phones.
- I'm reading through each module (`normalize` → `schema` → `ingest` → `report`) so I can walk
  through it and change it live in the interview.
