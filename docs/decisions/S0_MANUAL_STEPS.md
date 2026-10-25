# S0 — what is recorded, and what is still open

Locations, sources, holiday rules, the USD 0 cap, anonymous access, the local scheduler, retention, and the repository skeleton are recorded. Connectors, dbt, the API, and the catalog still wait.

| Step | Decision | Status | Where |
|---|---|---|---|
| 1 | D-01 pilot cities | Product owner confirmed the five cities on 2026-10-25. | `docs/decisions/ADR-001-pilot-locations.md` |
| 2 | D-06 / OF-002 sources | Recorded. Non-commercial while the free Open-Meteo API is used. | `docs/decisions/source-registry.md` |
| 3 | D-03 holiday meaning | Years 2018 through 2026 are transcribed. Incomplete years stay out of the release. | Source registry and `contracts/fixtures/holiday_calendar.json` |
| 4 | D-05 / OF-003 budget | Recorded. USD 0, local Docker, Founder / Product Lead operates it. | `docs/decisions/ADR-003-budget-and-ownership.md` |
| 5 | D-04 and D-07 | Recorded. Anonymous read-only. Local command, no Airflow. | ADR-003 |
| 6 | D-08 / OF-004 retention | Recorded. Keep raw files and published releases on this computer. Withdraw in one local action. | `docs/decisions/ADR-004-retention-and-withdrawal.md` |
| 7 | D-02 history start | Raw ERA5 responses stored for 2018-01-01 through 2026-10-25. Six trailing days are null. | `ingestion/raw/open-meteo-era5/` |
| 8 | OF-005 contract mismatches | Recorded in ADR-005. Date span and rate numbers remain unset. | `docs/decisions/ADR-005-contract-reconciliation.md` |
| 9 | OF-006 repository | Recorded. Pinned ruff and pytest, CI job `ci`, secret scan, code owners file. | `README.md` and `.github/workflows/ci.yml` |

## Next — backlog recorded

The ERA5 responses for 2018-01-01 through 2026-10-25 are stored. The archive left 2026-10-20 through 2026-10-25 null. The hosted limits and the release decision stay open.
