# ADR-007 — Local learning Airflow

| Field | Value |
|---|---|
| Decision | Two tracks in one repository. |
| Status | Recorded on 2026-10-26. The learning track is not a production migration. |
| Production scheduler | `python -m openfactors.orchestration.daily` |
| Learning scheduler | `ops/compose/learn/docker-compose.yml`, project `openfactors-learn` |
| Public release | No-go. This record does not approve a host or a public URL. |

## Production track

The production data platform stays the local Docker project in `ops/compose/docker-compose.yml`: PostgreSQL, the S3-compatible object store, and dbt Core through `transform/`. The public process stays `src/openfactors/api/catalog.py`. FastAPI is not added. The release command still does not deploy.

ADR-003 still chooses the local daily command as the scheduler. This record does not replace that command.

## Learning track

Airflow 2.10.5 runs only in the `openfactors-learn` project. Its web UI is published on 127.0.0.1:18080. It is not part of dev, test, or staging. It does not receive the production database password, and it does not attach the production Postgres or object-store ports.

The DAG `openfactors_daily_learning` is paused and has no schedule. Its one task is `python -m openfactors.orchestration.daily`. On 2026-10-26 that task was triggered once on this computer. It logged `step=weather_fetch category=config` and exited 1, the same closed result as the production command. It did not publish. A migration that makes Airflow the production scheduler is a later ADR.

dbt Core, PostgreSQL, and the S3-compatible store stay on the production project. A second copy is not started here, so the learning track cannot take their ports or their volumes.
