# ADR-004 — Retention, withdrawal, and revisions

| Field | Value |
|---|---|
| Decision | D-08 |
| Ticket | OF-004 |
| Status | Working baseline, adopted 2026-10-24 |
| Applies while | Monthly cost cap is USD 0 and the only environment is local Docker |
| Machine copy | `contracts/fixtures/retention_policy.json` |
| Public cache | None. A CDN or hosted copy needs this ADR revised before it is turned on. |

## Raw files

Keep each Open-Meteo response and each proclamation file for as long as that source stays approved. Do not delete them on a timer. Deleting an approved source's raw files is a new decision.

`control.raw_manifests.retention_until` stays null. Null means no scheduled deletion. OF-014 writes the payload and its manifest only after the stored bytes match the SHA-256 digest, and that manifest carries a null `retention_until`.

The source-registry table uses `retention_mode`. `until_source_withdrawn` requires `raw_retention_days` to be null. `days` requires a positive `raw_retention_days`. Do not store a placeholder such as 36500 days.

## Successful releases

Every release that reached `published` stays queryable by its `release_id` while the cost cap is USD 0. Nothing expires for age. `release_id` is never reused.

A new `release_id` is created when published values change. `dataset_version` changes only when a column's type, order, nullability, or meaning changes. Those are different identifiers. A schema break is a new major `dataset_version` and is outside this baseline.

## Failed candidates

A candidate that fails a quality gate becomes `rejected`. The active pointer does not move. Rejected rows are not served. The previous published release remains the one readers see.

## Withdrawal

One operator action is finished only when all of these are true:

1. The release status is `withdrawn`, or the source `redistribution_status` is `suspended`.
2. If the active pointer cited that release, it now cites the newest remaining `published` release, or it is empty when none remains.
3. Local CSV and Parquet files for that `release_id` are deleted.
4. A request for that `release_id`, and a request that would have resolved a suspended source, returns 404.

There is no public cache at this cap, so there is no second purge window. License revocation withdraws every published release that contains the suspended source, including releases that previously passed quality. A corrected proclamation does not edit the old release. It is published as a new `release_id`, and the old one remains readable unless the correction is a withdrawal.

Suspending `open-meteo-era5` or `ph-official-gazette` withdraws the daily dataset releases that include that source. The product does not keep serving a half-filled table.

## The only copy

Raw files and releases exist on this computer. There is no offsite backup inside the USD 0 cap. Losing the disk loses the copy. Rebuilding means ingesting again. Recovery-time targets for a hosted restore stay open.

## Tabletop

OF-025 executes the source-suspension case in the local test suite: the source is suspended, published releases that include it are withdrawn, their local export files are deleted, and those release ids resolve as 404. There is still no public cache. OF-040 executes the local recovery drill: an isolated database restore, a raw-object restore checked by SHA-256, a release rollback, and recovery of a run that is still running. A failed run stays failed. Hosted recovery-time targets remain unset. This page is not a signed hosted-restore record.

| Case | Expected result |
|---|---|
| Quality gate fails | Candidate `rejected`. Active pointer unchanged. Last published release still readable. |
| Free Open-Meteo use must stop | Source `suspended`. Every release that includes it is `withdrawn`. Active pointer empty or moved to a release that does not include it. Local exports for withdrawn ids deleted. Those ids return 404. |
| Proclamation date corrected | New `release_id` with the corrected holiday. The previous release id still answers unless this ADR is changed to withdraw it. |
| Disk lost | No second copy. Restore is a new ingestion, not a backup return. |

## Acceptance

| Role | Result |
|---|---|
| Founder / Product Lead | Adopted by instruction on 2026-10-24 |
| Executed withdrawal drill | Not run. Belongs to OF-025. |
