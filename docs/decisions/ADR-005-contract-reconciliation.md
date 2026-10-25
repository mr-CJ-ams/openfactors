# ADR-005 — Contract reconciliation

| Field | Value |
|---|---|
| Decision | OF-005 |
| Status | Working baseline, 2026-10-24 |
| Checked | PRD, HLD, LLD, API specification, `D1_OpenFactors_OpenAPI_v1.yaml`, source registry, retention policy |
| Checker | `contracts/check_contract_alignment.py` |

PRD controls scope. LLD controls column meaning. The API specification and OpenAPI control HTTP behavior. Where those disagreed, this record is the correction. Each affected document was updated.

## Resolved

| Topic | Agreed rule |
|---|---|
| `source_updated_at` | Nullable. It is the latest meaningful upstream update time. It is never retrieval time and never the measurement time. Open-Meteo ERA5, as called for this MVP, does not send one, so the published value is null. `retrieved_at` stays internal. |
| Revision winner | `source_updated_at` when it is present, otherwise `retrieved_at`, then `artifact_id`. |
| `is_public_holiday` | Not null on a published row. A year whose calendar is incomplete is left out of the release. It is not stored as false, and it is not a null flag. |
| `holiday_name` | Null when the published flag is false. Several names on one date are joined with ` \| ` in alphabetical order. |
| Examples | Approved location id is `ph-manila`. Approved source ids are `open-meteo-era5` and `ph-official-gazette`. The old `PH-PILOT-001` placeholder is retired. |
| Retention column | `retention_mode = until_source_withdrawn` with `raw_retention_days` null. A day count is required only when `retention_mode = days`. |
| One location per request | Factor and export calls take one `location_id`. Unknown id is 404. |
| Decimal scales | Temperature `NUMERIC(7,3)`. Precipitation `NUMERIC(10,3)`. JSON numbers stay finite. Parquet and the warehouse keep the scale. |
| Errors | 400 malformed syntax, 404 unknown or withdrawn, 405 method not allowed, 422 invalid filters or unknown query parameters or export cap, 429 with `Retry-After`, 500 with a trace id, 503 dependency down. Empty valid results are 200. |
| Export caps | 100,000 rows and 50 MB, provisional and already the same in the HLD, LLD, API spec, and backlog. |

## Left unset on purpose

No date-span maximum and no rate-limit numbers. Inventing them here would pretend a benchmark happened. They stay open until a later limits ADR. Public launch still waits on them.

## Acceptance

| Role | Result |
|---|---|
| Founder / Product Lead | Adopted by instruction on 2026-10-24 |
| Automated check | `python contracts/check_contract_alignment.py` |
