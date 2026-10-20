#!/bin/bash
# Physical backup + point-in-time-recovery drill on the disposable cluster.
# Prereq: archive_mode=on, archive_command copies segments to $ARCHIVE; database cv_drill populated.
#   1. pg_basebackup (plain, WAL streamed)           2. durable import of more data after the backup
#   3. record T_good, then simulate an operator error (delete accounts)
#   4. restore the backup into a NEW directory, replay archived WAL to T_good, promote
#   5. compare row counts with those recorded at T_good, run pg_amcheck on the restored copy
# usage: backup_restore_drill.sh MORE_DATA_DIR
. "${CV_ENV_FILE:-/tmp/pgenv.sh}"
MORE=${1:?folder}
ROOT=${CV_ROOT:-/home/user/credvault}; PGB=$ROOT/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
ARCHIVE=${ARCHIVE:-/tmp/pg-archive}; BK=/tmp/pg-backup; RS=/tmp/pg-restore; RSOCK=/tmp/pg-restore-sock
PSQL="$PGB/psql -h $POSTGRES_HOST -U postgres -q -At"
export POSTGRES_DB=cv_drill
cd $ROOT
rm -rf $BK $RS $RSOCK; mkdir -p $RSOCK
T0=$(date +%s.%N)
$PGB/pg_basebackup -h $POSTGRES_HOST -U postgres -D $BK -Fp -X stream -c fast >/dev/null || exit 1
T1=$(date +%s.%N)
python3 -c "print('pg_basebackup: %.1f s, %s'%($T1-$T0, '$(du -sh $BK | cut -f1)'))"
A0=$($PSQL cv_drill -c "select count(*) from vault_account")
.venv/bin/python manage.py importfolder $MORE --quiet 2>&1 | grep -v import_metrics | tail -1
$PSQL postgres -c "select pg_switch_wal()" >/dev/null
sleep 2
A1=$($PSQL cv_drill -c "select count(*) from vault_account"); L1=$($PSQL cv_drill -c "select count(*) from vault_importrecord")
sleep 2
TGOOD=$($PSQL postgres -c "select to_char(now() at time zone 'utc','YYYY-MM-DD HH24:MI:SS.US')")
sleep 2
echo "backup had $A0 accounts; at T_good ($TGOOD UTC) accounts=$A1 ledger=$L1"
$PSQL cv_drill -c "delete from vault_importrecord where account_id in (select id from vault_account order by id limit 50000)" -c "delete from vault_account where id in (select id from vault_account order by id limit 50000)"
echo "after simulated operator error: accounts=$($PSQL cv_drill -c 'select count(*) from vault_account')"
$PSQL postgres -c "select pg_switch_wal()" >/dev/null; sleep 2
cp -a $BK $RS; rm -f $RS/postmaster.pid $RS/backup_label.old
cat >> $RS/postgresql.conf <<CONF
unix_socket_directories = '$RSOCK'
port = 5499
archive_mode = off
restore_command = 'cp $ARCHIVE/%f %p'
recovery_target_time = '$TGOOD+00'
recovery_target_action = promote
CONF
touch $RS/recovery.signal
T2=$(date +%s.%N)
$PGB/pg_ctl -D $RS -l /tmp/pg-restore.log -w start >/dev/null || { tail -20 /tmp/pg-restore.log; exit 1; }
RP="$PGB/psql -h $RSOCK -p 5499 -U postgres -q -At"
# pg_ctl -w returns once read-only connections are allowed; recovery may still be replaying WAL.
until [ "$($RP postgres -c 'select pg_is_in_recovery()')" = "f" ]; do sleep 0.5; done
T3=$(date +%s.%N)
python3 -c "print('restore + WAL replay to target + promote: %.1f s'%($T3-$T2))"
echo "restored: accounts=$($RP cv_drill -c 'select count(*) from vault_account') (expected $A1) ledger=$($RP cv_drill -c 'select count(*) from vault_importrecord') (expected $L1)"
echo "duplicate fingerprints on restored copy: $($RP cv_drill -c 'select count(*) from (select fingerprint from vault_account group by 1 having count(*)>1) x')"
$PGB/pg_amcheck -h $RSOCK -p 5499 -U postgres --heapallindexed cv_drill > /tmp/bench/amcheck_restore.txt 2>&1; echo "amcheck on restored copy exit $? (output lines: $(wc -l < /tmp/bench/amcheck_restore.txt))"
$PGB/pg_ctl -D $RS -m fast -w stop >/dev/null
rm -rf $BK $RS
