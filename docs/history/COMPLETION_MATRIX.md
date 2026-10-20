# CredVault — Completion Matrix (final)

Audit performed 2026-10-14 against branch `arena/0f03219f-credvault` (from
`main` @ `a957402`, clean tree). Verification was **executed**, not assumed:
a PostgreSQL 16 instance with `pg_trgm` was stood up, all 7 migrations
applied, and the full suite plus live end-to-end runs (including a
kill-`-9` crash/recovery drill and a 1,000,000-line pause/resume cycle)
were performed before any change.

**All seven defects (D1–D7) have been fixed and verified** (fix status at the
bottom of this file). Post-fix suite: **199 tests, OK (3 skipped)** — the 3
skips are the opt-in performance tests; with `VAULT_RUN_PERFORMANCE=1` all
17 performance tests pass.

## Baseline (pre-change)

- `manage.py check`: 0 issues; `migrate`: all 7 migrations apply cleanly.
- `manage.py test vault`: **192 tests, 192 passed, 3 skipped** (the 3 skips
  are the opt-in performance tests gated on `VAULT_RUN_PERFORMANCE=1`).
- With `VAULT_RUN_PERFORMANCE=1`: all 16 performance tests pass
  (bulk ≈ 8,166 lines/s; durable path ≈ 3,911 lines/s at 20k lines).
- Live: login/authorization, CRUD, search (exact + substring + domain
  range), references, exports, i18n (en/ar RTL), import jobs list/detail/
  status/records/verify, pause/resume/cancel at 1,000,000 lines, and
  SIGKILL crash recovery at 1,500,000 lines all behave as documented.

## Feature status

| # | Feature | Intended behavior (README/impl) | Status | Evidence / findings |
|---|---------|--------------------------------|--------|---------------------|
| 1 | Admin login/logout | Staff-only sign-in; `next` sanitized; i18n aware | **Complete** | 17 access tests + live: anon→login redirect, external `next` ignored, non-staff refused |
| 2 | Dashboard | Exact workspace counts | **Complete** | Live + `test_count_is_exact_below_ten_thousand` |
| 3 | Record CRUD | Add/edit/view/delete; format validation; dedupe | **Fixed (D1)** | Detail intro now renders `record.url.name · record.identifier`; live: shows the actual URL and identifier. Test: `test_record_detail_intro_shows_the_url_and_identifier` |
| 4 | Quick search | Exact, 6 field targets, domain range scan under COLLATE "C" | **Complete** | Live: domain/url/ip/password/all-field verified; `test_quick_search_stays_exact_for_non_domain_values` |
| 5 | Advanced search | Substring AND-filters, keyword OR, date ranges, sort | **Complete** | Live + `test_advanced_search_matches_substrings`; sorting verified asc/desc mirror |
| 6 | Reference tables | Add/reuse/delete (protected while in use), usage, suggest | **Complete** | Live: reuse, blocked delete, unused delete, suggest endpoint. Note: suggest prefix range uses DB collation (results stay complete via icontains fallback); not a defect on supported clusters |
| 7 | Import (upload/path/streaming) | Durable job + per-line ledger; redirect contract | **Fixed (D2)** | Port-without-path colon lines now parse (port merged into the URL); live import stored `https://portfix.example:8443/login` with the port. Two tests in `tests_formats.py`; all previously accepted formats unchanged |
| 8 | Import job lifecycle | Pause/resume/cancel/verify/status/records | **Fixed (D3)** | Per-row account lookups replaced by bounded bulk lookups (`id__in` chunks of 5000) in both `verify_import_job` and `recover_job`; live: Verify page on the 1,500,000-row job returns "Verified - No discrepancies" in ~43 s (was ≈15+ min / unusable). Query-count regression test: `test_verify_uses_bounded_account_lookups` |
| 9 | Crash recovery | Exactly-once resume after interruption | **Complete** | Live SIGKILL at 780k/1.5M lines: ledger + counters + checkpoint consistent (480k committed + 300k duplicate-skips), lease backdated to expiry, Resume endpoint re-imported the remaining 720k exactly once, final 1,500,000/1,500,000, zero duplicate accounts |
| 10 | `importfolder` command | Durable path import; per-path output | **Fixed (D4)** | Non-completed jobs are reported per path on stderr, the summary says "Done with errors …", the failed count is included, and the command exits non-zero; an unreadable source now fails with a clean `CommandError` (no traceback). Live: unreadable file → exit 1, clean message; success path still exit 0. Tests: `test_importfolder_reports_incomplete_jobs`, `test_importfolder_rejects_an_unreadable_source` |
| 11 | `copy_from_sqlite` | Atomic copy of old SQLite db + verification | **Fixed (D5)** | Pre-check for missing NOT-NULL-no-default target columns → clean `CommandError` naming the column(s), before any COPY; nothing is saved. Live: pre-fingerprint SQLite file → exit 1, "…has no fingerprint column(s) … Nothing was saved", scratch DB left empty (atomic rollback confirmed). Test: `test_copy_refuses_a_source_missing_required_columns` |
| 12 | Export CSV/JSON | Streaming, honors active search filters | **Complete** | Live: full + filtered exports exact |
| 13 | i18n en/ar | Translations, RTL, session-persisted | **Complete** | Live: setlang round-trip, `dir="rtl"`, Arabic labels |
| 14 | Configuration / setup | README setup procedure | **Fixed (D6)** | `settings.py` DB defaults now match the documented `credvault` user with the password taken from `POSTGRES_PASSWORD` (no committed credential anywhere in the repo — grep-verified). Env-var-driven connection proven by the full suite |
| 15 | Streaming progress UI | NDJSON contract: count → job → progress → done | **Fixed (D7)** | Processing-phase events now carry the same file-based `files_completed/total_files` as the count phase (verified in a live streaming import); admin.js renders "N / M files processed". Count-phase events unchanged (pinned by existing test) |
| 16 | Django admin | All models registered; sensitive fields excluded | **Complete** | Renders; `last_error`, `source_line`, `error_message` excluded from admin |
| 17 | Performance suite | Opt-in benchmarks | **Complete** | 16/16 pass with `VAULT_RUN_PERFORMANCE=1` |
| 18 | Security posture | CSRF, authorization, credential-safe ledger | **Complete** | Ledger stores hashes only; error text redacted to exception class names; `next` sanitized; middleware gates all workspace routes |

## Prioritized corrective plan

**P1 — correctness / data integrity**
1. **D2** parser: colon-separated, port-without-path lines. Add a merge
   fallback in `utils._parse_with_delimiter` (port-looking field 2 belongs to
   the URL) that is tried when the port reading parses and the plain reading
   fails, preserving every currently accepted line. Tests in `tests_formats.py`.
2. **D1** `detail.html`: render `record.url.name · record.identifier`. Test.
3. **D6** `settings.py`: DB defaults to the documented `credvault`/empty.
   Verified by a clean env-var-free migrate against a fresh role.

**P2 — reliability at the documented scale**
4. **D3** `import_engine.py`: replace the per-row account fingerprint lookups
   in `verify_import_job` and `recover_job` with bounded bulk lookups
   (the pattern the engine already uses in `_write_ledger_entries`). Query-
   count regression test.
5. **D4** `importfolder`: report non-completed jobs, include failed counts,
   exit non-zero. Test with a mocked failed job.

**P3 — minor quality**
6. **D5** `copy_from_sqlite`: pre-check for missing NOT NULL columns → clean
   `CommandError`. Test.
7. **D7** streaming events: processing phase reports the same file-based
   totals as the count phase; JS renders "files processed". Test.
8. Remove the unused `recent_records` context from `import_job_detail`
   (confirmed absent from the template).

Out of scope (documented, not defects): production hardening of
`SECRET_KEY`/`DEBUG`/`ALLOWED_HOSTS` (consistent documented development
posture), background-worker architecture (explicitly the existing design),
Windows/spawn worker path (not exercisable on this host), locale-collation
cluster testing (only C locale available in this environment; the COLLATE "C"
comparisons are correct by construction and index-backed).

## Fix status (all changes verified)

| Defect | Fix | Files changed | Verified by |
|--------|-----|---------------|-------------|
| D1 | Detail intro renders `record.url.name · record.identifier` | `vault/templates/vault/detail.html` | Test `test_record_detail_intro_shows_the_url_and_identifier` + live page |
| D2 | `_parse_with_delimiter` retries with the port merged into the URL when field 2 is a port and the plain reading fails | `vault/utils.py` | 2 new tests in `vault/tests_formats.py`; live import of a `:8443` line stored with the port; plain 3/4-part lines unchanged |
| D3 | `_committed_account_fingerprints` / `_pair_with_account_fingerprints` (chunked `id__in`) replace per-row lookups in `verify_import_job` and `recover_job` | `vault/import_engine.py` | Test `test_verify_uses_bounded_account_lookups` (2,000 rows, ≤2 account SELECTs); live Verify of the 1.5M-row job in ~43 s |
| D4 | Non-completed jobs → per-path stderr error, "Done with errors" summary with failed counts, non-zero exit; `SourceChangedError` → clean `CommandError` | `vault/management/commands/importfolder.py` | Tests `test_importfolder_reports_incomplete_jobs`, `test_importfolder_rejects_an_unreadable_source`; live unreadable-file run (exit 1, clean message) and success run (exit 0) |
| D5 | `_required_columns` pre-check in `_copy_table` → `CommandError` naming the missing NOT-NULL column before any COPY | `vault/management/commands/copy_from_sqlite.py` | Test `test_copy_refuses_a_source_missing_required_columns`; live copy of a pre-fingerprint SQLite file (clean error, exit 1, atomic rollback confirmed) |
| D6 | DB defaults: user `credvault`, password from `POSTGRES_PASSWORD`; committed plaintext password removed | `admin_LogIntel/settings.py` | grep: no `eggarf123` in repo; full suite connects via env vars |
| D7 | Processing-phase events use file-based `files_completed/total_files`; JS renders "N / M files processed" | `vault/views.py`, `vault/static/monitoring/admin.js` | Live streaming import: count phase `1/1 files scanned` and processing phase `1/1 files processed` agree; `node --check` on the served JS |
| (P3h) | Removed unused `recent_records` context (one dead query per job-detail page) | `vault/views.py` | Grep: absent from all templates; suite green |

**Regression check (post-fix):** full suite 199 tests OK (3 skipped);
performance suite 17 tests OK with `VAULT_RUN_PERFORMANCE=1`
(≈7,867 lines/s bulk at 20k). No previously passing behavior changed.
