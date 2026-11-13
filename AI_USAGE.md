# AI Usage Disclosure

> **TODO (candidate):** this is a factual draft of how the AI was used in this session. Edit it
> so it's true *for you*, especially the "myself" section. Don't claim anything you didn't do.

## Tools used
Claude Code (Claude Opus), run in my repository.

## What I used them for
- Explaining the two assignments and helping me choose A.
- Writing essentially all of the code in `solution/`: normalisation rules, schema/validation,
  SQLite schema, ingestion, reporting, CLI, and the test suite.
- Profiling and fixing performance on the large dataset (226 s → 67 s).
- Building an independent "oracle" outside the repo: it replays the generator's RNG and writes
  the ground-truth reports, which we diffed against the pipeline on 6 seeds.
- Drafting `SUBMISSION.md`.

## What I wrote or decided myself
_TODO (candidate): be specific and honest. For example: which design decisions you reviewed,
challenged or changed; which parts you rewrote; edge cases you thought of; what you checked
line by line before submitting._

## Where the AI was wrong or unhelpful
These happened in the session and were caught by measurement or tests, not by trusting the output:
- **Performance:** the first version did one SQL `execute` per row and kept two secondary
  indexes on the ledger. Correct, but the large dataset took ~226 s, over the 2-minute
  target. Profiling showed ~23 ms per commit from random index-page writes. The indexes were
  speculative (the report never uses them), so they were removed and inserts batched.
- **A gap in the tests:** the first test fixture never had a call without a `call.initiated`
  event, so a planted bug ("count calls without initiated") passed all tests. Found by
  mutation testing; the fixture now covers it.
- **Small slips:** an edit broke a line's indentation in `ingest.py`, caught by reading the file
  back, and a shell heredoc failed on Windows Git Bash.
- _TODO (candidate): add anything you yourself rejected or corrected._

## How I verified AI-generated code
- 94 pytest tests, including a hand-built dataset where every expected number was worked out by hand.
- Mutation checks: planted bugs, confirmed each one makes a test fail.
- Independent oracle built from the generator's ground truth: an exact match on seeds 1, 7, 42,
  123, 2026, 99999 (small) and 42 (large).
- Re-ingest / incremental vs. all-at-once comparisons, and a byte search of the DB file for
  plaintext names and phones.
- _TODO (candidate): reading the code yourself, and being able to change it live, is the
  verification that matters most in the interview._
