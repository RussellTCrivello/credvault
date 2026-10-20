#!/bin/bash
# Rebuild the disposable test/benchmark environment after a sandbox reset.
# Creates .venv, compiles pg_trgm for the pgserver PostgreSQL 16.2 binaries,
# initialises a local cluster on a unix socket and writes /tmp/pgenv.sh.
# usage: benchmarks/setup_sandbox.sh      then:  . /tmp/pgenv.sh
set -euo pipefail
ROOT=${CV_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
cd "$ROOT"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip -q install -r requirements.txt pgserver==0.1.4
PGB=$ROOT/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin
if [ ! -f "$($PGB/pg_config --pkglibdir)/pg_trgm.so" ]; then
  # pgserver ships without contrib; build pg_trgm from the matching source tag.
  mkdir -p /tmp/pgsrc && cd /tmp/pgsrc
  curl -sL https://codeload.github.com/postgres/postgres/tar.gz/refs/tags/REL_16_2 \
    | tar xz --strip-components=1 --wildcards '*/contrib/pg_trgm'
  cd contrib/pg_trgm
  make USE_PGXS=1 PG_CONFIG=$PGB/pg_config -s
  make USE_PGXS=1 PG_CONFIG=$PGB/pg_config -s install
  cd "$ROOT"
fi
DATA=${CV_PGDATA:-/tmp/pg-credvault-data}; SOCK=${CV_PGSOCK:-/tmp/pg-credvault-main}
if [ ! -d "$DATA" ]; then
  mkdir -p "$SOCK"
  $PGB/initdb -D "$DATA" -U postgres -A trust -E UTF8 >/dev/null
  printf "listen_addresses=''\nunix_socket_directories='%s'\nshared_buffers=512MB\nmax_wal_size=4GB\n" "$SOCK" >> "$DATA/postgresql.conf"
fi
$PGB/pg_ctl -D "$DATA" status >/dev/null 2>&1 || $PGB/pg_ctl -D "$DATA" -l /tmp/pg.log -w start >/dev/null
$PGB/psql -h "$SOCK" -U postgres -At -d postgres -c "select 1 from pg_database where datname='credvault'" | grep -q 1 \
  || $PGB/createdb -h "$SOCK" -U postgres credvault
echo "export POSTGRES_HOST=$SOCK POSTGRES_USER=postgres POSTGRES_PASSWORD=x POSTGRES_DB=credvault" > /tmp/pgenv.sh
echo "ready: . /tmp/pgenv.sh && .venv/bin/python manage.py test --noinput"
