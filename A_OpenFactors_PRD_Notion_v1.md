# OpenFactors — Product Requirements Document (PRD)

> **Status:** Draft for review  
> **Version:** 1.0  
> **Date:** 23 October 2026  
> **Product owner:** Founder / Product Lead (TBD)  
> **Engineering owner:** Lead Data Engineer (TBD)  
> **Target release:** MVP — date TBD  
> **Scope:** Philippines-first, historical daily weather + public holiday factors

## 1. Executive summary

**OpenFactors** is a public-facing data platform that converts permitted external datasets into documented, validated, analytics-ready tables. Its initial release combines daily historical weather features for selected Philippine locations with Philippine public-holiday indicators. Students, analysts, researchers, and developers can discover datasets, understand their provenance, and download CSV or Parquet files or query a versioned REST API.

**Product thesis:** Analysts should spend less time finding, cleaning, aligning, and validating external contextual data, and more time analyzing business outcomes.

**Decision requested:** Approve a narrow MVP with two sources, a defined location set, a daily-grain dataset contract, and public downloads. Do not promise all-country coverage, causal insights, or enterprise SLAs at launch.

## 2. Problem and opportunity

### Problem statement
Users seeking to study how weather, holidays, and economic conditions relate to business performance must repeatedly collect data from disparate sources, reconcile incompatible geographic and temporal grains, interpret licensing, and validate data quality. This work is duplicated across projects and is difficult for new analysts to reproduce.

### Evidence status
- **Observed hypothesis, not yet validated:** Users face meaningful preparation overhead.
- **Discovery needed:** Interview 8–12 prospective users and measure current data-preparation time, failed source attempts, and desired formats.
- **Competitive research needed:** Compare existing public data portals, data marketplaces, weather APIs, and prepared feature datasets before asserting differentiation.

## 3. Vision, goals and non-goals

### Vision
Become a trustworthy, developer-friendly source of contextual external factors for reproducible analytics and forecasting.

### MVP goals
1. Publish one clearly documented daily dataset at **location × local calendar date** grain.
2. Provide historical weather and Philippine public-holiday attributes with explicit source attribution.
3. Allow unauthenticated users to inspect metadata and download approved public datasets.
4. Provide a documented read-only API with stable schema versioning and basic usage protections.
5. Operate repeatable, tested ingestion and transformation workflows with visible freshness and quality status.

### Non-goals (MVP)
- Business revenue or transaction datasets; automatic causal conclusions.
- AI-generated forecasts, recommendations, or no-code pipeline creation.
- Global geographic coverage or real-time severe-weather alerts.
- User uploads, custom dataset creation, paid plans, or organization workspaces.
- A contractual high-availability or accuracy guarantee.

## 4. Target users and jobs to be done

| Persona | Job to be done | Primary need |
|---|---|---|
| Student / beginner analyst | Build a school project without writing multiple API connectors | Easy CSV, definitions, examples |
| Business intelligence analyst | Join external factors to daily business KPIs | Stable keys, date alignment, predictable schema |
| Data scientist | Reproduce feature engineering for experiments | Historical coverage, missingness, provenance, Parquet |
| Software developer | Add contextual features to an app | Documented API, pagination, versioning |
| Platform maintainer | Keep published data accurate and available | Alerts, retries, backfills, lineage, release controls |

## 5. User journeys

### Journey A — Discover and download
1. Visitor opens dataset catalog.
2. Filters by country, location, date coverage, and category.
3. Opens a dataset detail page showing grain, columns, units, sources, license, quality status, and last successful refresh.
4. Selects date range and location(s).
5. Downloads CSV or Parquet and receives the dataset version / generated-at timestamp.
6. Uses sample SQL or Python to join with business data.

**Success condition:** A new analyst can find and download a usable dataset without creating an account.

### Journey B — Integrate via API
1. Developer reads OpenAPI documentation and example request.
2. Calls a versioned endpoint with location and date filters.
3. Receives paginated records with stable types and metadata.
4. Handles empty results, rate limits, and source freshness using documented response semantics.

**Success condition:** A developer can retrieve a defined slice without guessing field meaning.

### Journey C — Recover an ingestion failure (internal)
1. Scheduled ingestion detects a timeout, invalid schema, or source outage.
2. Workflow retries transient failures with bounded exponential backoff.
3. Failure creates a run record and alert; previously published dataset remains available.
4. Maintainer diagnoses source change and performs a targeted backfill.
5. Validation gates pass before publication; incident is documented.

**Success condition:** Failed runs do not silently publish incomplete or duplicate data.

## 6. Prioritized user stories and acceptance criteria

| ID | Priority | User story | Acceptance criteria |
|---|---|---|---|
| US-01 | P0 | As an analyst, I want a dataset catalog so I can find relevant factors. | Catalog lists published datasets with geography, grain, coverage, and freshness. |
| US-02 | P0 | As an analyst, I want a data dictionary so I can interpret each field. | Each public field has type, unit, description, nullable status, and source. |
| US-03 | P0 | As an analyst, I want filtered CSV downloads so I can work in Excel or Python. | Valid filters return deterministic columns; invalid ranges produce actionable errors. |
| US-04 | P0 | As a data scientist, I want Parquet exports so I can process larger datasets efficiently. | Export preserves documented schema and includes dataset version metadata. |
| US-05 | P0 | As a developer, I want a read-only API so I can integrate data into applications. | Versioned endpoint supports location/date filters, pagination, examples, and error codes. |
| US-06 | P0 | As a user, I want freshness and provenance so I can judge whether data is appropriate. | Dataset page shows source, license/terms, coverage, refresh time, and known limitations. |
| US-07 | P0 | As a maintainer, I want idempotent ingestion so reruns do not duplicate records. | Re-running the same interval yields one record per defined business key. |
| US-08 | P0 | As a maintainer, I want quality gates so invalid data is not published. | Uniqueness, schema, coverage, and accepted-range checks run before promotion. |
| US-09 | P1 | As an analyst, I want example notebooks so I can reproduce joins. | At least one runnable Python and one SQL example provided. |
| US-10 | P1 | As a maintainer, I want backfills so I can correct missing periods. | Authorized backfill specifies interval, is logged, and can be rerun safely. |
| US-11 | P1 | As a user, I want change history so I can reproduce past analyses. | Breaking changes create new schema versions; revisions are documented. |
| US-12 | P2 | As a researcher, I want monthly economic indicators so I can expand analysis. | Deferred to post-MVP with native monthly grain retained. |

## 7. Functional requirements

| ID | Requirement | Priority | Verification |
|---|---|---|---|
| FR-01 | Fetch approved weather observations for configured locations and date ranges. | Must | Integration test with representative interval |
| FR-02 | Fetch Philippine holiday records and map them to local calendar dates. | Must | Holiday fixture tests |
| FR-03 | Persist raw payloads and ingestion metadata separately from curated records. | Must | Raw artifact and manifest inspection |
| FR-04 | Transform data into standardized typed models with source provenance. | Must | dbt tests and schema review |
| FR-05 | Publish location-date external factor dataset only after quality checks pass. | Must | Simulated failed-quality release |
| FR-06 | Provide catalog and per-dataset documentation pages. | Must | Acceptance walkthrough |
| FR-07 | Export filtered CSV and Parquet data. | Must | Format and schema tests |
| FR-08 | Expose versioned read-only REST API and OpenAPI documentation. | Must | Contract tests |
| FR-09 | Schedule incremental refresh and support controlled historical backfill. | Must | Orchestration test |
| FR-10 | Record job state, counts, duration, freshness, and failures. | Must | Operational dashboard review |
| FR-11 | Show example joins to user-owned business outcomes. | Should | Notebook execution |
| FR-12 | Provide user accounts and saved workspaces. | Won't (MVP) | Not applicable |

## 8. Data product contract

**Initial product:** `daily_external_factors_ph`  
**Grain:** one row per `location_id` and Philippine local `date`  
**Business key:** (`location_id`, `date`)  
**Primary timezone:** `Asia/Manila` for local-day alignment  
**Historical range:** `2018-01-01` through the latest published `Asia/Manila` date. Open-Meteo ERA5 returned that start date for every pilot city on 2026-10-24. The full backfill is still OF-012.  
**Initial locations:** `ph-manila`, `ph-baguio`, `ph-cebu-city`, `ph-tacloban`, `ph-davao-city`. Working baseline in `docs/decisions/ADR-001-pilot-locations.md` and `contracts/fixtures/pilot_locations.json`.  
**Refresh policy:** daily attempt; publish only on passing quality checks

| Column | Type | Meaning / rule |
|---|---|---|
| date | DATE | Local calendar date (Asia/Manila) |
| location_id | TEXT | Stable identifier in location dimension |
| country_code | CHAR(2) | ISO 3166-1 alpha-2; `PH` for MVP |
| temperature_mean_c | DECIMAL | Daily mean temperature in °C; nullable |
| precipitation_mm | DECIMAL | Daily precipitation in mm; nullable |
| is_public_holiday | BOOLEAN | National public holiday. Published rows are true or false only. An incomplete calendar year is omitted from the release. |
| holiday_name | TEXT | Holiday name; null when the flag is false. Several names are joined with ` \| ` in alphabetical order. |
| is_weekend | BOOLEAN | Saturday or Sunday in local calendar |
| weather_source_id | TEXT | Identifier of contributing weather source |
| holiday_source_id | TEXT | Identifier of contributing holiday source |
| source_updated_at | TIMESTAMPTZ | Latest meaningful upstream update time. Null when the source provides none. Never the retrieval time. |
| dataset_version | TEXT | Published semantic dataset version |

**Important semantic decisions:**
- Weather measurements may be modeled or reanalysis-derived; document the exact source and resolution rather than calling them station observations.
- Missing weather values remain null, not silently replaced with zero.
- Holiday classifications must document national vs local scope and changes to official declarations; verify authoritative calendars where needed.
- `source_updated_at` is never the retrieval time and never the original measurement time. For Open-Meteo ERA5 it is null.
- Weather and holidays are contextual factors, **not proof of causation** for business outcomes.
- Future monthly economic data should retain monthly grain in separate products; any daily alignment must be explicitly documented as a feature-engineering choice.

## 9. Proposed technical architecture and constraints

**Data flow:** Approved external APIs → Python connectors → immutable raw object storage → staging models → validated warehouse tables → published marts → API / CSV / Parquet / catalog.

**Candidate implementation (subject to ADR approval):** Python, Docker Compose, PostgreSQL, dbt Core, object storage (e.g., MinIO), FastAPI, React/TypeScript, GitHub Actions. Introduce Airflow, Grafana, and Terraform in staged milestones when their operational value justifies the complexity.

**Architectural requirements:**
- Each source connector has bounded retries, timeout, rate-limit handling, source identifier, and checkpoint semantics.
- Every ingestion run records `run_id`, source, interval, status, counts, error category, and timestamps.
- Raw payloads are retained according to license and retention policy, with checksums and ingestion metadata.
- Transformations are deterministic, tested, and rerunnable.
- Publication is atomic or otherwise protects users from partial datasets.
- Public API cannot directly query raw storage or unrestricted internal tables.
- Secrets are stored outside source control; public data access is read-only.

## 10. Non-functional requirements and service objectives

*Proposed MVP targets to validate through load testing and operating experience; not contractual guarantees.*

| Area | Target / rule |
|---|---|
| Data freshness | Daily refresh attempted; show age of latest successful publication |
| Data completeness | ≥ 99% expected location-date coverage for published supported periods, excluding documented upstream gaps |
| Uniqueness | 100% unique (`location_id`, `date`) in published product |
| Reliability | ≥ 99% successful scheduled pipeline runs over a rolling 30-day evaluation period |
| API latency | p95 < 1 second for a bounded, cached common query under documented test load |
| Security | HTTPS; no secrets in repository; least-privilege credentials; basic abuse controls |
| Accessibility | Semantic navigation, keyboard-usable download flow, readable documentation |
| Observability | Pipeline duration, run status, row counts, freshness, validation failures, API errors |
| Cost | Monthly budget cap approved before public deployment; usage and storage monitored |
| Recoverability | Documented replay/backfill process; test one failed-run recovery before launch |

## 11. API requirements (draft contract)

`GET /api/v1/datasets` — published dataset catalog.  
`GET /api/v1/datasets/daily-external-factors-ph` — dataset metadata and dictionary.  
`GET /api/v1/factors/daily?location_id=...&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&page=1&page_size=100` — paginated records.  
`GET /api/v1/exports/daily-external-factors-ph?format=csv&location_id=...&start_date=...&end_date=...` — approved export.

**Required behavior:** documented request limits; validation errors (400/422); missing resource (404); rate limiting (429); unavailable dependency (503); consistent JSON error envelope; versioning policy; response metadata including `dataset_version`, `generated_at`, and `data_as_of`. Exact endpoint naming and limits are subject to implementation review.

## 12. Quality, provenance, licensing and governance

**Publish-blocking tests:** expected schema, uniqueness, valid locations, valid date ranges, no impossible negative precipitation, supported units, successful source mapping, and coverage threshold. Additional anomaly checks can warn without blocking until thresholds are calibrated.

**Dataset metadata must include:** source URL and provider, retrieval date, original source grain, transformation notes, license and redistribution decision, known gaps, revision policy, owner, version, and last validation result.

**License gate:** Public API access does not itself grant redistribution rights. Before publishing any source-derived records, review the provider's terms, underlying data license, attribution requirements, commercial restrictions, and caching/export permissions. If rights are unclear, do not redistribute those records until resolved.

## 13. Dependencies, assumptions and risks

| Risk | Impact | Mitigation | Owner |
|---|---|---|---|
| Upstream API changes or downtime | Stale or missing data | Contract tests, retries, cached last-good release | Data engineering |
| Redistribution restrictions | Legal/compliance exposure | Source-by-source license review and release gate | Product / legal review |
| Holiday calendar inaccuracies | Misleading features | Verify definitions and authoritative changes | Data quality |
| Temporal or geographic misalignment | Incorrect analytical joins | Explicit location/date grain and timezone | Data modeling |
| Cloud costs exceed budget | Unsustainable free service | Export limits, caching, quotas, cost alerts | Platform engineering |
| Scope creep from too many datasets | Delayed MVP | Freeze weather + holidays scope | Product owner |
| Low user adoption | Limited validation of usefulness | Early interviews and public beta feedback | Product owner |

## 14. Success metrics and instrumentation

**North-star candidate:** number of distinct users or projects completing a successful dataset consumption action (download or valid API retrieval) per month. Avoid interpreting raw requests as meaningful adoption.

| Metric | Definition | MVP measurement |
|---|---|---|
| Dataset consumption | Completed downloads + successful API retrievals (deduplicated where feasible) | Event logs with privacy safeguards |
| Activation | % of catalog visitors completing first valid download | Anonymous analytics, consent as required |
| Data freshness | Time since last validated publication | Publication metadata |
| Data quality pass rate | Passing scheduled validation runs / total validation runs | Quality-run history |
| Pipeline success rate | Successful scheduled runs / scheduled runs | Orchestrator metrics |
| Time to first dataset | Median time from catalog visit to completed download | Product analytics |
| User usefulness | % of interviewed pilot users able to complete an analysis | Structured usability sessions |

**Initial success thresholds:** Set baselines during private beta, then approve quantitative targets. Do not invent adoption forecasts before user discovery.

## 15. Release plan and milestones

| Phase | Outcome | Exit criteria |
|---|---|---|
| Discovery | Validated use case and sources | User interviews, competitor scan, licensing review, pilot locations approved |
| Foundation | Repeatable ingestion and raw storage | Tests pass; re-ingestion is idempotent; run metadata retained |
| Data product | Trusted warehouse and mart | Data contract signed off; publish-blocking checks enforced |
| Access | API, downloads, catalog | Contract tests, documentation, bounded load test pass |
| Operational readiness | Deployable public beta | CI/CD, alerts, backfill drill, runbooks, cost cap |
| Public beta | Real-world validation | Usage metrics, issue intake, first feedback cycle |

## 16. Definition of done — MVP

- [ ] Product owner approves the dataset scope, geography, and target users.
- [ ] Source redistribution and attribution rules are documented and approved.
- [ ] Historical weather and holiday ingestion works for approved intervals.
- [ ] Raw ingestion metadata and source provenance are recoverable.
- [ ] Published mart satisfies its grain, schema, and quality contract.
- [ ] Re-running ingestion does not create duplicate published records.
- [ ] Failed validation prevents publication of invalid data.
- [ ] Catalog displays source, coverage, dictionary, freshness, and limitations.
- [ ] CSV and Parquet exports are usable in Python and spreadsheet tools.
- [ ] Versioned read-only API passes contract and security tests.
- [ ] CI/CD and monitoring are operating in the target environment.
- [ ] One simulated source outage and one historical backfill are documented.
- [ ] At least three target users complete a moderated dataset-consumption test.
- [ ] Public README, support/issue channel, and changelog are published.

## 17. Open questions and decision log

| ID | Decision needed | Recommended starting position | Status |
|---|---|---|---|
| D-01 | Which pilot locations? | Five cities: Manila, Baguio, Cebu City, Tacloban, Davao City | Working baseline 2026-10-24. ADR-001. Product owner confirmed the five cities on 2026-10-25. |
| D-02 | How many historical years? | Start `2018-01-01`, confirmed against Open-Meteo ERA5 | Working baseline 2026-10-24. Raw responses through 2026-10-25 stored on 2026-10-25. Six trailing days are null. |
| D-03 | What holiday authority? | Official Gazette proclamations; national regular and special non-working days | Working baseline 2026-10-24. Source registry. Years 2018 through 2026 transcribed on 2026-10-25. |
| D-04 | Anonymous vs API-key access? | Anonymous read-only, no accounts | Working baseline 2026-10-24. ADR-003. Rate limits still unset. |
| D-05 | Public hosting budget? | USD 0 per month, local Docker only | Working baseline 2026-10-24. ADR-003. |
| D-06 | Which raw data can be redistributed? | Open-Meteo ERA5 under CC BY 4.0 for a non-commercial product; transcribed Official Gazette holidays | Working baseline 2026-10-24. Source registry. |
| D-07 | Which orchestrator at launch? | Local scheduled or manual command. No Airflow. | Working baseline 2026-10-24. ADR-003. |
| D-08 | How are revisions represented? | New `release_id` for content changes. Keep every published release while the cap is USD 0. Raw files stay until the source is withdrawn. | Working baseline 2026-10-24. ADR-004. |

## 18. Related engineering artifacts

This PRD describes **what** the product must achieve and **why**. Maintain separate linked documents for detailed implementation:

- System Architecture / High-Level Design (HLD)
- Architecture Decision Records (ADRs)
- Data Source Registry and License Review
- Data Contracts and Data Dictionary
- API OpenAPI Specification
- Test Strategy and Quality Rules
- Infrastructure and Deployment Runbook
- Incident Response and Backfill Runbook
- Product Roadmap and GitHub Epic / Issue Backlog

---

**Change history**

| Version | Date | Change | Author |
|---|---|---|---|
| 1.0 | 2026-10-23 | Initial Notion-ready PRD based on OpenFactors MVP concept | Draft |
| 1.1 | 2026-10-24 | D-01 working baseline: five pilot locations and a 2018-01-01 start | Repository baseline |
| 1.2 | 2026-10-24 | D-02 through D-07 working baseline: Open-Meteo ERA5, Official Gazette holidays, USD 0 local budget | Repository baseline |
| 1.3 | 2026-10-24 | D-08 working baseline: keep raw files and published releases on this computer; withdraw in one local action | Repository baseline |
| 1.4 | 2026-10-24 | OF-005: align source timestamp, holiday publication, retention column, and example location ids | Repository baseline |
