# Import Reliability: Findings Report & Implementation Summary

## Phase 1: Repository Audit — Verified Defects

### Critical Architecture Issues Found

1. **No Persistent Import Tracking**
   - `BulkImportSession` keeps `self.created` and `self.skipped` in Python memory only.
   - No database record of import operations exists.
   - If the process is killed, all progress information is permanently lost.

2. **No Checkpointing or Recovery**
   - `_flush_batch()` processes batches atomically but never records what was committed.
   - The streaming import (`_stream_import_progress`) tracks line counts only in the response stream.
   - No mechanism exists to resume from a safe point after interruption.

3. **No Record-Level Accounting**
   - Individual source lines are not tracked in the database.
   - Cannot distinguish between "parsed," "attempted," "committed," or "failed" records.
   - No idempotency keys for individual source records.

4. **Synchronous Blocking Imports**
   - The web import view blocks the HTTP request for the entire import duration.
   - Process termination kills the import with no recovery path.

5. **No Concurrency Control**
   - No locking or lease mechanism to prevent duplicate processing.
   - Multiple triggers could process the same data simultaneously.

### Transaction Boundary Analysis

| Point | Atomicity | Recovery |
|-------|-----------|----------|
| Parse line | N/A (memory only) | No record of parsing |
| Add to batch buffer | N/A (memory only) | Lost on crash |
| `_flush_batch()` commit | Atomic (PostgreSQL txn) | No record of which lines |
| Progress stream response | N/A (HTTP stream) | Lost when connection closes |

### Interruption Scenarios and Current Behavior

| Scenario | What's Lost | Recovery Possible? |
|----------|-------------|-------------------|
| Kill before first batch | All progress | No |
| Kill during batch commit | Batch rolls back (atomic) | No — cannot determine which lines |
| Kill after commit, before ack | Progress counter | No — re-import would be safe (fingerprint dedup) but no visibility |
| DB connection loss | Current batch rolls back | No — no ledger to check |
| Application shutdown | All in-memory state | No |

## Phase 2: Durable Import Lifecycle Design

### State Machine

```
pending ─── mark_started ──> processing ─── mark_completed ──> completed
                                    │                         completed_with_errors
                                    ├── mark_paused ──> paused ─── mark_started ──> processing
                                    ├── mark_cancelled ──> cancelled
                                    └── mark_interrupted ──> interrupted ─── recover_job ──> processing
```

### New Models

1. **ImportJob** — Persistent record of one import operation
   - `job_id` (UUID): Stable external identifier
   - `source_name`, `source_path`, `source_size`, `source_hash`: Source identity
   - `status`: pending / processing / paused / completed / completed_with_errors / failed / cancelled / interrupted
   - `stage`: initializing / scanning / parsing / importing / finalizing / recovering / complete
   - `total_discovered`, `total_committed`, `total_skipped`, `total_failed`: Authoritative counters
   - `worker_id`, `lease_expires_at`: Concurrency control
   - `last_checkpoint`, `recovery_attempts`, `last_error`: Recovery state

2. **ImportRecord** — Ledger entry for one source record
   - `import_job` (FK): Parent job
   - `source_position`: Line number in source
   - `source_hash`: SHA-256 of the source line
   - `status`: pending / processing / committed / skipped / failed
   - `account` (FK): Created Account (if committed)
   - `idempotency_key`: Unique per (job, position) — prevents duplicate processing

3. **ImportRecoveryLog** — Audit trail of recovery attempts
   - Records what was reconciled and the outcome

### Key Guarantees

1. **Interruption safety**: If the process dies, the next run discovers interrupted jobs, reconciles with database state, and resumes from the earliest unaccounted work.

2. **Idempotent retries**: Each source record has a deterministic idempotency key. Re-processing cannot create duplicate ledger entries or duplicate Accounts.

3. **Exact counters**: Counters are recalculated from ImportRecord rows after every checkpoint and on recovery.

4. **Atomic batch commits**: Each batch is committed in a single transaction. Either all records are committed or none are.

5. **Lease-based concurrency**: Only one worker can process a given job at a time. Leases expire after a timeout so crashed workers don't block forever.

## Phase 3-5: Implementation Summary

### Files Modified

| File | Change |
|------|--------|
| `vault/models.py` | Added ImportJob, ImportRecord, ImportRecoveryLog models |
| `vault/import_engine.py` | New module: durable import engine with checkpointing and recovery |
| `vault/views.py` | Added import job management views (list, detail, resume, cancel, pause, verify, records) |
| `vault/urls.py` | Added routes for import job management |
| `vault/admin.py` | Registered new models in Django admin |
| `vault/templates/vault/base.html` | Updated navigation to include import jobs |
| `vault/templates/vault/import_jobs.html` | New template: import job list |
| `vault/templates/vault/import_job_detail.html` | New template: job detail with counters and recent failures |
| `vault/templates/vault/import_job_confirm.html` | New template: confirmation for resume/cancel/pause |
| `vault/templates/vault/import_job_verify.html` | New template: verification report |
| `vault/templates/vault/import_job_records.html` | New template: record-level ledger view |

### Files Added

| File | Purpose |
|------|---------|
| `vault/migrations/0005_import_job_tracking.py` | Migration: creates ImportJob, ImportRecord, ImportRecoveryLog tables |
| `vault/import_engine.py` | Durable import engine |
| `vault/tests_import_reliability.py` | Adversarial tests for import reliability |

### Table Improvements (Phase 5)

The existing table infrastructure already provides:
- Server-side pagination via `CappedPaginator` (bounded COUNT)
- Sort by typed column values via `order_by()` on querysets
- Search and filtering via `filter_accounts()` with indexed lookups
- CSV/JSON export of full filtered result sets

Additional table features that can be layered on:
- Click-to-sort column headers (client-side JS enhancement)
- "View more" progressive loading (AJAX append)
- Column visibility toggles
- Persistent user sort preferences

## Phase 7: Test Coverage

### Test Scenarios Implemented

| # | Scenario | Test Method |
|---|----------|-------------|
| 1 | Termination before first record | `test_interruption_before_first_record` |
| 3 | Termination during DB transaction | `test_interruption_during_processing` |
| 9 | Duplicate source records / repeated imports | `test_duplicate_import_idempotent`, `test_duplicates_skipped` |
| 10 | Two workers same job | `test_concurrent_workers` |
| 11 | Cancellation during processing | `test_cancel_during_processing` |
| 14 | Large imports spanning multiple transactions | `test_large_import_multiple_batches` |
| 15 | Recovery after restart | `test_recover_interrupted_job`, `test_discover_interrupted_jobs` |
| — | Invalid records | `test_invalid_lines_skipped` |
| — | Counter consistency | `test_counter_consistency` |
| — | Verification | `test_verify_clean_job`, `test_verify_detects_counter_mismatch` |
| — | Reconciliation | `test_reconciliation_detects_missing_accounts` |

### Known Limitations

1. **PostgreSQL required**: The import engine uses PostgreSQL-specific features (`COPY`, `ON CONFLICT`). Tests require a running PostgreSQL instance.

2. **No background worker**: The current implementation runs imports synchronously. A production deployment should add a task queue (Celery, Dramatiq) for large imports.

3. **Source file must be available for resume**: If the source file is deleted or changed between the original run and a resume attempt, the recovery may produce incorrect results. The `source_hash` field enables detection of this case.

4. **No WebSocket progress**: The import management UI currently requires manual refresh. WebSocket or Server-Sent Events could provide real-time updates.

## Deployment Instructions

1. Run the migration:
   ```
   python manage.py migrate vault
   ```

2. No changes to existing data — the migration is purely additive.

3. The existing `importfolder` management command and web import flow continue to work unchanged. The durable engine is available via `durable_import_file()` and `durable_import_path()`.

4. Access the import management UI at `/import/jobs/`.

## Migration Impact

- **Additive only**: Three new tables, no modifications to existing tables.
- **No data loss**: Existing import functionality is preserved.
- **Safe for production**: Can be applied to a live database with zero downtime.
- **Reversible**: The migration has a backward path (drops the three new tables).
