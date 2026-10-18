#!/bin/bash
# usage: run_batch.sh LABEL ENV=VAL... -- importfolder args
. "${CV_ENV_FILE:-/tmp/pgenv.sh}"
LABEL=$1; shift
PGB=${CV_ROOT:-/home/user/credvault}/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
PSQL="$PGB/psql -h /tmp/pg-credvault-main -U postgres -q -At"
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)" -c "CREATE DATABASE cv_run TEMPLATE credvault"
export POSTGRES_DB=cv_run
cd ${CV_ROOT:-/home/user/credvault}
.venv/bin/python manage.py migrate -v0
W0=$($PSQL cv_run -c "select wal_bytes from pg_stat_wal"); S0=$($PSQL cv_run -c "select pg_database_size('cv_run')")
A0=$($PSQL cv_run -c "select count(*) from vault_account")
T0=$(date +%s.%N)
env "$@" 2>&1 | tail -2 | head -1 | cut -c1-150
T1=$(date +%s.%N)
W1=$($PSQL cv_run -c "select wal_bytes from pg_stat_wal"); S1=$($PSQL cv_run -c "select pg_database_size('cv_run')")
A1=$($PSQL cv_run -c "select count(*) from vault_account")
python3 - <<PY
t=$T1-$T0; a=$A1-$A0
print("$LABEL", "seconds=%.1f"%t, "accounts_added=%d"%a, "acct_per_s=%.0f"%(a/t), "wal_MB=%.0f"%((float($W1)-float($W0))/1e6), "db_growth_MB=%.0f"%(($S1-$S0)/1e6))
PY
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)"
