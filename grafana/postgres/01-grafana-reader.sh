#!/bin/bash
# Design 018: the read-only role Grafana queries with, created once, on the first
# start of an empty volume (the image runs /docker-entrypoint-initdb.d/ then).
# Grafana runs dashboard SQL exactly as written, so this role keeps an edited
# panel from changing data. "Future" tables are covered by ALTER DEFAULT
# PRIVILEGES, which applies only to tables created by the role that ran it: the
# superuser, which is also the role the ingest creates the tables as.
set -euo pipefail

psql -v ON_ERROR_STOP=1 -v reader_password="$GRAFANA_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE grafana_reader LOGIN PASSWORD :'reader_password';
GRANT CONNECT ON DATABASE wingman TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT ON TABLES TO grafana_reader;
SQL
