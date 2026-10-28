# Snapshot cutover

Public Beta v0.1 stays no-go. No host is purchased. These commands rename an already validated `openfactors_candidate` into `openfactors_serving` on the local test or staging cluster. They do not copy a snapshot to a VPS, and they do not change `openfactors_dev`.

PostgreSQL catalog rows in `pg_database` are the record of the cutover. There is no separate state file.

## Commands

Run these from the repository root after `restore-candidate` has left a validated candidate in place.

Promote that candidate. The confirmation is the candidate release id:

```text
python -m ops.publish_snapshot promote-candidate test --confirm-release <release-id>
```

Roll the serving database back to the previous generation. The confirmation is that previous release id. The generation that was serving is kept as `openfactors_candidate`:

```text
python -m ops.publish_snapshot rollback-serving test --confirm-release <release-id>
```

`staging` is the other accepted cluster. `dev` is rejected. The commands accept no database-name argument. Renames use only `openfactors_candidate`, `openfactors_serving`, `openfactors_previous`, and `openfactors_expired`.

## What a promotion does

The first promotion requires `openfactors_candidate` and requires `openfactors_serving` and `openfactors_previous` to be absent. It renames the candidate to `openfactors_serving` in one transaction.

A later promotion requires the candidate and the current serving database. The serving database is closed with a committed `ALLOW_CONNECTIONS false`, its sessions are terminated, and one transaction renames it to `openfactors_previous` while the candidate becomes `openfactors_serving`. The previous generation stays closed. The promoted database is opened and checked again.

If `openfactors_previous` already exists and `openfactors_expired` does not, that same transaction first renames previous to expired. An expired database is not dropped. While `openfactors_expired` exists, another promotion is refused.

A confirmation that does not match the candidate release id does not rename anything. The candidate is checked again before the rename: release id, row count, recomputed content hash, schema version, required objects, excluded objects, owners, and `of_api` permissions.

A candidate left by `rollback-serving` is a preserved serving generation. The same transaction that renames the databases writes the catalog comment `openfactors-demoted` on `openfactors_candidate`, and that comment commits with those renames. A failure before that `COMMIT` leaves the old names and does not leave the comment. `promote-candidate` and `abort-candidate` both refuse a database that has the comment. A new `restore-candidate` creates a database and has no such comment. The preserved generation stays until the separate cleanup command in `snapshot-cleanup.md`. It is not overwritten by promotion or rollback.

## Downtime

New connections to the serving database fail from the committed close until the promoted database accepts connections. Sessions that were already connected are terminated. The API keeps using the name `openfactors_serving` and sees the promoted database on its next connection. This window is not a measured recovery time.

The first promotion has no serving database to close.

## Recovery

`ALTER DATABASE ... RENAME` commits when the `COMMIT` in that psql script finishes. Every rename in that script is in one transaction, so PostgreSQL keeps all of those renames or none of them. The checks after that script — opening the promoted database, reading its release, and comparing its content hash — run only after that commit. They do not roll the rename transaction back. A failed check starts a second transaction that renames the databases back and then opens the restored serving database. The demoted-candidate comment is cleared in that same second transaction when the command is a rollback.

A SQL error before `COMMIT` rolls that whole transaction back. The names stay as they were when the transaction started, and no generation from it is partly renamed or deleted. The error says the compensating transaction failed before commit and was rolled back.

A lost connection is different. If `psql` does not receive the result of `COMMIT`, the commit is unconfirmed: either every rename in that transaction committed, or none did. The error says the commit was not confirmed. Inspect `pg_database` before retrying either command. Do not assume the names were restored. Nothing in that path drops or overwrites a generation.

Inspect the catalog from the repository root. This reads `pg_database` and does not rename anything:

```text
docker compose --project-name openfactors-test --env-file ops/local/runtime/test/secrets.env --file ops/compose/docker-compose.yml exec -T postgres psql --username of_admin --dbname postgres --command "SELECT datname, datallowconn FROM pg_database WHERE datname LIKE 'openfactors_%' ORDER BY 1;"
```

Run that from the repository root. For staging, use project name `openfactors-staging` and `ops/local/runtime/staging/secrets.env`. The command prints database names and connection flags. It does not print passwords.

When `datallowconn` is false for `openfactors_serving` and the names are still `openfactors_candidate` and `openfactors_serving`, the close committed and the rename did not. New API reads of `openfactors_serving` fail until a retry finishes. Sessions that were already connected stay until the retry terminates them. The API process is not restarted. Retry the same promotion command. It terminates the remaining sessions, renames the candidate into place, and opens the promoted database:

```text
python -m ops.publish_snapshot promote-candidate test --confirm-release <candidate-release-id>
```

When the rename committed and the process stopped before the final check, `openfactors_serving` already has the confirmed release and `openfactors_candidate` is absent. Retry the same command. It opens that serving database and checks it again. It does not create another database.

A database name that is present when the plan does not allow it, a previous generation that is still accepting connections, an expired generation, or a catalog comment other than the demoted mark stops the command before a rename.

Rollback does not need `openfactors_expired`. It renames the current serving database to `openfactors_candidate` and the previous database to `openfactors_serving` in one transaction, marks the demoted database, then opens both. The demoted generation remains available for inspection and is not a fresh publication candidate. Retrying a rollback that already renamed the databases checks the restored serving release and leaves `openfactors_expired` where it is. A confirmation for any other release is refused. The same API interruption applies while the serving database is closed: new reads fail, and they succeed again when the restored generation accepts connections. That window is not a measured recovery time.

## Limitations

The dump SHA-256 used to build the candidate detects accidental corruption. It does not authenticate the publisher. `snapshot-transfer-acceptance.md` accepts a signed upload into a directory. `restore-candidate` does not read that directory yet, so this cutover still does not check the signature.

`openfactors_previous` is the on-cluster copy used for rollback. It is not an independent offsite backup. Promotion and rollback do not delete `openfactors_previous` or `openfactors_expired`. While an expired generation remains, a further promotion waits. The separate cleanup command in `snapshot-cleanup.md` can remove a confirmed expired generation. It refuses a demoted candidate while `openfactors_previous` is absent, because that candidate is the only on-cluster copy of its release. It does not remove `openfactors_previous`. A later recovery or backup-preservation workflow has to clear that candidate before another restore can use the name.

The development cluster is not a cutover target. Promotion, rollback, restore, and abort share the advisory lock described in `snapshot-cleanup.md`. A manual administrator session does not take that lock, so SQL entered by hand can still change the catalog. Public Beta v0.1 remains no-go.
