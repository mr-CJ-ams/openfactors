# ADR-001 — Pilot locations and working historical start

| Field | Value |
|---|---|
| Decision | D-01 (locations). D-02 start date confirmed for the approved weather source. |
| Ticket | OF-001 |
| Status | Working baseline, adopted 2026-10-24 |
| Public release | OF-002, OF-003, and OF-004 have working baselines. Still blocked by OF-005. |
| Supersedes | PRD placeholders "selected pilot locations" and "historical range to be set" |

## Decision

The MVP dataset `daily_external_factors_ph` uses five Philippine cities. Each published row is one `location_id` on one `Asia/Manila` calendar date. `location_id` values below are permanent public identifiers.

| location_id | display_name | admin_region | admin_province | latitude | longitude | Why this city |
|---|---|---|---|---:|---:|---|
| `ph-manila` | Manila | National Capital Region | — | 14.590600 | 120.979900 | Dense lowland capital. Western Luzon reference. |
| `ph-baguio` | Baguio | Cordillera Administrative Region | Benguet | 16.411900 | 120.593300 | Highland city, about 1,465 m. Cooler than the lowland sites. Province is the geographic grouping; Baguio is a highly urbanized city. |
| `ph-cebu-city` | Cebu City | Central Visayas | Cebu | 10.309000 | 123.893000 | Central Visayas reference. Highly urbanized city, geographically in Cebu. |
| `ph-tacloban` | Tacloban | Eastern Visayas | Leyte | 11.243000 | 125.008100 | Eastern seaboard. Different rainfall and storm exposure from the western cities. |
| `ph-davao-city` | Davao City | Davao Region | Davao del Sur | 7.063900 | 125.608300 | Southern Mindanao. Large city extent; this point does not describe the whole municipality. |

Coordinates are representative city reference points from the Wikipedia list of Philippine cities, retrieved 2026-10-24 (Baguio from that city's coordinate box: 16.4119° N, 120.5933° E). They are not weather stations and not survey monuments. `provider_location_ref` and `weather_grid_description` are the ERA5 cells returned for 2018-01-01, copied in OF-012. Published weather is a provider grid or model value at this point, not a claim about every place inside the city.

**Historical start:** `2018-01-01` inclusive, through the latest `Asia/Manila` date included in a successful publish. `valid_from` on every pilot row is `2018-01-01`. `valid_to` is null. On 2026-10-24, Open-Meteo ERA5 returned that start date for all five cities (`contracts/fixtures/open_meteo_era5_2018-01-01.json`). The provider documents this archive from 1940. OF-012 records the cell mapping and the local-day parser. The raw ERA5 responses for 2018-01-01 through 2026-10-25 are stored under `ingestion/raw/open-meteo-era5/`. The archive left 2026-10-20 through 2026-10-25 null. That hole stays null. Do not shrink the advertised range quietly to make coverage look complete.

Five locations from 2018-01-01 through 2026-10-24 is about 16,000 location-dates, inside the provisional 100,000-row export cap.

## Rules that go with the list

- Timezone for every pilot location is `Asia/Manila`. Country code is `PH`.
- Grain is city reference point × local date. Barangay and station grain are out of scope.
- A `location_id` is never reused, renamed, or pointed at a different place. A coordinate correction or a new provider grid that changes the geographic meaning gets a new `location_id` and a new ADR.
- Unknown `location_id` values are rejected. They are not created on the fly by an API query.
- Adding a sixth location is a new decision, not a silent fixture edit.
- Contract tests use the five IDs in this ADR. The retired placeholder location id was removed from the API and data-model examples in OF-005.

## Machine-readable copy

`contracts/fixtures/pilot_locations.json` matches `core.dim_location` in the data-model document. If this ADR and that file disagree, the fixture is wrong until they are edited together.

## What this decision does not close

- D-02 is confirmed for the start date, not for every later day. OF-018 loads these five rows into `core.dim_location` and fills `core.dim_date` from 2018-01-01 through the current Asia/Manila date. That end is the working spine, not a maximum date span. The raw ERA5 pull through 2026-10-25 is stored and is not a published release.
- D-03, D-05, D-06, D-07, and D-08 are recorded. No public row is approved by this ADR. Contract reconciliation is OF-005.
- The weather grid cells observed on the 2018-01-01 probe are copied into the warehouse in OF-012.

## Acceptance

OF-001 is satisfied for the location list and the working date denominator when this ADR and the fixture stay in sync. It is not a public-release signature.

| Role | Name | Date | Result |
|---|---|---|---|
| Product owner | Founder / Product Lead | 2026-10-25 | Confirmed these five cities |
| Engineering | Repository baseline | 2026-10-24 | Adopted for fixtures |
