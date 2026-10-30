# Native PostgreSQL on the serving host

Public Beta v0.1 stays no-go. No host is purchased. Nothing in this repository connects to a VPS, edits `pg_hba.conf`, creates a production role, or runs a production restore. The commands below are for a later approval. Do not run them until that approval is given.

Local dev, test, and staging stay on Docker Compose. That path is the default. The native path starts only when the environment variable `OPENFACTORS_DB_BACKEND` is exactly `native`. Any other value, including an empty value other than the unset default, stops before `CREATE DATABASE`. The native path does not fall back to Docker.

## What runs where

The accepted snapshot is already on the serving host at `/var/openfactors/accept/accepted/<release-id>`. `restore-accepted` still requires Linux and user id 0. It still checks `READY`, the pinned signature, and the dump checksum before it creates a database.

Root is not the database user. PostgreSQL peer authentication maps the operating-system user to a database role of the same name. Root would not match `of_restore`. The program therefore starts `psql` and `pg_restore` with:

```text
runuser -u of_restore -- psql --host /var/run/postgresql --username of_restore --no-password
```

`runuser` is the dedicated operating-system identity. Peer then maps OS user `of_restore` to database role `of_restore`. Do not use trust. Do not add a `pg_ident` line that maps root to `of_restore`. Do not replace existing authentication rules with a weaker rule.

The accepted snapshot stays owned by `of_publish_accept` and mode `0440`. `of_restore` cannot read that directory. Root has already verified the signature and the dump checksum, then copies only the dump into a per-restore handoff:

```text
/var/openfactors/restore/<private-name>.handoff/<release-id>.dump
```

`/var/openfactors/restore` must be mode `0710` and group `of_restore`. That lets `of_restore` traverse a path it is given, and it does not let `of_restore` list or write the directory. The handoff directory is mode `0550` `root:of_restore`. The dump file is mode `0440` `root:of_restore`. `of_restore` can read that one file and cannot replace it, chmod it, or write it. The private directory that holds the manifest and signature stays mode `0700` and owned by root. The handoff is removed in a `finally` block after success or failure. The accepted snapshot is left in place. Do not use mode `0777`, a world-readable dump, or a handoff directory that `of_restore` can write.

The disposable integration test uses the same idea inside a PostgreSQL 16.10 container: `docker exec -u of_restore`. That container is not the VPS. The test harness variables `OPENFACTORS_NATIVE_TEST_HARNESS` and `OPENFACTORS_NATIVE_DOCKER_CONTAINER` must be unset on the serving host. The harness does not use the production handoff path.

## Database roles

`of_restore` is `LOGIN`, `NOSUPERUSER`, `CREATEDB`, `NOCREATEROLE`, and `NOINHERIT`. `CREATEDB` lets it create `openfactors_candidate` and own that database, which is what a later rename requires. `NOINHERIT` means membership does not grant table rights until `SET ROLE`.

`of_migrate` and `of_publish` are `NOLOGIN`. `of_restore` is a member of both, without `ADMIN OPTION`, so `pg_restore` can run `OWNER TO` for those roles. Restored tables and views must end owned by `of_migrate` or `of_publish`, not by `of_restore` or `of_api`.

`of_api` stays `NOSUPERUSER`, `NOCREATEDB`, and `NOCREATEROLE`. The bootstrap sets `NOINHERIT` on `of_api`. That does not grant any right. Direct `SELECT` grants on the public catalog views stay the only API access. `of_restore` is also a member of `of_api`, without `ADMIN OPTION`, so the restore can `SET ROLE of_api` and prove that private relations are denied. `of_restore` cannot turn `of_api` into a superuser.

Native `pg_restore` uses `--no-owner` and `--no-acl`. `of_restore` is `NOINHERIT`, so it cannot create tables in a schema already owned by `of_migrate`. Objects are created by `of_restore` and then `ops/sql/serving_restore_privileges.sql` gives `control` and `core` to `of_migrate`, and `publish` and `quality` to `of_publish`. A dump cannot grant `of_api` extra rights. The candidate is dropped if validation fails, if an object has an unexpected owner, or if `of_api` can read a private relation.

`of_api` remains strictly read-only. This design does not give it `CREATEDB`, writes, or superuser.

## Local proof

From the repository root, with Docker running:

```text
python -m pytest tests/test_native_backend.py tests/test_native_postgres_integration.py -q
```

Unset `OPENFACTORS_REQUIRE_DATABASE` for this focused run. The integration test starts `postgres:16.10-bookworm`, replaces its host-based authentication file with peer for `of_restore` and `reject` for everyone else, and removes the container at the end. It checks native restore, object ownership, the advisory lock, candidate validation, promotion, and rollback. It also checks that a Linux root client is not accepted as `of_restore`.

The fixture dump is a stand-in with the required objects and owners. It is not a production snapshot.

## Manual steps, after a separate approval

Do not run this section now.

### 1. Confirm the host

What: Confirm the serving machine is Ubuntu and PostgreSQL is 16.

Why: The native client and the role script are written for that version.

Where: The serving host, in a root shell.

How:

```text
psql --version
id
```

Expected result: The version line contains `16`. `id` shows user id 0 before `restore-accepted` is used.

Next step: Stop if the version is not 16. Do not upgrade a production cluster in this task.

### 2. Add the operating-system user

What: Create the OS user `of_restore` if it does not exist.

Why: Peer authentication needs that name. Root must not connect as the database role.

Where: The serving host.

How:

```text
id of_restore || useradd --system --no-create-home --shell /usr/sbin/nologin of_restore
```

Expected result: `id of_restore` prints a system account. It is not a member of `sudo` or `postgres`.

Next step: Stop if the account already exists and is a login administrator.

### 3. Add one peer rule

What: Allow only the OS user `of_restore` to open a local socket as the database role `of_restore`.

Why: `runuser` changes the client to that OS user. Peer then accepts it. Root still does not match.

Where: The PostgreSQL 16 `pg_hba.conf` on the serving host. Do not edit the development computer.

How: Inspect the file first. Add this line above any broader `local` rule:

```text
local   all   of_restore   peer
```

Reload PostgreSQL only after the line is saved:

```text
pg_ctlcluster 16 main reload
```

Do not use trust. Do not delete an existing rule to make this one work. Do not map root in `pg_ident.conf`. If an earlier `local` rule uses trust, stop and report it. Replacing that rule is a separate decision.

Expected result: `of_restore` can run `psql --host /var/run/postgresql --username of_restore --no-password --dbname postgres -c 'SELECT current_user;'`. The answer is `of_restore`. The same command run as root fails.

Next step: Stop if root can connect as `of_restore`.

### 3b. Let of_restore traverse the private restore directory

What: Change `/var/openfactors/restore` from mode `0700` to mode `0710` and group `of_restore`.

Why: `pg_restore` runs as `of_restore` and must open one handoff file. The parent stays non-writable and unlistable. Accepted snapshot permissions are not changed.

Where: The serving host. Do not change this directory on the development computer.

How:

```text
chgrp of_restore /var/openfactors/restore
chmod 0710 /var/openfactors/restore
stat -c '%A %U %G' /var/openfactors/restore
```

Expected result: `drwx--x--- root of_restore`. `ls` as `of_restore` fails. A known child path can be traversed.

Next step: Stop if the mode is `0777`, if the group is not `of_restore`, or if `of_restore` can create a file in that directory.

### 4. Create the database roles

What: Run `ops/sql/bootstrap_serving_roles.sql` as the PostgreSQL superuser.

Why: The serving cluster has `of_api` and does not yet have `of_migrate`, `of_publish`, or `of_restore`.

Where: The serving host, connected to the `postgres` database. The script refuses to run if `openfactors_dev`, `openfactors_test`, or `openfactors_staging` exists.

How:

```text
sudo -u postgres psql --dbname postgres --set ON_ERROR_STOP=1 --file ops/sql/bootstrap_serving_roles.sql
sudo -u postgres psql --dbname postgres --command "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolcanlogin FROM pg_roles WHERE rolname IN ('of_api', 'of_migrate', 'of_publish', 'of_restore') ORDER BY 1;"
```

Expected result:

| Role | Superuser | Create DB | Create role | Inherit | Login |
|---|---|---|---|---|---|
| of_api | false | false | false | false | true |
| of_migrate | false | false | false | false | false |
| of_publish | false | false | false | false | false |
| of_restore | false | true | false | false | true |

`of_api` does not gain a password from this script. Setting its password, if it does not already have one, is a separate approval. The script's `NOINHERIT` change does not grant `of_api` any privilege.

Next step: Stop if any flag differs.

### 5. Restore one accepted release

What: Restore the accepted snapshot into `openfactors_candidate`.

Why: This is the native serving restore. It still authenticates the signature before `CREATE DATABASE`.

Where: The repository on the serving host, as Linux root, with the virtualenv Python.

How:

```text
cd /var/openfactors/openfactors
sudo --preserve-env=OPENFACTORS_DB_BACKEND OPENFACTORS_DB_BACKEND=native /var/openfactors/venv/bin/python -m ops.publish_snapshot restore-accepted serving <release-id>
```

Replace `<release-id>` with the canonical lowercase UUID already present under `/var/openfactors/accept/accepted/`. Do not pass a dump path.

Expected result: The command prints the candidate release id, schema version, row count, and content hash. `openfactors_serving` is unchanged. A failed check drops only a candidate this command created.

Next step: Stop. Do not promote until the printed release id is the release you intend to serve.

### 6. Promote, only after the release id is confirmed

What: Rename the authenticated candidate to `openfactors_serving`.

Why: Promotion is a separate command from restore.

Where: The same host and the same `OPENFACTORS_DB_BACKEND=native` setting.

How:

```text
sudo --preserve-env=OPENFACTORS_DB_BACKEND OPENFACTORS_DB_BACKEND=native /var/openfactors/venv/bin/python -m ops.publish_snapshot promote-candidate serving --confirm-release <release-id>
```

Expected result: The printed database is `openfactors_serving`. An older serving database, if one existed, is now `openfactors_previous` and is not accepting connections. Nothing is dropped.

Next step: Point the API at `openfactors_serving` only in a later approved step. This command does not restart the API.

## Rollback

What: Put `openfactors_previous` back into service.

Why: The previous generation was kept.

How:

```text
sudo --preserve-env=OPENFACTORS_DB_BACKEND OPENFACTORS_DB_BACKEND=native /var/openfactors/venv/bin/python -m ops.publish_snapshot rollback-serving serving --confirm-release <previous-release-id>
```

Expected result: `openfactors_serving` is the previous release. The generation that was just serving is preserved as `openfactors_candidate` with the comment `openfactors-demoted`. Do not drop that candidate until a later cleanup is approved. Do not drop `openfactors_serving`.

If restore fails before promotion, run:

```text
sudo --preserve-env=OPENFACTORS_DB_BACKEND OPENFACTORS_DB_BACKEND=native /var/openfactors/venv/bin/python -m ops.publish_snapshot abort-candidate serving
```

That drops only `openfactors_candidate`, and only when it is not a demoted serving generation.

## Approval checkpoints

1. Approve creation of the OS user `of_restore`.
2. Approve the single `pg_hba.conf` peer line and the reload. Do not approve a trust rule.
3. Approve `ops/sql/bootstrap_serving_roles.sql`, including `ALTER ROLE of_api NOINHERIT`.
4. Approve one `restore-accepted serving` for a named release id.
5. Approve `promote-candidate serving` for that same release id.
6. Approve an API cutover to `openfactors_serving` only after promotion. That cutover is not part of this change.

Public Beta v0.1 stays no-go until the launch checklist blockers pass. This document does not mark them passed.
