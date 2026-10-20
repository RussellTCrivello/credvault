-- Partitioning prototype (scratch schema `ptest`; never touches vault_* tables).
-- Question: does hash partitioning change single-node ingest / lookup / count / maintenance cost
-- for the two largest tables, and can it preserve the uniqueness guarantees?
-- Source rows are copied from the populated database so key distributions are real.
-- Run with: psql -f partition_prototype.sql  (timing is switched on; read the output)
\set ON_ERROR_STOP on
-- rows copied from each source table (edit for more/less disk): 1,000,000 by default
\set lim 1000000
\timing on
DROP SCHEMA IF EXISTS ptest CASCADE;
CREATE SCHEMA ptest;
SET search_path = ptest, public;
SET max_parallel_workers_per_gather = 0;   -- same plan shape on a 2 vCPU host

-- ---- Ledger -------------------------------------------------------------
-- plain: same shape and indexes as vault_importrecord after migration 0008
CREATE TABLE led_plain (LIKE public.vault_importrecord INCLUDING DEFAULTS);
ALTER TABLE led_plain ADD PRIMARY KEY (id);
ALTER TABLE led_plain ADD CONSTRAINT led_plain_idem UNIQUE (idempotency_key);
CREATE INDEX led_plain_job_status ON led_plain (import_job_id, status);
CREATE INDEX led_plain_job_pos    ON led_plain (import_job_id, source_position);
CREATE INDEX led_plain_account    ON led_plain (account_id);
CREATE INDEX led_plain_status     ON led_plain (status);

-- partitioned by hash(import_job_id): a job's rows stay together; the global uniqueness of
-- idempotency_key (sha256 of "job:position") is enforced as UNIQUE (import_job_id, idempotency_key),
-- which is equivalent because the key already contains the job id.
CREATE TABLE led_part (LIKE public.vault_importrecord INCLUDING DEFAULTS) PARTITION BY HASH (import_job_id);
DO $$ BEGIN FOR i IN 0..15 LOOP
  EXECUTE format('CREATE TABLE led_part_%s PARTITION OF led_part FOR VALUES WITH (MODULUS 16, REMAINDER %s)', i, i);
END LOOP; END $$;
ALTER TABLE led_part ADD PRIMARY KEY (id, import_job_id);
ALTER TABLE led_part ADD CONSTRAINT led_part_idem UNIQUE (import_job_id, idempotency_key);
CREATE INDEX led_part_job_status ON led_part (import_job_id, status);
CREATE INDEX led_part_job_pos    ON led_part (import_job_id, source_position);
CREATE INDEX led_part_account    ON led_part (account_id);
CREATE INDEX led_part_status     ON led_part (status);

\echo '== ledger bulk load of N rows (plain)'
INSERT INTO led_plain SELECT * FROM public.vault_importrecord ORDER BY id LIMIT :lim;
\echo '== ledger bulk load (hash partitioned x16)'
INSERT INTO led_part  SELECT * FROM public.vault_importrecord ORDER BY id LIMIT :lim;
VACUUM (ANALYZE) led_plain; VACUUM (ANALYZE) led_part;

\echo '== sizes'
SELECT 'plain' t, pg_size_pretty(pg_total_relation_size('led_plain')) total, pg_size_pretty(pg_indexes_size('led_plain')) idx;
SELECT 'part'  t, pg_size_pretty(sum(pg_total_relation_size(c.oid))) total, pg_size_pretty(sum(pg_indexes_size(c.oid))) idx
  FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'ptest.led_part'::regclass;

\echo '== (10 s settle)'
SELECT pg_sleep(1);
\echo '== sustained insert of 500k further ledger rows (new job id), plain vs partitioned'
SELECT (SELECT max(id) FROM public.vault_importrecord) AS maxid, (SELECT import_job_id FROM public.vault_importrecord ORDER BY id LIMIT 1) AS jid \gset
INSERT INTO led_plain (id, import_job_id, source_position, source_hash, source_line, status, account_id,
                       account_fingerprint, error_message, error_category, retryable, processed_at, idempotency_key)
SELECT :maxid + g, :jid, 10000000 + g, md5(g::text)||md5(g::text), '', 'committed', NULL, '', '', '', false, now(),
       encode(sha256(('p:'||g)::bytea),'hex')
FROM generate_series(1,500000) g;
INSERT INTO led_part (id, import_job_id, source_position, source_hash, source_line, status, account_id,
                       account_fingerprint, error_message, error_category, retryable, processed_at, idempotency_key)
SELECT :maxid + g, :jid, 10000000 + g, md5(g::text)||md5(g::text), '', 'committed', NULL, '', '', '', false, now(),
       encode(sha256(('p:'||g)::bytea),'hex')
FROM generate_series(1,500000) g;

\echo '== lookups: 20000 random idempotency_key point reads'
\echo '-- plain'
SELECT count(*) FROM (SELECT l.id FROM generate_series(1,20000) g
   JOIN led_plain l ON l.idempotency_key = encode(sha256(('p:'||(1+g*17%500000))::bytea),'hex')) s;
\echo '-- partitioned (key lookups without the job id cannot prune: all 16 partitions are probed)'
SELECT count(*) FROM (SELECT l.id FROM generate_series(1,20000) g
   JOIN led_part l ON l.idempotency_key = encode(sha256(('p:'||(1+g*17%500000))::bytea),'hex')) s;
\echo '-- partitioned, with job id (prunes to one partition; this is how the importer queries)'
SELECT count(*) FROM (SELECT l.id FROM generate_series(1,20000) g
   JOIN led_part l ON l.import_job_id = :'jid' AND l.idempotency_key = encode(sha256(('p:'||(1+g*17%500000))::bytea),'hex')) s;

\echo '== per-job status counts (verification path)'
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) SELECT status, count(*) FROM led_plain WHERE import_job_id = :'jid' GROUP BY status;
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) SELECT status, count(*) FROM led_part  WHERE import_job_id = :'jid' GROUP BY status;

\echo '== maintenance: VACUUM (FREEZE, ANALYZE) whole table'
VACUUM (FREEZE, ANALYZE) led_plain;
VACUUM (FREEZE, ANALYZE) led_part;
\echo '== maintenance: REINDEX of the unique key (plain: one big index; part: 16 small ones, concurrently possible)'
REINDEX INDEX led_plain_idem;
REINDEX INDEX led_part_idem;

-- ---- Accounts ----------------------------------------------------------------
-- Partitioning accounts by hash(fingerprint) keeps UNIQUE(fingerprint) valid but the primary key must
-- then contain the partition key, and the ledger's FK account_id -> account(id) needs a unique index
-- on id alone, which PostgreSQL refuses on a partitioned table. Show that failure, then measure what
-- partitioning would otherwise cost/gain.
CREATE TABLE acct_part (LIKE public.vault_account INCLUDING DEFAULTS) PARTITION BY HASH (fingerprint);
DO $$ BEGIN FOR i IN 0..15 LOOP
  EXECUTE format('CREATE TABLE acct_part_%s PARTITION OF acct_part FOR VALUES WITH (MODULUS 16, REMAINDER %s)', i, i);
END LOOP; END $$;
\set ON_ERROR_STOP off
\echo '== expected failure: UNIQUE(id) on a table partitioned by fingerprint'
ALTER TABLE acct_part ADD CONSTRAINT acct_part_id_only UNIQUE (id);
\set ON_ERROR_STOP on
ALTER TABLE acct_part ADD PRIMARY KEY (id, fingerprint);
ALTER TABLE acct_part ADD CONSTRAINT acct_part_fp UNIQUE (fingerprint);
CREATE TABLE acct_plain (LIKE public.vault_account INCLUDING DEFAULTS);
ALTER TABLE acct_plain ADD PRIMARY KEY (id);
ALTER TABLE acct_plain ADD CONSTRAINT acct_plain_fp UNIQUE (fingerprint);
\echo '== account load: plain'
INSERT INTO acct_plain SELECT * FROM public.vault_account ORDER BY id LIMIT :lim;
\echo '== account load: partitioned'
INSERT INTO acct_part SELECT * FROM public.vault_account ORDER BY id LIMIT :lim;
VACUUM (ANALYZE) acct_plain; VACUUM (ANALYZE) acct_part;
\echo '== 20000 random fingerprint lookups'
SELECT count(*) FROM (SELECT a.id FROM (SELECT fingerprint FROM public.vault_account TABLESAMPLE SYSTEM (0.5) LIMIT 20000) f JOIN acct_plain a USING (fingerprint)) s;
SELECT count(*) FROM (SELECT a.id FROM (SELECT fingerprint FROM public.vault_account TABLESAMPLE SYSTEM (0.5) LIMIT 20000) f JOIN acct_part a USING (fingerprint)) s;
\echo '== count(*)'
SELECT count(*) FROM acct_plain;
SELECT count(*) FROM acct_part;
\echo '== sizes'
SELECT pg_size_pretty(pg_total_relation_size('acct_plain')) plain;
SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) part
  FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'ptest.acct_part'::regclass;
DROP SCHEMA ptest CASCADE;
