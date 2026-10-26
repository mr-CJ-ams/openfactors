# OpenFactors

Analytics-ready daily weather and Philippine national-holiday factors. The product decisions live in `docs/decisions`. This repository is the local monorepo for that work. Nothing here is a public deployment.

| Path | What will live here |
|---|---|
| `ingestion` | Python connectors and raw-file writes |
| `transform` | dbt models |
| `serving/api` | Read-only API |
| `serving/web` | Catalog |
| `ops` | Local scheduler and recovery commands |
| `contracts` | Fixtures and the contract check |
| `docs/decisions` | Approved baselines |

## Checks

Python 3.13. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
.venv\Scripts\python tools\scan_secrets.py
.venv\Scripts\python -m contracts.check_dataset_contract
.venv\Scripts\pytest
```

The same commands are the GitHub Actions job named `ci`.

## Local database

Docker Desktop is required. dev, test, and staging each get their own Postgres database, object-storage bucket, ports, and volume. Production is not included. Published ports are bound to 127.0.0.1. Dev Postgres is 55430, test is 55432, and staging is 55433.

```powershell
.venv\Scripts\python -m ops.local_stack up dev
.venv\Scripts\python -m ops.migrate dev
.venv\Scripts\python -m ops.local_stack backup dev
.venv\Scripts\python -m ops.local_stack smoke
.venv\Scripts\python -m ops.local_stack down dev --volumes
```

`python -m ops.migrate dev` creates the control-plane tables, loads the two approved sources, loads the five pilot locations and the Asia/Manila date spine, and grants the API role read access to `publish.daily_factors` only. `dbt build` in `transform/` parses landed payloads into staging views, keeps one weather revision per location-day, broadcasts the winning national holiday calendar, and builds the 12-column candidate mart. A candidate with a bad schema, duplicate key, or unknown location, date, or source is not published. Coverage below 99 percent of the approved location-dates, a negative precipitation, or an unverified holiday also blocks publication. A candidate release stores an immutable content id, schema version 1.0.0, the source-manifest hash, the content checksum, and the linked quality result. Promotion copies an approved candidate into the fact table and switches the active pointer in one transaction. A rejected candidate, or a promotion that fails before commit, is not served. Rollback points the view at an older published release, and that release stays stored. Suspending a source withdraws every published release that contains it, removes that release's local export files, and resolves the release as 404. There is no public cache. Raw files stay. GET /api/v1/datasets lists the active published dataset with its coverage, license, and publication time. GET / shows that same catalog as a page, including geography, grain, the last successful publication, terms, sources, and gaps. The dictionary page lists the 12 fields and pins one release on a CSV or Parquet download. Invalid dates stay on the form. GET /docs is the API guide. Its examples are unauthenticated GET requests, and a later page repeats the release id from the first response. `python -m openfactors.orchestration.daily` is the local daily attempt. It is not an HTTP route. A failed step does not publish, and a failed attempt leaves a log line and a failed run record. Historical backfill is the local command `python -m openfactors.orchestration.backfill`: it checks the interval, records the operator, and `--dry-run` does not publish. `python -m openfactors.orchestration.status` is the local operations report. It keeps the last attempt, the last successful run, and the last publication as separate times. `python -m ops.local_stack restore-check` loads a backup into a separate database, checks it, and removes that database. Hosted recovery-time targets are unset. A withdrawn release is left out. An unknown query parameter returns 422. GET /api/v1/datasets/daily-external-factors-ph returns the 12-field dictionary, sources, and limitations for the active release or a pinned published release. An unknown or withdrawn pin returns 404. GET /api/v1/factors/daily returns one location for an inclusive date range, pinned to one published release, in date order. Page size is 1 to 1000, and a page past the end is 200 with an empty list. Public API errors use one envelope with a trace id. A refused request returns 429 and Retry-After. A loopback client is not rate limited. Any other client is limited to 60 requests per 60 seconds, provisional until staging measurements. GET /openapi.json serves the approved OpenAPI 3.1 document. CI diffs it against the implemented routes. GET /api/v1/exports/daily-external-factors-ph?format=csv returns the same 12 fields as the factor query. A formula-like holiday name is prefixed with an apostrophe. `format=parquet` returns those same rows, with date32, decimal measurements, a UTC timestamp, and the release id in the file metadata. An export above 100,000 rows or 50,000,000 bytes returns 422 and no file. A completed export includes an ETag. Attribution is on the dataset dictionary and in a JSON manifest. No date-span maximum is set. A raw manifest row cannot be updated or deleted. Postgres and object storage accept connections on localhost only. `smoke` starts the test project, checks that the API role can use only the `publish` schema, checks that a connection without SSL is refused, writes a backup, restores it, and deletes the test volumes. Passwords are generated under `ops/local/runtime/`, which is not committed. Postgres listens with TLS. Object storage listens with TLS and a local self-signed certificate. The object-storage image is `pgsty/silo`, a community MinIO-compatible fork, because the official `minio/minio` image is no longer on Docker Hub. The public role cannot read raw or staging data or write, Postgres refuses a connection without TLS, and the API listens on 127.0.0.1 unless a bind host is configured. `python -m openfactors.orchestration.measure` records a local factors-query sample, the coverage counts, and the 30-day reliability plan. The p95 target is not claimed on this computer. `python -m openfactors.orchestration.accept` prints the release checklist. The decision is no-go, and the command does not sign it. `python -m openfactors.orchestration.release` records the public beta as not deployed. It does not create a release tag or a public address. `examples/join_factors.ipynb` and `examples/join_factors.sql` join sample orders to one pinned release. The orders are sample data. The API guide at `/docs` records schema 1.0.0, and a value change is a new release id. No public field is deprecated. `python -m openfactors.orchestration.backfill_ui` is the local operator form. It is not part of the public API. Accounts and saved workspaces are not part of the API. Monthly economic indicators are not in this dataset. They are not copied onto daily rows. The product owner confirmed the five pilot cities on 2026-10-25. Holiday years 2018 through 2026 are transcribed from Official Gazette proclamations. An incomplete year stays out of the release. The raw ERA5 responses for 2018-01-01 through 2026-10-25 are stored for the five pilot cities. The archive left 2026-10-20 through 2026-10-25 null. `python -m ops.load_stored` loads that calendar and those responses into the local database, rebuilds the candidate view, and promotes that release locally. `python -m openfactors.api.catalog` listens on http://127.0.0.1:8000 unless OPENFACTORS_API_BIND_HOST is set, and reads the published release as of_api through the psql client. Local database TLS for that client is sslmode=require, which encrypts the session and does not verify the self-signed certificate. A configured host must use sslmode=verify-full and a trusted CA file. `python -m examples.join_factors` joins the two sample Manila orders to that release. The orders are sample data. ADR-006 proposes a zero-cost public beta and does not approve a host or a public address. ADR-007 adds a local learning Airflow project that runs the production daily command and does not replace it. ADR-008 reconsiders the 366-day query span and does not enforce it. The public release decision stays no-go.

## Reviews and branch protection

When this project has a GitHub remote, protect the default branch:

1. Require a pull request before merging.
2. Require the status check named `ci`.
3. Put a real GitHub login in `.github/CODEOWNERS`, then require review from code owners.
4. Disallow force pushes.

Those settings are on the GitHub repository. They are not stored in git itself. The monthly cost cap is USD 0, so this skeleton uses GitHub-hosted Actions only if that stays free.
