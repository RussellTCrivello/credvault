#!/bin/bash
# Stale planner statistics on vault_account / vault_importrecord (section 25 of
# docs/scalability/BENCHMARK_RESULTS.md).  Needs the sandbox from setup_sandbox.sh.
#
# Loads 600,000 lines, snapshots the database as a template, then for each
# scenario recreates the database from the snapshot, optionally forces the
# statistics to "1 page, 0 rows" (what ANALYZE leaves after a purge), and
# imports the same 250,000-line file with a hard time cap.
#
# On commit 9d63660 (before the fix) the "stale_account" scenario did not finish
# within 600 s; with the fix all three finish in about 30 s.
# usage: benchmarks/stale_stats_experiment.sh [CAP_SECONDS=300]
set -uo pipefail
. /tmp/pgenv.sh
ROOT=${CV_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}; cd "$ROOT"
CAP=${1:-300}; W=${CV_WORK:-/tmp/ss}; mkdir -p $W
PGB=$ROOT/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
PA="$PGB/psql -h $POSTGRES_HOST -U postgres -q -At"
export VAULT_IMPORT_WORKERS=2 VAULT_PG_ADVISORY=0
gen() { .venv/bin/python manage.py perfbench generate $W/$1 --lines $2 --files 1 --duplicate-rate 0.04 --invalid-rate 0.09 --seed $3 >/dev/null 2>&1; }
[ -d $W/base ] || gen base 600000 10
[ -d $W/file ] || gen file 250000 12
recreate() { $PA -d postgres -c "DROP DATABASE IF EXISTS credvault WITH (FORCE)"; $PA -d postgres -c "CREATE DATABASE credvault $1"; }
recreate ""; $PA -d credvault -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
.venv/bin/python manage.py migrate --noinput >/dev/null 2>&1
.venv/bin/python manage.py importfolder $W/base --quiet >/dev/null 2>&1
BASE=$($PA -d credvault -c "select count(*) from vault_account")
$PA -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='credvault'" >/dev/null
$PA -d postgres -c "DROP DATABASE IF EXISTS snap WITH (FORCE)"; $PA -d postgres -c "CREATE DATABASE snap TEMPLATE credvault"
NOAV="ALTER TABLE vault_account SET (autovacuum_enabled=false); ALTER TABLE vault_importrecord SET (autovacuum_enabled=false);"
run() {  # name, setup sql
  recreate "TEMPLATE snap"; $PA -d credvault -c "$NOAV $2" >/dev/null
  T0=$(date +%s.%N)
  timeout -s KILL $CAP .venv/bin/python manage.py importfolder $W/file --quiet > $W/out_$1.txt 2>&1; RC=$?
  T1=$(date +%s.%N)
  $PA -d postgres -c "select pg_terminate_backend(pid) from pg_stat_activity where datname='credvault' and pid<>pg_backend_pid()" >/dev/null
  NOW=$($PA -d credvault -c 'select count(*) from vault_account')
  python3 -c "print('$1: rc=$RC seconds=%.1f new_accounts=%d' % ($T1-$T0, $NOW-$BASE))"
}
run control_healthy        "ANALYZE vault_account; ANALYZE vault_importrecord;"
run stale_account          "UPDATE pg_class SET reltuples=0, relpages=1 WHERE relname='vault_account';"
run stale_ledger           "UPDATE pg_class SET reltuples=0, relpages=1 WHERE relname='vault_importrecord';"
