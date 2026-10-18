#!/bin/bash
. "${CV_ENV_FILE:-/tmp/pgenv.sh}"
PGB=${CV_ROOT:-/home/user/credvault}/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
PSQL="$PGB/psql -h /tmp/pg-credvault-main -U postgres -q -At"
for spec in "200k:${CV_BENCH:-/tmp/bench}/g200k_c" "1M:${CV_BENCH:-/tmp/bench}/g1m_b"; do
  L=${spec%%:*}; P=${spec#*:}
  $PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)" -c "CREATE DATABASE cv_run TEMPLATE credvault"
  POSTGRES_DB=cv_run ${CV_ROOT:-/home/user/credvault}/.venv/bin/python ${CV_ROOT:-/home/user/credvault}/manage.py migrate -v0
  POSTGRES_DB=cv_run ${CV_ROOT:-/home/user/credvault}/.venv/bin/python ${CV_BENCH:-/tmp/bench}/mem_run.py $L $P
done
$PSQL postgres -c "DROP DATABASE IF EXISTS cv_run WITH (FORCE)"
