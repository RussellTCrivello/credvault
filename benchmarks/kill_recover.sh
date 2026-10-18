#!/bin/bash
. "${CV_ENV_FILE:-/tmp/pgenv.sh}"
PGB=${CV_ROOT:-/home/user/credvault}/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
PSQL="$PGB/psql -h /tmp/pg-credvault-main -U postgres -q -At"
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)" -c "CREATE DATABASE cv_run TEMPLATE credvault"
export POSTGRES_DB=cv_run
cd ${CV_ROOT:-/home/user/credvault}
.venv/bin/python manage.py migrate -v0
A0=$($PSQL cv_run -c "select count(*) from vault_account")
VAULT_IMPORT_BATCH_ROWS=20000 .venv/bin/python manage.py importfolder ${CV_BENCH:-/tmp/bench}/g200k_c > ${CV_BENCH:-/tmp/bench}/kill_run.log 2>&1 &
PID=$!
sleep ${KILL_AFTER:-22}
pkill -9 -P $PID; kill -9 $PID
sleep 1
echo "killed at $(date +%T); ledger rows: $($PSQL cv_run -c 'select count(*) from vault_importrecord') accounts_added: $(( $($PSQL cv_run -c 'select count(*) from vault_account') - A0 ))"
$PSQL cv_run -c "select status, stage, total_committed, total_skipped, last_activity_at from vault_importjob" 2>&1 | head -3
# let the lease expire as it would in production (lease is renewed by live worker only)
$PSQL cv_run -c "update vault_importjob set lease_expires_at = now() - interval '1 second' where status='processing'"
T0=$(date +%s.%N)
.venv/bin/python manage.py recoverimports
.venv/bin/python manage.py recoverimports --run 2>&1 | tail -2
T1=$(date +%s.%N)
python3 -c "print('recovery seconds %.1f'%($T1-$T0))"
echo "final ledger rows: $($PSQL cv_run -c 'select count(*) from vault_importrecord'), distinct idem keys: $($PSQL cv_run -c 'select count(distinct idempotency_key) from vault_importrecord'), accounts_added: $(( $($PSQL cv_run -c 'select count(*) from vault_account') - A0 )) (uninterrupted run: 176179), duplicate fingerprints: $($PSQL cv_run -c 'select count(*) from (select fingerprint from vault_account group by 1 having count(*)>1) x')"
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)"
