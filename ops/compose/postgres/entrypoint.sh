#!/bin/bash
set -euo pipefail

install -d -o postgres -g postgres -m 700 /var/lib/postgresql/certs
cp /certs/server.crt /var/lib/postgresql/certs/server.crt
cp /certs/server.key /var/lib/postgresql/certs/server.key
chown postgres:postgres /var/lib/postgresql/certs/server.crt /var/lib/postgresql/certs/server.key
chmod 644 /var/lib/postgresql/certs/server.crt
chmod 600 /var/lib/postgresql/certs/server.key

exec docker-entrypoint.sh postgres \
  -c ssl=on \
  -c ssl_cert_file=/var/lib/postgresql/certs/server.crt \
  -c ssl_key_file=/var/lib/postgresql/certs/server.key \
  -c hba_file=/etc/openfactors/pg_hba.conf
