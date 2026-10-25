#!/bin/bash
set -euo pipefail

install -d -m 700 /certs
if [[ ! -f /certs/server.crt || ! -f /certs/server.key ]]; then
  openssl req -new -x509 -days 825 -nodes \
    -subj "/CN=openfactors-local" \
    -addext "subjectAltName=DNS:localhost,DNS:postgres,DNS:minio,IP:127.0.0.1" \
    -keyout /certs/server.key \
    -out /certs/server.crt
fi
cp /certs/server.crt /certs/public.crt
cp /certs/server.key /certs/private.key
chmod 600 /certs/server.key /certs/private.key
