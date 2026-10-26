# S0 — what is recorded, and what is still open

Locations, sources, holiday rules, the USD 0 cap, anonymous access, the local scheduler, retention, and the repository skeleton are recorded. The local connectors, dbt models, API, and catalog are in this repository. The hosted limits and the release decision stay open.

| Step | Decision | Status | Where |
|---|---|---|---|
| 1 | D-01 pilot cities | Product owner confirmed the five cities on 2026-10-25. | `docs/decisions/ADR-001-pilot-locations.md` |
| 2 | D-06 / OF-002 sources | Recorded. Non-commercial while the free Open-Meteo API is used. | `docs/decisions/source-registry.md` |
| 3 | D-03 holiday meaning | Years 2018 through 2026 are transcribed. Incomplete years stay out of the release. | Source registry and `contracts/fixtures/holiday_calendar.json` |
| 4 | D-05 / OF-003 budget | Recorded. USD 0, local Docker, Founder / Product Lead operates it. | `docs/decisions/ADR-003-budget-and-ownership.md` |
| 5 | D-04 and D-07 | Recorded. Anonymous read-only. The production scheduler is the local command. Learning Airflow is ADR-007 and is not that scheduler. | ADR-003 |
| 6 | D-08 / OF-004 retention | Recorded. Keep raw files and published releases on this computer. Withdraw in one local action. | `docs/decisions/ADR-004-retention-and-withdrawal.md` |
| 7 | D-02 history start | Raw ERA5 responses stored for 2018-01-01 through 2026-10-25. Six trailing days are null. | `ingestion/raw/open-meteo-era5/` |
| 8 | OF-005 contract mismatches | Recorded in ADR-005. Date span and rate numbers remain unset. | `docs/decisions/ADR-005-contract-reconciliation.md` |
| 9 | OF-006 repository | Recorded. Pinned ruff and pytest, CI job `ci`, secret scan, code owners file. | `README.md` and `.github/workflows/ci.yml` |
| 10 | ADR-006 public hosting | Proposed on 2026-10-26. Not approved. No host is selected. | `docs/decisions/ADR-006-zero-cost-public-hosting.md` |
| 11 | Public beta query limits | Reconsidered on 2026-10-26. The 366-day span is not enforced. A non-loopback client is limited to 60 requests per 60 seconds. That limit is provisional until staging measurements. | `docs/decisions/ADR-008-public-beta-query-limits.md` |

## Next — backlog recorded

The stored holiday calendar and ERA5 files can be loaded with `python -m ops.load_stored`. That rebuilds the local candidate view and promotes the approved release on this computer. `python -m openfactors.api.catalog` serves that release at http://127.0.0.1:8000. `python -m examples.join_factors` joins the two sample Manila orders to that release. ADR-006 compares two zero-cost host configurations and does not approve one. ADR-007 adds a local learning Airflow project that runs the production daily command and does not replace it. ADR-008 reconsiders the 366-day span and does not enforce it. One stored city is 3,221 dates, which fits the existing page size and export caps. A non-loopback client is limited to 60 requests per 60 seconds. That limit is provisional until staging measurements. Loopback stays unlimited. The catalog reads the published release as of_api through the psql client. Local TLS is sslmode=require. A configured host uses verify-full and a trusted CA. The default bind stays 127.0.0.1:8000. The hosted limits and the release decision stay open.
