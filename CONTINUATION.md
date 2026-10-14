# CredVault — Continuation Record (live handoff) — FINAL

> Updated at every milestone. A new session recovers the exact task from this file + repo state.

## Repository state (final)

- **Path:** `/home/user/credvault`
- **Branch:** `arena/0f03219f-credvault` (base `main` @ `a957402`)
- **Working tree:** uncommitted changes (auto-persisted):
  - Stabilization (D1–D7): `vault/utils.py`, `vault/templates/vault/detail.html`, `admin_LogIntel/settings.py`, `vault/management/commands/importfolder.py`, `vault/management/commands/copy_from_sqlite.py`, `vault/views.py`, `vault/static/monitoring/admin.js`, `vault/import_engine.py`, `vault/tests.py`, `vault/tests_formats.py`, `vault/tests_import_reliability.py`, `vault/tests_performance.py`
  - Performance (this session): `vault/import_engine.py` (3 optimizations), `vault/tests_deep_verification.py` (assertion update)
  - Docs: `COMPLETION_MATRIX.md`, `FINAL_REPORT.md`, `PERFORMANCE_RECOVERY_REPORT.md`, `CONTINUATION.md`
- **Suite:** 199 OK (3 skipped), 52.3 s; perf 20k: durable 5,176/s (was 3,911/s), bulk 8,290/s; 17/17 perf OK.
- **Dev DB:** `credvault` 2,750,017 accounts (2.75M search baseline target).
- **Scratch DB:** `perf_repro` last held 1M-row crash drill (1M committed, exactly-once). Can be dropped/recreated freely.

## Environment restore

- `.venv` at repo root (Django 5.2.18, psycopg 3.3.6, Python 3.11)
- PG 16.2 at `/tmp/pgtest/data` (pg_ctl -D /tmp/pgtest/data -l /tmp/pgtest/pg.log -w start)
- DBs: `credvault` (dev), `perf_repro` (scratch)
- Env vars: `POSTGRES_HOST=127.0.0.1 POSTGRES_USER=credvault POSTGRES_PASSWORD=credvault123 POSTGRES_DB=credvault`
- Full restore recipe: see `FINAL_REPORT.md`.

## Last completed task + verified results

**Performance phase complete:**

1. Baselines: bulk 100k 12.0s 8,351/s; durable 100k 30.0s 3,338/s (pre-opt) → 20.0s 4,988/s (+49%) post-opt; 20k in-suite 3,911→5,176/s (+32%); 500k 111.5s 4,484/s (no cliff).
2. 3 optimizations in `vault/import_engine.py`:
   - Ledger COPY+INSERT..SELECT not bulk_create
   - Remove double canonicalization (parse_line already canonical)
   - Stage-join reference id resolution not 50k IN lists
3. Search baseline at 2.75M rows: dashboard 307ms median, all search shapes 8-37ms except domain C-scan 117ms, browse sorts ~108ms, suggest 6ms — all <1s, semantics preserved.
4. Crash drill on optimized path: SIGKILL at 300k → crash state consistent (ledger==terminal==accounts==checkpoint) → lease backdated → recover_job → 1M committed, 0 dup fingerprints, contiguous positions, exactly-once True, 187s for remaining 700k.

**Stabilization phase (previous):** 7 defects fixed, live-verified per FINAL_REPORT.md.

## Current task

**All acceptance criteria met.** No unfinished implementation tasks. Next actions are optional follow-ups:

- Drop scratch DB `perf_repro` if desired: `psql -h 127.0.0.1 -U credvault -d postgres -c "DROP DATABASE perf_repro;"`
- Clean `/tmp` fixtures: `rm /tmp/perf_*.txt /tmp/drill_*.txt /tmp/drill_*.log /tmp/drill_*.py /tmp/search_baseline*.txt /tmp/repro_*.log`
- Commit working tree (currently uncommitted per platform convention) when ready.

## Known findings / non-defects

- Fast mode only +7% at 100k (GIN rebuild offsets savings) — documented.
- One 100k Django-test run stalled 17 min I/O-bound (empty wait_event) — not reproducible outside test runner; 500k/1M runs confirm stable.
- `__pycache__` committed in history — harmless, recommend .gitignore cleanup (out of scope).

## Next exact action

**None required** — project meets all final acceptance criteria per PERFORMANCE_RECOVERY_REPORT.md §9. If new work starts, begin from this file.
