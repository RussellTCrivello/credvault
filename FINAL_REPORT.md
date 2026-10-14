# CredVault — Final Report

Date: 2026-10-14 · Branch: `arena/0f03219f-credvault` (base `main` @ `a957402`)

Scope: complete and stabilize the existing application. No new features, no
architecture changes, no rewrites. Seven defects (D1–D7) found in the audit
were fixed; everything else was left as designed. Details in
`COMPLETION_MATRIX.md`.

## 1. What was done

1. **Audit** — read every module; stood up PostgreSQL 16 with `pg_trgm`;
   ran the full suite, performance suite, and live end-to-end drills
   (1M-line pause/resume, SIGKILL crash/recovery at 1.5M lines, SQLite copy,
   exports, i18n, access control) *before* changing anything.
2. **Completion matrix** — 18 features scored against README intent
   (`COMPLETION_MATRIX.md`); 7 defective, prioritized P1–P3.
3. **Implementation** — the 7 fixes, each with a regression test where the
   behavior is testable, plus one dead-context removal (P3h).
4. **Testing & regression** — full suite + performance suite re-run after
   every fix batch; live spot-verification of each fix.

## 2. Verified results (executed, not assumed)

Environment: PostgreSQL 16.2 (local), `pg_trgm` 1.6, Django 5.2.18,
Python 3.11. All commands below were actually run on this date.

### Test suites

| Run | Result |
|-----|--------|
| `manage.py test vault` (pre-change baseline) | **192 passed, 3 skipped**, 51.5 s |
| `manage.py test vault` (post-change, final) | **199 passed, 3 skipped**, 54.8 s |
| `manage.py test vault.tests_performance` with `VAULT_RUN_PERFORMANCE=1` | **17 passed**, ~14 s |
| `manage.py check` | 0 issues |

The 3 skips are the opt-in performance tests (always skipped without the
flag — same as baseline). 7 new tests were added:
detail-intro, 2 parser-port formats, bounded verify lookups, 2 importfolder
failure paths, copy pre-check.

### Each fix, verified live against the running server + dev database

- **D1 detail intro** — `GET /records/1/` renders
  `https://example.com/login · bob` (real URL · identifier), not the bare
  `·` placeholder.
- **D2 port-in-URL import** — a streaming import of
  `https://portfix.example:8443/login|alice|s3cret` and
  `https://portfix.example/login|carol|pw` created both records; the port
  line stored its URL with `:8443`. Previously the port line was silently
  dropped.
- **D3 verify scale** — Verify page on the real 1,500,000-row job
  (`028f7c44…`): **200 in ~43 s**, "Verified - No discrepancies —
  Discovered 1,500,000 / Committed 1,200,000 / Skipped 300,000 / Failed 0".
  Pre-fix this path ran one account query per committed ledger row
  (≈15 min+ at this scale; measured during the audit). A debug-cursor test
  pins ≤2 account SELECTs for 2,000 rows.
- **D4 importfolder failures** — folder containing an unreadable file:
  `CommandError: /tmp/impfail: An import source file is missing or
  unreadable. …` on stderr, **exit 1**, no traceback, no rows saved. A
  non-completed job is reported per path, the summary reads "Done with
  errors …" with failed counts, and exit is non-zero. Success path still
  exits 0 with the normal summary (re-run live after the change).
- **D5 copy pre-check** — copied the working legacy fixture, stripped the
  `fingerprint` column (pre-0007 schema), ran
  `manage.py copy_from_sqlite` into a fresh migrated DB: clean
  `CommandError: vault_account: the SQLite file has no fingerprint
  column(s), which the current schema requires. … Nothing was saved.`,
  **exit 1**, and the scratch DB verified empty afterwards (atomic
  rollback). The success path was verified live earlier (counts +
  fingerprints matched, sequences reset).
- **D6 committed credential** — `settings.py` defaults now match the
  documented configuration; `eggarf123` appears nowhere in the repository
  (grep-verified); the suite's env-var-driven connection works unchanged.
- **D7 streaming file counts** — live streaming import NDJSON shows the
  count phase `files_scanned: 1 / total_files: 1` and processing-phase
  events `files_completed: 0 → 1, total_files: 1` — consistent file-based
  counting. `admin.js` renders "N / M files processed"; `node --check`
  passes on the served file.
- **P3h dead context** — `recent_records` removed from
  `import_job_detail`; confirmed absent from every template; one fewer
  query per job-detail page load.

### Pre-existing behavior re-verified after the changes

Full-suite pass covers: access control/CSRF, CRUD + validation, exact
search semantics (exact/substring/domain), reference reuse/protection,
i18n round-trip, exports, import dedup, job lifecycle (pause/resume/cancel/
verify/status/records), crash recovery (exactly-once resume), bulk and
durable import performance. The pre-change live drills (1M pause/resume,
SIGKILL recovery at 1.5M) remain valid: the changed code paths were all
exercised again by the post-change suite plus the live spot-checks above.

## 3. Assumptions and recommendations (not verified here)

- **Deployment posture** — `SECRET_KEY` in `settings.py`, `DEBUG=True`,
  and permissive `ALLOWED_HOSTS` are the project's documented development
  configuration; left untouched per scope. Before any real deployment:
  set a strong secret, `DEBUG=False`, and an explicit host list.
- **DB credentials** — the app reads `POSTGRES_PASSWORD` from the
  environment (documented in README and the settings header). The local
  sandbox role uses a sandbox-only password; production credentials are
  out of scope.
- **Locale coverage** — this environment offers only C/C.utf8 locales, so
  the COLLATE "C" domain-scan behavior could not be exercised against a
  non-C-database cluster. The comparisons are correct by construction and
  index-backed; recommend one CI job on a UTF-8-locale cluster if such
  clusters are a supported target.
- **Arabic catalog lag** — `locale/ar` covers many but not all newer
  strings (several import-progress labels fall back to English). Running
  `makemessages`/`compilemessages` with a translator is a content task,
  not a code defect.
- **`__pycache__` committed** — the repo history contains compiled
  bytecode; harmless but noisy. Recommend a `.gitignore` entry and one
  cleanup commit (not done — out of scope without approval).
- **Scale ceiling** — verify/recovery are now query-bounded but still
  stream the ledger; at 10M+ rows a background job would be the right
  shape. That is an architecture change and was intentionally not made.
- **Windows worker path** — the spawn-based parse-worker path is not
  exercisable on this host and was not tested.

## 4. Files changed

| File | Change |
|------|--------|
| `vault/templates/vault/detail.html` | D1: intro uses real model fields |
| `vault/utils.py` | D2: port-aware retry in `_parse_with_delimiter` |
| `vault/import_engine.py` | D3: bounded fingerprint lookups in verify/recover |
| `vault/management/commands/importfolder.py` | D4: clean failure reporting + non-zero exit |
| `vault/management/commands/copy_from_sqlite.py` | D5: required-column pre-check |
| `admin_LogIntel/settings.py` | D6: documented DB defaults, no committed password |
| `vault/views.py` | D7: file-based processing events; P3h: dead context removed |
| `vault/static/monitoring/admin.js` | D7: "N / M files processed" |
| `vault/tests.py`, `vault/tests_formats.py`, `vault/tests_import_reliability.py`, `vault/tests_performance.py` | 7 new regression tests |
| `COMPLETION_MATRIX.md` | New: audit + matrix + plan + fix status |
| `FINAL_REPORT.md` | This report |

## 5. Performance phase (continuation — verified results)

Date: 2026-10-14, same env. All numbers executed, not assumed. Full procedure and evidence in `PERFORMANCE_RECOVERY_REPORT.md` and `CONTINUATION.md`.

**Baselines (100k lines, fresh DB):**
- Bulk `import_path` (2 workers, no ledger): 12.0 s = 8,351/s
- Durable pipeline (product default): 30.0 s = 3,338/s

**Bottlenecks (cProfile + wall timers):**
1. Ledger ORM `bulk_create` param marshalling (~18 s of 54.8 s profiled) — largest.
2. Double canonicalization per line (~5.7 s validators).
3. 5×50k IN-list SELECTs for reference ids (~2.6 s).

**Optimizations in `vault/import_engine.py`:**
1. Ledger via temp-table COPY + INSERT…SELECT (same pattern as references/accounts) — 3,338→4,122/s.
2. Remove second normalization in hot loop (`parse_line` already canonical; verified identical on 100,009 lines; `_canonical_record` deleted) — →4,744/s.
3. Reference ids via set-joins against staged batch (`_stage_name_ids`) not IN lists; fallback `_resolve_reference_ids` for direct callers — →4,988/s (+49%).

**Scale:**
- 500k durable: 111.5 s = 4,484/s, 500k accounts — no superlinear cliff.
- 20k in-suite: 3,911→5,176/s (+32%); bulk 8,290/s unchanged; 199 OK (3 skipped), 17/17 perf OK.

**Search baseline (2.75M-row dev DB, real views):**
dashboard 307ms median, url exact 15ms, url trgm 14ms, domain C-scan 117ms, ip exact 37ms, ip broad 35ms, identifier 11ms, keyword 10ms, browse sorts ~108ms, suggest 6ms — all <1 s, semantics preserved.

**Crash drill on optimized flush path:**
- 1M file, SIGKILL at 300k committed → crash state consistent (ledger==terminal==accounts==checkpoint, 300k).
- Lease backdated, `recover_job` resumed 700k → completed 1M, 0 duplicate fingerprints, contiguous positions, exactly-once True (discovered==committed+skipped+failed==ledger==accounts==1M), recovery log outcome partial with note "Resumed 700000…".

**Files changed in performance phase:**
- `vault/import_engine.py`: 3 optimizations.
- `vault/tests_deep_verification.py`: query-count assertion updated to new faster path (1 query not 4).
- Docs: `PERFORMANCE_RECOVERY_REPORT.md`, `CONTINUATION.md`.
