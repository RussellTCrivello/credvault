#!/bin/bash
# usage: exp_import.sh NAME DIR [importfolder args...]  -> one JSON line (seconds, accounts added, WAL bytes)
. "${CV_ENV_FILE:-/tmp/pgenv.sh}"
NAME=$1; DIR=$2; shift 2
ROOT=${CV_ROOT:-/home/user/credvault}
PGB=$ROOT/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
PSQL="$PGB/psql -h $POSTGRES_HOST -U postgres -q -At -d $POSTGRES_DB"
cd $ROOT
L0=$($PSQL -c "select pg_current_wal_lsn()"); A0=$($PSQL -c "select count(*) from vault_account")
T0=$(date +%s.%N)
.venv/bin/python manage.py importfolder $DIR --quiet "$@" > ${CV_BENCH:-/tmp/bench}/exp_$NAME.log 2>&1; RC=$?
T1=$(date +%s.%N)
L1=$($PSQL -c "select pg_current_wal_lsn()"); A1=$($PSQL -c "select count(*) from vault_account")
W=$($PSQL -c "select pg_wal_lsn_diff('$L1','$L0')")
python3 -c "import json;s=$T1-$T0;a=$A1-$A0;print(json.dumps(dict(name='$NAME',rc=$RC,seconds=round(s,1),accounts_before=$A0,accounts_added=a,accounts_per_second=round(a/s,1),wal_bytes=$W,shared_buffers='$($PSQL -c 'show shared_buffers')')))"
