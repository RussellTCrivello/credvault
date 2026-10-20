# CredVault — Import Reliability Repair & Full-Suite Regression Report

**Date:** 2026-10-12
**Branch:** `arena/527aaf46-credvault` (from `main` @ `3367b2d`)
**Scope:** Work packages A–F. No production schema weakened; no data touched.
Migration `vault.0006_url_domain_search_index` is additive (one new index).

**Verification environment note:** this repair was executed and verified on
Linux (Debian 12, Python 3.11.2, Django 5.2.18, psycopg 3.3.6, PostgreSQL
18.6, database collation `C.UTF-8`) — the reporting machine (Windows,
Python 3.11, locale-collated PostgreSQL cluster) was not available here.
Both Windows-only root causes (package C: no `fork` start method; package
D: locale collation) were fixed *by construction* to be
platform/collation-independent, and the Windows code paths are exercised on
Linux by the new forced-`spawn` tests. The four verification commands must
be re-run on the Windows machine for final confirmation (nothing in the fix
is Linux-specific).

---

## 1. Root-cause report for every original failure

Reported baseline (Windows): `vault.tests_import_reliability` 35 tests,
1 failure + 1 error; `vault` 148 tests, 10 failures + 2 errors + 2 skipped.
Reproduced in this sandbox (Linux/`C.UTF-8`): reliability 35 tests,
1 failure + 1 error (identical); vault 148 tests, 6 failures + 1 error +
2 skipped — the remaining Windows-only entries are explained below.

### A. `RecoveryTests.test_recover_interrupted_job` — `total_committed` 0, expected 1 (P0)

**Root cause (product bug).** `recover_job()` recalculates the counters from
the authoritative ledger and assigns them to the in-memory `ImportJob`
("Step 4"), but every settling branch afterwards persists only its own
fields: `mark_completed()` saves with
`update_fields=['status','stage','completed_at','last_activity_at']`. The
recalculated counters never reached the database on the success/with-errors
paths, so the test's `refresh_from_db()` still saw the stale zeros. (The
resume/pause branches used a full `job.save()` and did persist — the
behavior was inconsistent between branches.) Not a fixture problem, not a
transaction-boundary problem: purely stale persistence.

**Fix.** `recover_job()` Step 4 now saves the counters immediately
(`total_discovered`, `total_committed`, `total_skipped`, `total_failed`,
`last_activity_at` via `update_fields`) before any state transition, so all
branches persist them exactly once.

**Regression tests.** The original test now passes unchanged, plus
`test_recovery_persists_counters_and_is_idempotent` (counters survive
`refresh_from_db()`; a second `recover_job()` run changes nothing: same
counters, same 3 ledger rows, `recovery_attempts == 2`, outcome `success`
both times).

### B. `test_reconciliation_detects_missing_accounts` — FK rejects `account_id=99999` (P0, reported "duplicated")

**Root cause (invalid test fixture; constraint behaving correctly).** The
test inserted a ledger row pointing at a non-existent account. PostgreSQL
rightly rejects that — the FK is the first line of defense and must stay.
The "duplication" of the failure on Windows is explained by Django's
PostgreSQL schema: FKs are created `DEFERRABLE INITIALLY DEFERRED`, so
inside a `TestCase` transaction the invalid insert *succeeds* and the
violation surfaces later, at teardown's `check_constraints()`
(`SET CONSTRAINTS ALL IMMEDIATE`) — producing a second failure/error entry
on top of the test's own outcome.

**What reconciliation is contractually supposed to detect.** A committed
ledger row whose account row no longer exists. Through supported paths this
state is unreachable: inserting a dangling `account_id` is rejected, and
deleting an `Account` NULLs the link (`on_delete=SET_NULL`) — and a
committed row with a NULL account link is a tolerated state elsewhere in
the suite (`test_verify_clean_job`, `test_recover_interrupted_job`). The
dangling-id branch in `recover_job()` is therefore *defensive* handling for
states that can only arise outside the ORM (manual SQL surgery, partial
restore).

**Fix (test strategy, constraints untouched).** The rewritten test:
1. `test_foreign_key_rejects_orphaned_account_reference` proves the
   production FK rejects the orphaned insert (`IntegrityError` under
   `SET CONSTRAINTS ALL IMMEDIATE` — production autocommit semantics —
   inside a savepoint; row does not persist).
2. `test_reconciliation_detects_missing_accounts` creates a *real* account
   and a committed ledger row, then simulates the impossible state **inside
   the test's own transaction on the disposable test database only**:
   `SET CONSTRAINTS ALL IMMEDIATE` (flush queued checks), drop the FK
   constraint, raw-`DELETE` the account (bypassing Django's SET_NULL
   collector), and re-add the FK `NOT VALID` afterwards. `TestCase`
   rollback restores the original constraint at teardown — migrations and
   production schema are not touched. It then asserts `recover_job()`
   reports `missing_accounts == 1`, lists the orphaned id, defensively
   reclassifies the row (`failed` / `reconciliation_missing`), completes
   the job `completed_with_errors` with outcome `partial`, and persists the
   recalculated counters (also covering fix A on this branch).

Also corrected in passing: `reconciliation_details['previously_committed']`
now snapshots the committed count *before* reclassification (the lazy
queryset used to recount afterwards and always reported 0).

### C. `_parse_pool()` used `multiprocessing.get_context("fork")` (P1, Windows error)

**Root cause.** Windows offers only the `spawn` start method;
`get_context("fork")` raises `ValueError: cannot find context for 'fork'`.
Production code masked this (`worker_count()` forced 1 on non-POSIX →
inline parsing), but any explicit `workers>1` — the setting
`VAULT_IMPORT_WORKERS` or `BulkImportSession(workers=2)` in
`test_parse_workers_store_the_same_records_as_inline_parsing` — crashed on
Windows. That test is the 2nd Windows error.

**Fix.**
- New `_parse_context()`: prefers `fork` where the platform provides it
  (unchanged Linux behavior), falls back to `spawn` elsewhere. Workers only
  run the module-level `_parse_chunk` over plain `str`/`bytes`, so both
  contexts parse identically; spawn children re-import `vault.importer`
  without touching the database, `manage.py` is `__main__`-guarded, and
  only the parent ever creates a pool → no recursive process creation.
  Cleanup (`pool.close()/join()` in `close()`, also via `finally` in
  `_import_one_file_session`) and failure propagation (`AsyncResult.get()`
  re-raises worker exceptions in the parent) are unchanged for both
  contexts.
- `worker_count()` now honors an explicit `VAULT_IMPORT_WORKERS > 0` on
  **every** platform (spawn pool on Windows); the *default* stays 1 on
  non-POSIX and `min(4, cpu_count)` on POSIX, so nothing changes for
  unconfigured deployments. Frozen Windows executables need
  `multiprocessing.freeze_support()` (documented in README).

**Tests.** `test_parse_pool_prefers_fork_and_falls_back_to_spawn` (context
selection), `test_spawn_workers_store_the_same_records_as_inline_parsing`
(forces `spawn` on any OS: real spawn pool, 60 records identical to inline
parsing, pool closed), `test_worker_count_honors_explicit_configuration_everywhere`
(simulated `os.name == 'nt'`). The existing inline-vs-worker equivalence
test is unchanged and passes.

### D. Search/export regressions — `q=example.com` / `q=scale.example` returned nothing (P1, Windows-only failures)

Affected on Windows: `test_no_records_displayed_until_queried`,
`test_search_by_domain_email_url_and_password_is_subsecond` (both
`example.com` subcases), `test_export_exports_all_matching_results_beyond_first_page`
(0 of 150 — the search itself matched 0; export reuses the *same*
`filter_accounts()` queryset and streams every match, never just page 1).

**Root cause.** The "domain also matches paths under the domain" tier is
implemented as B-tree range scans
`lower(name) >= 'https://example.com/' AND lower(name) < 'https://example.com/' || U+FFFF`.
That identity holds only under **byte-order (C) collation**. The Windows
installer initializes clusters with a locale collation (e.g.
`English_United States.1252`), where the `U+FFFF` upper bound does not
reliably sort above ordinary text — the ranges collapse and match nothing.
This sandbox's cluster is `C.UTF-8` (byte order), which is why these three
tests pass here pre-fix and fail on Windows. The evidence pattern matches
exactly: `field=domain` (which has an `icontains` fallback) survived on
Windows; the two no-fallback paths (`q=...` default and `field=url` with a
domain-like value) failed; browse, search and export all consume
`filter_accounts()` and all saw 0.

**Fix (semantics preserved, still index-backed).**
- Migration `0006` (additive `RunSQL`, same pattern as migration `0003`):
  `CREATE INDEX vault_url_name_lower_c_idx ON vault_url (lower(name) COLLATE "C")`.
- `_match_url_ids()` range clauses now compare
  `Collate(Lower('name'), 'C')` — byte order regardless of the database
  collation — with upper bound `U+10FFFF` (the maximum code point, also
  covering astral characters that `U+FFFF` missed even under C).
  Exact-match clauses keep using the existing `lower(name)` index; the
  `force_domain` `icontains` fallback is retained.
- Verified: `EXPLAIN` shows `BitmapOr` of `vault_url_name_lower_idx`
  (equality) + range scans on `vault_url_name_lower_c_idx`; a 30k-row
  synthetic probe matched exactly the intended URL. Performance assertions
  (`< 1.0 s` per query) were kept and pass.

Documented search semantics are untouched: quick search stays exact,
`domain` still matches paths under the domain, advanced search stays
substring, and exports stream the entire matching queryset.

### E. Import redirect expectations `/records/` vs `/import/jobs/<job-id>/` (P1, 5 failures)

**Root cause (intended behavior change; tests not updated).** The durable
import integration deliberately redirects after a non-streaming import: one
job → its detail page (`/import/jobs/<job-id>/`), several jobs (one per
listed path) → the job list (`/import/jobs/`). This is the point of the
merge — the user lands where the job's state, stage, progress bar and
counters are inspectable — and is recorded in `FINAL_SUMMARY.md`
("Redirects to import job detail/list after completion"). The streaming
progress flow (`X-Import-Progress: 1`) keeps its own contract (the NDJSON
`done` event carries `redirect: /records/` for the browser JS).

**Decision: the job pages are the intended destination.** The five tests in
`vault/tests.py` now assert the job detail/list redirect *and* follow it to
verify the job is visible with inspectable state/progress (status badge
"Completed", `100.0%`, source name, job ids/links on the list page). The
choice is documented in README ("Import jobs and recovery") and in the view
docstring.

**Related product bug found while updating these tests (fixed).** The
uploaded-files branch ran `session.run()` once *per file* on one job; every
run restarts line numbering at 0, so per-`(job, position)` idempotency keys
collided and `bulk_create(ignore_conflicts=True)` silently dropped the
ledger rows of every file after the first — undercounting the job. All
uploaded files now go through **one** session (positions 1..N across
files); `test_import_page_accepts_several_files_at_once` asserts ledger
positions `[1, 2, 3]`.

### F. Full-suite verification

See section 4. Additionally hardened during this package (needed for
"resume the same job and verify exact reconciliation" to be true at all):

- **Resume was dead code.** `recover_job()` only resumed when the ledger
  contained `pending` rows — but the engine never writes `pending` rows
  (ledger entries are created at flush time), so after a crash the
  unaccounted *tail* of the source was silently settled as "success" and
  lost. Recovery now resumes whenever a lines factory is supplied and the
  source has positions the ledger has not accounted for.
- **Position fidelity on replay.** Resume used to renumber remaining lines
  from `max(processed)+1`, mislabeling `source_position` (and idempotency
  keys) whenever ledger rows were lost mid-stream. The session now accepts
  `PositionedLine(position, line)` items and keeps the original positions.
- **`_durable_flush_batch` atomicity.** The ledger write happened *after*
  the accounts' commit, contradicting the documented guarantee ("ledger
  and data in ONE atomic transaction"); a kill in that window left
  committed accounts with no ledger rows (resume would mislabel them as
  duplicates). Ledger creation now runs inside the same `atomic()` block —
  accounts and ledger commit or roll back together (`synchronous_commit`
  stays ON for this path).
- **`recovery_completed_at`** is now set when recovery ends in a resume;
  the resume view passes `lines_factory_for_job(job)` so the **Resume**
  button actually re-reads `source_path` (file or folder, with the
  recorded `--ext` filter now persisted in `job.options`).
- **`can_resume()`** recognizes a `processing` job whose lease has expired
  (abandoned by a crashed worker) as resumable — matching
  `discover_interrupted_jobs()` and the lease design.

---

## 2. Changed files (minimal, additive where possible)

| File | Change |
| --- | --- |
| `vault/import_engine.py` | A/F: persist recalculated counters in `recover_job()`; snapshot `previously_committed` before reclassification; resume on unaccounted positions (not just `pending` rows); `PositionedLine` replay with original positions; `recovery_completed_at` after resume; ledger write moved inside the batch transaction; `lines_factory_for_job()`; persist `ext` in job options. |
| `vault/importer.py` | C: `_parse_context()` (fork → spawn fallback); `_parse_pool()` uses it; `worker_count()` honors explicit `VAULT_IMPORT_WORKERS` on every platform (defaults unchanged); docstrings updated. |
| `vault/views.py` | D: `_match_url_ids()` range scans under `COLLATE "C"` with `U+10FFFF` bound; E: single session for all uploaded files; resume view passes the lines factory; redirect behavior documented in the view docstring. |
| `vault/models.py` | F: `ImportJob.can_resume()` treats an expired-lease `processing` job as resumable. |
| `vault/migrations/0006_url_domain_search_index.py` | D (new): additive `vault_url_name_lower_c_idx` on `lower(name) COLLATE "C"`. |
| `vault/tests.py` | E: five import tests assert job detail/list + inspectable state/progress; C: three new worker tests (context selection, forced-spawn equivalence, worker-count policy). |
| `vault/tests_import_reliability.py` | A/B/F: FK-rejection test; reconciliation test rewritten with test-isolated simulation; counter-persistence/idempotency regression test; new `CrashRecoveryTests` (failure injection + resume-via-view). |
| `README.md` | Documents the redirect decision, recovery/resume semantics, collation-independent domain search (migration 0006), and Windows parse-worker behavior. |
| `IMPORT_REPAIR_REPORT.md` | This report. |

No test expectation was weakened to hide a regression: performance
assertions (`< 1.0 s`), exact/partial search semantics, export-completeness
and fingerprint/dedup assertions are all intact; the only changed
expectations are the five redirect targets (intended UX, documented) and
the invalid FK fixture (replaced by a valid strategy plus an explicit
FK-rejects-orphan test).

## 3. Regression tests demonstrating each correction

- **A:** `test_recover_interrupted_job` (original, now passing),
  `test_recovery_persists_counters_and_is_idempotent`.
- **B:** `test_foreign_key_rejects_orphaned_account_reference`,
  `test_reconciliation_detects_missing_accounts` (rewritten).
- **C:** `test_parse_pool_prefers_fork_and_falls_back_to_spawn`,
  `test_spawn_workers_store_the_same_records_as_inline_parsing`,
  `test_worker_count_honors_explicit_configuration_everywhere`,
  plus the pre-existing `test_parse_workers_store_the_same_records_as_inline_parsing`.
- **D:** `test_no_records_displayed_until_queried`,
  `test_search_by_domain_email_url_and_password_is_subsecond`,
  `test_export_exports_all_matching_results_beyond_first_page` (all
  unchanged expectations, now collation-independent).
- **E:** the five updated import tests (redirect + job visibility +
  progress inspection + multi-file ledger positions `[1,2,3]`).
- **F (failure injection):**
  `CrashRecoveryTests.test_kill_after_commit_before_ack_resumes_without_duplicates`
  — worker killed (BaseException, past the session's `except Exception`,
  exactly like SIGKILL/TerminateProcess) after the 2nd committed batch and
  before acknowledgement; lease expires; `discover_interrupted_jobs()`
  finds it; `recover_job()` with the source factory finishes the job:
  7 unique accounts (6 pre-crash untouched, none duplicated), ledger
  positions 1–8 exactly once each, invalid line skipped at its true
  position 4, counters `8/7/1/0` reconciled, `verify_import_job()` clean,
  and a second recovery run is idempotent.
  `CrashRecoveryTests.test_resume_view_reprocesses_abandoned_source_file` —
  the same kill, then the web **Resume** action completes the job.
  Synthetic records and the disposable test database only; no real
  credentials anywhere in tests, logs, or this report.

## 4. Exact verification output (sandbox: Linux, PostgreSQL 18.6, Python 3.11.2, Django 5.2.18)

```
$ python manage.py check
System check identified no issues (0 silenced).

$ python manage.py makemigrations --check --dry-run
No changes detected

$ python manage.py test vault.tests_import_reliability --verbosity 2
...
Ran 39 tests in 3.137s

OK

$ python manage.py test vault --verbosity 2
...
Ran 155 tests in 47.385s

OK (skipped=2)
```

- `vault.tests_import_reliability`: **39 tests, 0 failures, 0 errors,
  0 skipped** (was 35 with 1 failure + 1 error; +4 new tests).
- `vault`: **155 tests, 0 failures, 0 errors, 2 skipped** (was 148 with
  10 failures + 2 errors on Windows / 6 failures + 1 error reproduced on
  Linux; +7 new tests). The 2 skips are the intentional opt-in performance
  measurements (`VAULT_RUN_PERFORMANCE=1`).
- Targeted selection (duplicate imports, batch commits, account/reference
  reuse, interrupted recovery, resume, import-folder ingestion):
  **23 tests, OK**.
- Migration `vault.0006_url_domain_search_index` applies cleanly on top of
  `0005` (additive index; existing data untouched).

## 5. Remaining unverified behaviors / durability limitations

Per the ground rules, **crash-safe recovery is not "proven" by these unit
tests**, and the following remain unverified:

1. **Real process death.** The failure-injection tests simulate
   kill-after-commit-before-ack with a `BaseException` inside one process.
   A true `SIGKILL`/`TerminateProcess`/power-loss test of
   `runserver`/`importfolder` workers against a live cluster (including
   WAL recovery and `synchronous_commit` behavior at the OS level) has not
   been run.
2. **Windows confirmation.** The suite must be re-run on the Windows
   machine (its locale-collated cluster and spawn-based workers are the
   environments the C/D fixes target). The fixes are
   platform/collation-independent by construction, and the spawn path is
   exercised here by forced-spawn tests, but native confirmation is
   outstanding. Spawn throughput vs fork on large imports is unmeasured;
   frozen executables need `multiprocessing.freeze_support()`.
3. **Large-import behavior.** The opt-in throughput tests
   (`ImportThroughputTests`, hundreds of thousands of rows) were skipped;
   million-row imports with the durable engine (per-batch Python
   fingerprint/ledger lookups in `_write_ledger_entries`, the extra
   `vault_url_name_lower_c_idx` write cost, fast-mode interaction) are not
   benchmarked here.
4. **Streaming progress imports** (`X-Import-Progress: 1`) still use the
   non-durable `BulkImportSession`: no `ImportJob`, no ledger, no resume —
   pre-existing design, intentionally unchanged; their browser flow still
   ends at `/records/`.
5. **Uploaded-file jobs cannot auto-resume** (content is not kept on
   disk); recovery reconciles and settles them. Resuming a folder/path job
   requires the source files to still exist unchanged on the same machine;
   a mutated source will be replayed by position (no source-hash
   revalidation per line yet).
6. **Lease takeover latency:** an abandoned job becomes resumable only
   after its lease expires (default 300 s) or after it is marked
   interrupted; there is no automatic startup recovery sweep
   (`discover_interrupted_jobs()` exists but nothing schedules it).
7. **True multi-worker concurrency** (two live processes racing on one
   job) is covered only by the lease logic tests, not by parallel
   execution.

## 6. Pull request

This change set is submitted from branch `arena/527aaf46-credvault`; see
the branch's commit history and pull request for the exact revision.
Changed-file list: see section 2.

**The import implementation should not be declared production-ready until
items 1–3 of section 5 are verified on the target Windows/PostgreSQL
machine.**
