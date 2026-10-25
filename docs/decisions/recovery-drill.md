# Recovery drill

These procedures run on this computer. They are not HTTP routes. There is no offsite copy. Hosted recovery-time targets are unset. A measured duration from one local drill is not an approved target.

## Database restore

1. `python -m ops.local_stack backup dev` writes `ops/local/runtime/dev/backups/dev.dump`.
2. `python -m ops.local_stack restore-check dev ops/local/runtime/dev/backups/dev.dump` loads that file into the database `of_restore_drill`.
3. The check reads the fact count, the `open-meteo-era5` status, and the API role. The API role can use `publish` and cannot use `control`.
4. The command then removes `of_restore_drill`. It does not replace `openfactors_dev`.

The printed `measured_restore_seconds` is how long that check took. `rpo_target_seconds` and `rto_target_seconds` are null.

## Raw object restore

Copy the object file aside before it is lost. Put it back with `restore_raw_object` only when the SHA-256 of those bytes equals the digest on the manifest. If the key already holds different bytes, the stored file is left unchanged. The manifest row is not edited.

## Release rollback

Use `rollback_release` for a release whose status is `published`. The active pointer moves to that release. The other published rows stay stored. Do not update the pointer with an ad-hoc statement. A release that is not `published` is refused, so a withdrawn release is not reactivated.

## Failed run

A run left `running` after a crash resumes with `recover_running_run` and the same run id. Days already at the checkpoint are not stored again. A run whose status is `failed` stays `failed`. Recovery of that interval is a new attempt. The failed record keeps its run id for the operator. No email or SMS is sent.
