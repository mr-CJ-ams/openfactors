# OpenFactors — API Specification (Contract-First Design)

> **Status:** Draft v1.0 | **Owner:** Data Platform Engineering | **Date:** 2026-10-23  
> **Authoritative inputs:** PRD v1.0, HLD v1.0, Data Models & Data Contracts LLD v1.0  
> **Machine-readable companion:** `OpenFactors_OpenAPI_v1.yaml` (OpenAPI 3.1.0)  
> **Scope:** Philippines pilot, daily weather + national holidays. No production service exists yet.

## 1. Purpose and document precedence

Define a testable public, read-only API and export contract for `daily_external_factors_ph`. The PRD controls *what* the product must do; HLD controls system boundaries and release publication; LLD controls the canonical 12-field data contract. This specification controls HTTP representation, query behavior and error semantics. Any discrepancy is a **change request**, not permission to silently reinterpret an upstream document.

**Compatibility decisions:** Retain all four PRD/HLD/LLD endpoint paths. Adopt the LLD-required optional `release_id` filter to make pagination reproducible. Preserve exactly 12 public row fields, in PRD order. Internal IDs, run metadata, quality flags and raw artifacts remain private. FR-12 accounts/workspaces, US-12 economic data, and arbitrary uploads are excluded.

## 2. Public API boundary and security

- Base path `/api/v1`. OpenAPI served at `/openapi.json`; interactive docs may be provided at `/docs` with production security review.
- Anonymous HTTPS GET only for published data. No public mutation, login, ingestion, backfill, admin, or direct database access endpoints.
- API DB role has SELECT only on approved published projections; never raw, staging, control or object-storage credentials.
- Requests use allowlisted parameters; SQL must be parameterized. Restrict CORS to configured origins where appropriate; CORS is not an authentication mechanism.
- Enforce edge/application rate limits; `429` with `Retry-After`. Trust forwarded client IP headers only from configured proxies. Do not expose private internal traces in errors.
- License approval, source withdrawal and takedown gates override public availability, including old release URLs and cached files.

## 3. Canonical public data contract

**Dataset ID:** `daily_external_factors_ph`  
**URL slug:** `daily-external-factors-ph`  
**Grain:** one row per (`location_id`, `date`) within a release  
**Date:** Philippine local calendar date, `Asia/Manila`  
**Timestamps:** RFC 3339 UTC offsets (`Z` recommended)  
**Order:** `date ASC, location_id ASC` (deterministic)

| Position | Field | JSON type | Null allowed | Semantic contract |
|---|---|---|---|---|
| 1 | `date` | string, date | No | Local date `YYYY-MM-DD` |
| 2 | `location_id` | string | No | Approved stable location identifier |
| 3 | `country_code` | string | No | `PH` in MVP |
| 4 | `temperature_mean_c` | number | Yes | °C; warehouse `NUMERIC(7,3)`; do not replace missing with zero |
| 5 | `precipitation_mm` | number | Yes | Millimeters; warehouse `NUMERIC(10,3)`; zero is measured zero, not missing |
| 6 | `is_public_holiday` | boolean | No | True or false only on published rows. Incomplete calendar years are omitted from the release. |
| 7 | `holiday_name` | string | Yes | Null when the flag is false. Several names are joined with ` \| ` in alphabetical order. |
| 8 | `is_weekend` | boolean | No | Local Saturday/Sunday |
| 9 | `weather_source_id` | string | No | Approved provider reference |
| 10 | `holiday_source_id` | string | No | Approved provider reference |
| 11 | `source_updated_at` | string, date-time | Yes | Latest meaningful upstream update timestamp. Null for Open-Meteo ERA5, which does not send one. Never the retrieval time. |
| 12 | `dataset_version` | string | No | Schema/semantic version, not content release ID |

**Numeric precision:** JSON numbers do not guarantee exact decimal representation in every client; authoritative Parquet decimal scale and warehouse DECIMAL definitions remain governed by LLD. The API must serialize finite numbers only (no NaN/Infinity). Preserve decimal semantics in exports.

## 4. Endpoint contract

| Method / path | Purpose | PRD | Key behaviors |
|---|---|---|---|
| `GET /api/v1/datasets` | Public dataset catalog | FR-06, US-01, US-06 | Only approved, accessible active datasets; coverage, freshness, licensing |
| `GET /api/v1/datasets/daily-external-factors-ph` | Metadata + dictionary | FR-06, US-02, US-06 | 12 columns, sources, units, nullability, provenance, limitations; optional release pin |
| `GET /api/v1/factors/daily` | Filtered records | FR-08, US-05 | Required location and inclusive date interval; optional release pin; pagination |
| `GET /api/v1/exports/daily-external-factors-ph` | CSV/Parquet | FR-07, US-03/04 | Same filters and release pin; bounded synchronous export |
| `GET /openapi.json` | Contract discovery | FR-08 | Machine-readable OpenAPI 3.1 |

### 4.1 Query parameters

- `location_id`: required for factors/exports; exactly one approved pilot location per request; unknown -> `404`.
- `start_date`, `end_date`: required, ISO dates, **inclusive**, `start_date <= end_date`. Invalid or unsupported interval -> `422`.
- `release_id`: optional UUID. If absent, resolve active release **once** and pin for the entire request. If provided, return only accessible, retained, approved release; withdrawn/unknown -> `404` to avoid information disclosure.
- `page`: factors only; default `1`, minimum `1`.
- `page_size`: factors only; default `100`, range `1..1000`.
- `format`: exports only, required, `csv` or `parquet`.
- Export hard limits (provisional): 100,000 rows **and** 50 MB. Return `422` before streaming if limits cannot be met; require splitting date intervals. Define implementation-specific max date span by ADR and publish it in docs; do not invent a value.
- Unknown query parameters: reject with `422` to catch typos. Enforce query-size and request-time budgets at gateway.

### 4.2 Successful factor response

```json
{
  "dataset_id": "daily_external_factors_ph",
  "dataset_version": "1.0.0",
  "release_id": "00000000-0000-0000-0000-000000000001",
  "generated_at": "2026-10-23T12:00:00Z",
  "data_as_of": "2026-10-23T11:00:00Z",
  "pagination": {"page": 1, "page_size": 100, "total": 1},
  "data": [{
    "date": "2026-10-20",
    "location_id": "ph-manila",
    "country_code": "PH",
    "temperature_mean_c": 26.1,
    "precipitation_mm": 3.4,
    "is_public_holiday": false,
    "holiday_name": null,
    "is_weekend": false,
    "weather_source_id": "open-meteo-era5",
    "holiday_source_id": "ph-official-gazette",
    "source_updated_at": null,
    "dataset_version": "1.0.0"
  }]
}
```

*Identifiers are the approved pilot and source ids. Measurements in the example are illustrative. `is_public_holiday: false` is valid only when that calendar year is complete.* A valid query with no matching rows returns `200` with `data: []`, `total: 0`; requesting a page beyond the end returns `200` with empty data, not a fabricated error. `data_as_of` denotes release-level data freshness/publication reference, not the weather observation date or HTTP response time.

### 4.3 Catalog and dictionary

Catalog response contains `generated_at` and an array of published dataset summaries. Each summary includes `dataset_id`, `active_release_id`, `dataset_version`, `grain`, coverage and license summary. Never list pending, failed, withdrawn or unpublished datasets.

Dictionary includes release metadata; exact grain; timezone; geographic and temporal coverage; all 12 columns with name, logical/physical types, nullable status, unit, description, source, transformation, examples; approved attribution and redistribution terms; known gaps; quality summary; freshness; revision and deprecation policies. Source ingestion timestamps and release publication timestamps must be labeled distinctly. The frontend must not derive freshness from a failed ingestion attempt.

### 4.4 CSV and Parquet

- CSV: UTF-8, stable 12-column header order, ISO date/timestamp, dot decimal, empty cell for null, safe quoting. Prevent spreadsheet formula injection in externally sourced strings while documenting the reversible representation tradeoff.
- Parquet: exactly the same 12 logical fields; DATE (`date32`), booleans, strings, appropriate DECIMAL logical types, UTC timestamp. Preserve dataset/release metadata in Parquet key-value metadata.
- Headers: `X-Dataset-Version`, `X-Release-Id`, `X-Data-As-Of`, `Content-Disposition`, `ETag`; correct media type (`text/csv` or `application/vnd.apache.parquet`). Exported rows must match a factors query with identical filters and pinned release.
- Source attribution and license links must be available in dataset metadata and downloadable manifest/sidecar where redistribution terms require it; a raw file without sufficient attribution must not be published.
- File names include dataset slug, release ID, date interval and format; sanitize every component. Never expose internal object-storage keys.

## 5. Consistency, publication and caching

1. Read `active_dataset_release` once at request start, unless a specific approved `release_id` is supplied.
2. Every query and export reads **only** the pinned immutable release; a request never combines old/new rows.
3. Return `release_id` in JSON and headers; clients must pass it on subsequent pages for stable pagination.
4. Release promotion is atomic only after candidate data passes quality, provenance and license gates. Failed candidates leave active release unchanged.
5. Cache immutable release responses keyed by `release_id`, filters and representation; active-alias responses require short TTL or explicit invalidation.
6. Takedown/revocation must prevent access to withdrawn releases at origin and CDN. An old ETag or URL is not an authorization bypass.
7. Historical release retention and withdrawal exceptions must be documented before promising permanent reproducibility.

## 6. Error taxonomy

| Status | Condition | Behavior |
|---|---|---|
| `200` | Valid request, including zero results | Stable schema and metadata |
| `400` | Malformed HTTP/query encoding | Error envelope |
| `404` | Unknown dataset/location or unavailable release | No sensitive existence disclosure |
| `405` | Unsupported HTTP method | `Allow` header where supported |
| `422` | Invalid dates, missing filters, bad pagination, unsupported format, cap exceeded | Field-level actionable error |
| `429` | Throttled | `Retry-After` |
| `500` | Unexpected failure | Safe generic message + trace ID |
| `503` | Temporary dependency failure | Retryable, never partial successful dataset |

```json
{
  "error": {
    "code": "INVALID_DATE_RANGE",
    "message": "start_date must be on or before end_date",
    "trace_id": "example-trace-001",
    "details": {"field": "start_date"}
  }
}
```

Error codes are stable machine-readable identifiers; error messages are human-readable and may evolve. Do not expose SQL, raw API payloads, credentials or stack traces. Gateway-generated failures must be normalized where possible. Document 413/414/502/504 if the chosen gateway can return them; tests should not falsely assume every failure originates in FastAPI.

## 7. Performance, reliability and accessibility

- p95 under 1 second for bounded cached common factor query under **documented load**, measured separately from exports; PRD target, not a guarantee.
- Daily refresh attempted, while users see last successful publication and age. Pipeline success target >=99% over rolling 30 days; API uptime SLA is not asserted.
- Use appropriate indexes on (`release_id`, `location_id`, `date`), bounded page sizes, database statement timeouts, streaming where safe, connection pooling, and quotas.
- Avoid full-table COUNT on unbounded queries; count from pinned indexed slices or documented metadata strategy.
- Exports are size/time bounded; do not buffer arbitrary data in application memory.
- Public docs/download controls must be keyboard-accessible; provide copyable curl/Python/SQL examples when P1 documentation is released.

## 8. Security, privacy and operations

- All public traffic over HTTPS with TLS termination; no keys in repo, logs or URLs. Internal service credentials least privilege and rotated.
- No public PII is intended. IP/log telemetry is operational personal data; minimize and set retention by approved policy.
- Maintain request counts, p95 latency, 4xx/5xx rates, cache hit rate, export sizes, release IDs, freshness and takedown audit events.
- Protect against abusive anonymous downloads with request, bandwidth and concurrency limits. Set resource budget and alerting before public deployment.
- Run contract tests against a seeded local release; no test may depend on real-time third-party API availability.
- Backfill and publisher controls are internal-only and separately authorized, logged, and tested.

## 9. Requirements traceability

| Source requirement | API design response | Verification |
|---|---|---|
| FR-01 / FR-02 | Weather + holidays appear in validated public rows | End-to-end seeded fixtures; provider ingestion tests live in pipeline suite |
| FR-03 | Raw payloads inaccessible from public API | DB role/route negative tests |
| FR-04 | Stable 12-field typed projection | OpenAPI schema, dbt contract, serialization tests |
| FR-05 | Only approved release served | Fail promotion + active pointer consistency test |
| FR-06 / US-01/02/06 | Catalog, dictionary, freshness, provenance | Catalog/dictionary schema and UX tests |
| FR-07 / US-03/04 | CSV and Parquet exports | Header/type/row equivalence tests |
| FR-08 / US-05 | Versioned GET endpoints, pagination, error envelope | OpenAPI contract tests + release pin tests |
| FR-09 / US-07/10 | Internal schedule/backfill; no public mutation | Pipeline idempotency/backfill tests, negative HTTP tests |
| FR-10 | API exposes successful publication metadata; internal metrics separate | Freshness/monitoring tests |
| FR-11 / US-09 (P1) | Sample joins/docs later | Runnable notebook checks when prioritized |
| FR-12 (Won't) | No account or workspace endpoints | Route inventory check |
| US-08 | Invalid candidate not served | Quality-gate failure integration test |
| US-11 (P1) | Release pin and schema-version policy | Revision and retention tests |
| US-12 (P2) | No economic endpoint in MVP | Scope and route audit |

## 10. Contract verification matrix (release-blocking unless marked P1)

| ID | Scenario | Expected result |
|---|---|---|
| API-01 | OpenAPI document parses and validates | PASS |
| API-02 | Unknown/missing/extra query fields | 422 and consistent envelope |
| API-03 | Invalid date, inverted interval, unsupported location | 422 or 404 per contract |
| API-04 | Page 1 + page 2 with same pinned release | No missing/duplicate rows |
| API-05 | Active release changes between page requests | Pinned release stays stable |
| API-06 | Quality-gate failure during publish | Old release remains active |
| API-07 | CSV column order and null encoding | Exactly 12 PRD fields |
| API-08 | Parquet schema/metadata | Exactly 12 compatible fields + release metadata |
| API-09 | Export rows vs paginated JSON | Equivalent typed values |
| API-10 | Anonymous POST/PATCH/DELETE | No public mutation |
| API-11 | Public role probes raw/staging/control | Access denied |
| API-12 | Rate limit reached | 429 with Retry-After |
| API-13 | Upstream unavailable but active release healthy | Continue serving last approved release |
| API-14 | Serving DB unavailable | 503, no partial success |
| API-15 | Release withdrawn after cached access | Revoked at origin/CDN |
| API-16 | CSV holiday name begins `=` or `+` | Safe spreadsheet rendering |
| API-17 | License/attribution missing | Candidate blocked from publication |
| API-18 | Metadata claims new freshness after failed ingestion | Test must fail; display unchanged |
| API-19 | No matching rows | 200 + empty data, total 0 |
| API-20 | Excessive export | 422, no partial downloadable artifact |

## 11. Open decisions and architectural risks

| Decision | Current position | Must resolve before |
|---|---|---|
| ADR-API-01 | Max permitted date span not yet approved. ADR-008 reconsiders a 366-day span and does not enforce it. | Public launch |
| ADR-API-02 | Export 100k rows / 50MB provisional; benchmark | Public launch |
| ADR-API-03 | IP rate limit and bandwidth quotas TBD. ADR-008 proposes 60 requests per minute per client IP, not a bandwidth quota, and does not enforce it. | Public launch |
| ADR-API-04 | Pilot locations are the five ids in ADR-001 | Seeded release uses those ids |
| ADR-API-05 | Sources approved for a non-commercial MVP in the source registry | Any public release still needs the registry to stay current |
| ADR-API-06 | Retention and withdrawal are ADR-004 | Reproducibility on this computer only |
| ADR-API-07 | `source_updated_at` is nullable. Null is approved for Open-Meteo ERA5. Scales are `NUMERIC(7,3)` and `NUMERIC(10,3)`. | Resolved in ADR-005 |
| ADR-API-08 | Exact source freshness and quality-summary response fields require approved dictionary fixture | Contract freeze |
| ADR-API-09 | Release-specific metadata retrieval on historical schema versions; v1 must reject incompatible major versions | Multi-version release support |
| ADR-API-10 | ETag generation, CDN invalidation and cache key design | Production deployment |

## 12. Implementation order and definition of done

1. Freeze approved 12-field fixture and source attribution/license decision.
2. Implement read-only published view and DB role; seed a synthetic approved release.
3. Generate FastAPI/Pydantic response models from the OpenAPI contract or enforce equivalent tests.
4. Implement catalog and dictionary endpoints, then factors with pinned release and deterministic pagination.
5. Implement CSV and Parquet exports with bounded size, metadata and CSV safety.
6. Add edge rate limiting, caching, structured logging and failure normalization.
7. Run API-01 through API-20, performance benchmark, access-control audit and license review.
8. Record signoff in ADRs; do not declare production-ready while launch gates remain open.

**Definition of Done:** PRD functional requirements within MVP scope are traceable; OpenAPI contract matches actual responses; every public dataset is licensed and quality-approved; read-only permissions are verified; release pinning survives promotion; exports match the 12-field LLD; operational dashboards and runbooks exist; outstanding launch-blocking decisions are resolved.

## Appendix — Change control

Any change to a field's meaning, type, unit, grain, holiday interpretation, or local-date semantics is a **breaking semantic change**, even if the JSON type is unchanged. Version appropriately and update PRD, HLD, LLD, OpenAPI, migrations, sample files, docs and tests together. Release content revisions that preserve the schema get a new `release_id` without falsely implying a schema change.
