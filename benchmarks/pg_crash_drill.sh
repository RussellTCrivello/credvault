#!/bin/bash
# PostgreSQL crash drill: stop the *server* with `pg_ctl stop -m immediate` (no shutdown checkpoint,
# connections dropped, same crash-recovery path as a postmaster crash / power loss as far as the
# server's own WAL replay is concerned) in the middle of a durable import, restart, run amcheck,
# recover the interrupted jobs and compare with an uninterrupted run.
# This does NOT simulate loss of the OS page cache or a lying disk; see docs/OPERATIONS_AND_RECOVERY.md.
# usage: pg_crash_drill.sh DATA_DIR EXPECTED_ACCOUNTS EXPECTED_LINES
. "${CV_ENV_FILE:-/tmp/pgenv.sh}"
DATA=${1:?folder}; EXP_ACC=${2:?}; EXP_LINES=${3:?}
ROOT=${CV_ROOT:-/home/user/credvault}; PGB=$ROOT/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
PGDATA=${CV_PGDATA:-/tmp/pg-credvault-main-data}
PSQL="$PGB/psql -h $POSTGRES_HOST -U postgres -q -At"
cd $ROOT
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_drill WITH (FORCE)" -c "CREATE DATABASE cv_drill" || exit 1
export POSTGRES_DB=cv_drill
.venv/bin/python manage.py migrate -v0 || exit 1
.venv/bin/python manage.py importfolder $DATA --quiet > ${CV_BENCH:-/tmp/bench}/drill_import.log 2>&1 &
PID=$!
sleep ${KILL_AFTER:-40}
echo "stopping server immediately at $(date +%T)"
$PGB/pg_ctl -D $PGDATA -m immediate stop >/dev/null
sleep 2; pkill -9 -P $PID; kill -9 $PID 2>/dev/null; wait $PID 2>/dev/null
tail -n 3 ${CV_BENCH:-/tmp/bench}/drill_import.log
T0=$(date +%s.%N)
$PGB/pg_ctl -D $PGDATA -l /tmp/pg.log -w start >/dev/null
T1=$(date +%s.%N)
python3 -c "print('server restart + WAL crash recovery: %.1f s'%($T1-$T0))"
grep -E "redo|database system was not properly shut down|automatic recovery" /tmp/pg.log | tail -4
echo "after crash: accounts=$($PSQL cv_drill -c 'select count(*) from vault_account') ledger=$($PSQL cv_drill -c 'select count(*) from vault_importrecord')"
echo "-- amcheck (btree incl. heapallindexed) after crash recovery"
$PGB/pg_amcheck -h $POSTGRES_HOST -U postgres --heapallindexed --install-missing cv_drill > /tmp/bench/amcheck1.txt 2>&1; echo "amcheck exit $? (output lines: $(wc -l < /tmp/bench/amcheck1.txt))"
$PSQL cv_drill -c "update vault_importjob set lease_expires_at = now() - interval '1 second' where status='processing'"
T0=$(date +%s.%N)
.venv/bin/python manage.py recoverimports --run 2>&1 | grep -v import_metrics | tail -3
T1=$(date +%s.%N)
python3 -c "print('recoverimports --run: %.1f s'%($T1-$T0))"
echo "second recoverimports: $(.venv/bin/python manage.py recoverimports 2>&1 | tail -1)"
echo "final accounts=$($PSQL cv_drill -c 'select count(*) from vault_account') (expected $EXP_ACC)"
echo "ledger committed+skipped+failed by status:"; $PSQL cv_drill -c "select status, count(*) from vault_importrecord group by 1 order by 1"
echo "duplicate fingerprints: $($PSQL cv_drill -c 'select count(*) from (select fingerprint from vault_account group by 1 having count(*)>1) x')"
echo "ledger rows lacking an account although committed: $($PSQL cv_drill -c "select count(*) from vault_importrecord where status='committed' and account_id is null")"
$PGB/pg_amcheck -h $POSTGRES_HOST -U postgres --heapallindexed cv_drill > /tmp/bench/amcheck2.txt 2>&1; echo "final amcheck exit $? (output lines: $(wc -l < /tmp/bench/amcheck2.txt))"
