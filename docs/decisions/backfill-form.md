# Operator backfill form

The form is a local command: `python -m openfactors.orchestration.backfill_ui`. `--serve` listens on `127.0.0.1` only. The page is not part of the public API, and it is not an OpenAPI path.

The fields are the same as the backfill command: source, location, inclusive dates, a named operator, and a reason. Dry-run is checked. Leaving it checked does not publish. The operator name `anonymous` is refused. The public catalog returns 404 for a backfill path.
