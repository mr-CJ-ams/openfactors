# Snapshot candidate

Public Beta v0.1 stays no-go. No host is purchased. These commands create a selective snapshot and restore it into an isolated candidate database. They do not rename databases, close the API, or replace a live serving database.

The development computer keeps the source database. A later cutover, which is not part of this command, would publish a candidate that has already passed these checks.

## Prerequisites

- The source environment is up: `python -m ops.local_stack up dev`.
- Migrations have been applied and one release is active and published.
- The target cluster has the roles `of_migrate`, `of_publish`, and `of_api`. `of_api` must be able to log in. The dump does not create roles.
- The target cluster does not already have a database named `openfactors_candidate`.
- The command restores and drops only `openfactors_candidate`. These names are rejected: `openfactors_dev`, `openfactors_test`, `openfactors_staging`, `openfactors_serving`, `openfactors_previous`, `openfactors_expired`, `postgres`, `template0`, `template1`, and `of_restore_drill`.
- A dump is read from `openfactors_dev`, `openfactors_test`, or `openfactors_staging`. The manifest records that source name, and a restore rejects any other source.

## Commands

Run these from the repository root.

Create the dump and manifest from the development database:

```text
python -m ops.publish_snapshot create dev
```

The files are `ops/local/runtime/dev/publications/<release_id>.dump` and the matching `.json` manifest. The manifest records the release id, public schema version, row count, canonical content SHA-256, dump SHA-256, source database, server version, and the exclude list. Passing `--output` chooses another directory. An existing dump for that release id is left untouched.

Restore and validate on the isolated test cluster:

```text
python -m ops.publish_snapshot restore-candidate test ops/local/runtime/dev/publications/<release_id>.dump
```

The manifest is the `.json` file beside the dump. The dump SHA-256 is checked before `openfactors_candidate` is created. A mismatch does not create a database. That checksum detects accidental corruption. It does not authenticate the publisher. Authenticated transfer or signature verification remains a later deployment gate. A failed restore or a failed check drops only `openfactors_candidate`. A candidate that is already present is left in place.

Remove a candidate after a drill:

```text
python -m ops.publish_snapshot abort-candidate test
```

That drop is limited to `openfactors_candidate`. It does nothing when the candidate is already absent.

## What the dump keeps

The custom-format dump excludes `staging` and these tables: `core.fact_daily_weather`, `core.fact_public_holidays`, `core.holiday_calendar_coverage`, `control.raw_manifests`, `control.ingestion_runs`, and `control.checkpoints`. Publish objects, quality validation runs, the source registry, schema migrations, and the date and location dimensions stay. Owners are preserved. The candidate check recomputes the content hash and requires `of_api` to read the catalog while private schemas stay closed.
