#!/bin/bash
# usage: run_variant.sh NAME [perfbench concurrent args...]
. "${CV_ENV_FILE:-/tmp/pgenv.sh}"
NAME=$1; shift
PGB=${CV_ROOT:-/home/user/credvault}/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
PSQL="$PGB/psql -h /tmp/pg-credvault-main -U postgres -q"
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)" -c "CREATE DATABASE cv_run TEMPLATE credvault" || exit 1
export POSTGRES_DB=cv_run
cd ${CV_ROOT:-/home/user/credvault}; [ "$MIGRATE" = 1 ] && .venv/bin/python manage.py migrate -v0
.venv/bin/python manage.py perfbench concurrent "$@" > ${CV_BENCH:-/tmp/bench}/conc_$NAME.json 2>${CV_BENCH:-/tmp/bench}/conc_$NAME.err
echo exit $?
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)"
