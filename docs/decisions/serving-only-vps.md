# Serving-only VPS specification

Public Beta v0.1 stays no-go. No host is purchased. This file describes the approved serving layout. It does not deploy it, and it does not add snapshot cutover or backup automation.

The development computer keeps ingestion, the raw archive, dbt, validation, and release promotion. The VPS, when one is later approved, runs three things: an HTTPS reverse proxy, the standard-library API, and a PostgreSQL serving copy. Silo, Airflow, and private batch jobs stay off that machine for v0.1.

A published release reaches the VPS as a database snapshot. Validation of that snapshot, the cutover, and rollback are a later change. This specification does not implement them. Validation must use a separate database. It must not overwrite the live serving database.

## Processes

| Process | Where | Role |
|---|---|---|
| Caddy, or another HTTPS proxy | VPS, not in this repository yet | Public 443. Sends one client-address chain to the API. |
| `python -m openfactors.api.catalog` | VPS | The existing API, bound to loopback. |
| PostgreSQL 16.10 | VPS | Serving copy of published rows. Loopback only. |
| Ingestion, dbt, promotion, Silo | Development computer | Writes the release that a later snapshot will carry. |

The learning Airflow project is not part of this layout.

## Ports

| Port | Bind | Purpose |
|---|---|---|
| 443 | Public | HTTPS for the API. |
| 80 | Public, proxy only | Certificate issuance, then redirect to HTTPS. |
| 8000 | `127.0.0.1` | The API. Not published on the public interface. |
| 5432 | `127.0.0.1` | Postgres on the VPS. Not published. |

Dev on this computer uses host port 55430 so it does not collide with a local Postgres. That exception is not the VPS layout.

## Environment

The API already reads these variables. A serving host sets them. It does not invent new ones in this change.

| Variable | Serving value |
|---|---|
| `OPENFACTORS_API_BIND_HOST` | `127.0.0.1` |
| `OPENFACTORS_API_BIND_PORT` | `8000` |
| `OPENFACTORS_API_TRUSTED_PROXIES` | The proxy's TCP address. For a proxy on the same machine that is `127.0.0.1`. The proxy sends one `X-Forwarded-For` or `Forwarded` chain. Both headers together are a malformed request. |
| `OPENFACTORS_API_PGHOST` | A host name that matches the Postgres certificate. Setting it selects `sslmode=verify-full`. |
| `OPENFACTORS_API_PGPORT` | The loopback Postgres port. |
| `OPENFACTORS_API_PGDATABASE` | The serving database name. |
| `OPENFACTORS_API_PGPASSWORD` | The `of_api` password. Not an argument to `psql`. |
| `OPENFACTORS_API_PGSSLMODE` | `verify-full` |
| `OPENFACTORS_API_PGSSLROOTCERT` | The trusted CA file for that certificate. |
| `OPENFACTORS_API_PGPASS_DIR` | The private password directory. |
| `OPENFACTORS_API_EXPORT_DIR` | Directory for CSV and Parquet files the API writes. |
| `OPENFACTORS_API_PSQL` | Optional path to the `psql` client. |

`OPENFACTORS_API_PGUSER`, when set, must be `of_api`.

Leaving `OPENFACTORS_API_PGHOST` unset is the local development mode. It uses `sslmode=require` and does not verify the certificate. The serving layout does not use that mode.

The proxy must be the trusted peer. Otherwise every public caller appears as loopback and the 60-request window does not apply. A direct connection to port 8000 from the internet must be impossible, because that peer could supply its own forwarding header.

## Volumes

| Volume | Contents |
|---|---|
| Postgres data | The serving database. A validation restore uses a different database name. |
| Postgres certificates | The server certificate and the CA the API trusts. |
| `OPENFACTORS_API_PGPASS_DIR` | Mode `0700` on Unix. Password files only. |
| `OPENFACTORS_API_EXPORT_DIR` | Finished exports and their manifests. |

The password directory is not the system temporary directory. On Unix it is mode `0700` and owned by the user the API runs as, so other accounts cannot open it. Each API query writes `openfactors-api-<token>.pgpass` there and deletes that file when `psql` exits. Catalog startup takes an exclusive lock on that directory and holds the lock until the process exits. Only the process that holds the lock deletes recognized password files. A second API process cannot use the same directory while the lock is held, so it cannot delete a live password file. Other names, directories, and symbolic links stay. Cleanup does not follow a symbolic link. On Windows, Python does not store Unix mode bits, so `0700` is the requested mode and a POSIX host is where the mode can be checked. The Windows process lock is the file `.openfactors-api.lock` inside the directory. A POSIX host locks the directory itself.

Silo's `minio-data` volume is not part of the v0.1 VPS. The raw archive stays on the development computer. An encrypted copy of that archive and of the database snapshot must exist somewhere that is neither the development computer nor the VPS disk before a public launch. That copy is not built in this change.

## Resource assumptions

These are not measurements of a VPS.

The candidate named in the Phase 5.1 review is OVHcloud VPS-1 as shown on the order page on 2026-10-27: 2 vCores, 4 GB RAM, 40 GB NVMe, displayed from $4.54 per month. It is not a purchased server. Tax, contract length, and region were not on a completed order screen.

On this computer, on 2026-10-27, the dev custom dump is 3,523,158 bytes and the raw archive is 378,455 bytes. ADR-008 recorded a 25,681,920-byte fact relation on 2026-10-26. Docker image size and the resident memory of Postgres plus the API plus four `psql` processes are unmeasured. Staging has to record them before Silo or dbt is considered for the same machine.

The API limits that already exist, and that a proxy test must exercise before a public name is used, are 60 requests per rolling 60 seconds for a non-loopback client, a 5-second statement timeout, 4 concurrent `psql` processes, a 10-second process deadline, and a 30-second socket timeout. Those numbers are provisional until staging measurements.

## What this change does not do

It does not select a datacentre, buy a VPS, open an account, or start a public listener. It does not ship a snapshot, cut over a serving database, or schedule an offsite backup. Public Beta v0.1 stays no-go.
