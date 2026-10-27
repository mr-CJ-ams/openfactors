# Snapshot generation cleanup

Public Beta v0.1 stays no-go. No host is purchased. This command drops one obsolete database on the local test or staging cluster. It does not run as part of promotion or rollback, and it does not change `openfactors_dev`.

`openfactors_previous` stays. It is the rollback generation. This command does not archive a database before dropping it.

## Commands

Run these from the repository root. The target and the release id are both required.

Inspect an expired generation and do not drop it:

```text
python -m ops.publish_snapshot cleanup-generation test --target openfactors_expired --confirm-release <release-id> --dry-run
```

Drop that expired generation after the dry run says it is allowed:

```text
python -m ops.publish_snapshot cleanup-generation test --target openfactors_expired --confirm-release <release-id>
```

A demoted candidate is dropped only when `openfactors_previous` is still present. After rollback that database is absent, so the command refuses the candidate. The release id is the demoted generation, not the serving generation:

```text
python -m ops.publish_snapshot cleanup-generation test --target openfactors_candidate --confirm-release <release-id>
```

`staging` is the other accepted cluster. `dev` is rejected. The only accepted targets are `openfactors_expired` and `openfactors_candidate`.

## What the command checks

A dry run prints JSON and does not drop a database. The fields are the target, the release id read from that database, whether it is open or closed, the number of other sessions, the serving and previous connection states, and why cleanup is allowed or refused.

An expired database is allowed only when it is closed, its published release id matches the confirmation, and that release is not the serving release. The serving database must be open, must accept an `of_api` connection, and must have its own published release. A catalog comment is not enough, and the name is not enough. An open expired database is refused.

A candidate is allowed only when its catalog comment is `openfactors-demoted`, `openfactors_previous` is present, and the published release inside the candidate matches the confirmation and differs from the serving release. A fresh restore has no such comment and is refused. `abort-candidate` still refuses a demoted candidate. Only this command drops one.

After rollback, `openfactors_previous` is absent and the demoted candidate is the only on-cluster copy of its release. Cleanup refuses that candidate. There is no bypass. `restore-candidate` cannot use the name `openfactors_candidate` while that database remains. A later recovery or backup-preservation workflow has to remove it. Until then, another restore waits.

`openfactors_previous` is not a target. If it is open, cleanup is refused. If it is closed, it is left in place. `openfactors_serving`, the source databases, `postgres`, and the template databases are refused before any cleanup SQL.

Promotion, rollback, restore, abort, and cleanup take one PostgreSQL advisory lock for the cluster. The lock is held by one PostgreSQL session. That session stays connected until the command finishes, including `DROP DATABASE` or the renames. Before a connection change, a session termination, a rename, or `DROP DATABASE`, the command checks that this same session still holds the lock. If that session has disconnected, the command stops and leaves the databases as they are. Inspect `pg_database` before retrying. A second generation command is refused while the lock is held. The lock does not replace the release-id checks.

A manual administrator session does not take this advisory lock. SQL entered by hand can still change the catalog while a generation command is running. The lock serializes these generation commands only.

If another session is connected to the target, cleanup stops. It does not terminate that session. A closed target is opened only long enough to read its release id, then closed again. If that close fails, the command stops and names the recovery step: inspect `pg_database` and set `ALLOW_CONNECTIONS false` before retrying. The database is not dropped.

The drop runs only after those checks pass a second time. The statement is `DROP DATABASE openfactors_expired` or `DROP DATABASE openfactors_candidate`. It has no `CASCADE` and no `IF EXISTS`. That statement is sent on the lock-holding session. A disconnected lock session cannot run it. PostgreSQL commits that statement on its own. A successful drop cannot be rolled back. If the result is not confirmed, inspect `pg_database` before retrying. A second attempt finds the database absent and drops nothing.

## Failure handling

A wrong release id, a missing serving database, a closed serving database, an active session, or an unexpected comment leaves the target in place. Read the JSON reason from a dry run, or the error from the command. Then inspect the catalog:

```text
docker compose --project-name openfactors-test --env-file ops/local/runtime/test/secrets.env --file ops/compose/docker-compose.yml exec -T postgres psql --username of_admin --dbname postgres --command "SELECT datname, datallowconn FROM pg_database WHERE datname LIKE 'openfactors_%' ORDER BY 1;"
```

Run that from the repository root. It prints names and connection flags. It does not print passwords.

## Disk space

`DROP DATABASE` deletes that database's files on the PostgreSQL volume. Publication dumps under `ops/local/runtime` stay. The command does not copy the generation anywhere else first. Keep an independent copy if the rows are still needed. Free space returns to the database volume. This is not a measured capacity plan.

While `openfactors_expired` exists, another promotion waits. After this command removes it, a later promotion can rename `openfactors_previous` to `openfactors_expired` again. Public Beta v0.1 remains no-go.
