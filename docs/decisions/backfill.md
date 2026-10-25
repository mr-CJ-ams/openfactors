# Historical backfill

The historical backfill is one local command: `python -m openfactors.orchestration.backfill`. It is not an HTTP route. Airflow is not installed. The daily command still refuses a date interval.

The command requires a source id (`open-meteo-era5` or `ph-official-gazette`), one approved pilot location or `all`, an inclusive start and end date, a named operator, and a reason. `anonymous` is rejected. A location id outside the approved pilot list is rejected. The start cannot be before `2018-01-01`. The end cannot be after the current Asia/Manila date. No maximum number of days is set, and no request quota is set.

`--dry-run` checks that scope, writes an audit record with the operator, the reason, and the run id, and does not run the pipeline. An actual run uses the same order as the daily attempt: weather, holidays, transform, validation, then publish. A failed step does not publish. Transient failures use the connector attempt limit of 4. The audit and the failed run record stay in this process, with the daily run log. They are not a Postgres history.

Repeating the same source, location, interval, and code revision does not run the steps again. A new code revision can publish again. This command does not update or delete a published release. The previous release stays stored. Starting the command without `--dry-run` does not fetch weather and does not publish. It records a failed attempt.
