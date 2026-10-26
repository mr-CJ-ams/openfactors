# ADR-008 — Public Beta v0.1 query limits

| Field | Value |
|---|---|
| Decision | The 366-day span is reconsidered. Page size and export caps stay as they are. |
| Status | Reconsidered on 2026-10-26. Not approved. Not enforced. |
| Number | ADR-008. ADR-005 is unchanged. |
| Public release | No-go. This record does not deploy, and it does not mark a release go. |

The 366-day maximum was an owner-selected initial policy limit. It is not a measured benchmark, and it is not enforced. A one-year cap would cut the historical range this dataset is meant to serve. This record leaves that cap unimplemented and checks whether one location's full stored history already fits the existing page size and export caps.

The rate of 60 requests per minute per client IP remains an owner-selected initial policy limit. It is not enforced. It waits on a public hosting architecture. This record does not add a limiter on localhost and does not trust a forwarding header.

These observations are one local look at the dev database on 2026-10-26. They are not measured benchmarks, not a hosted load test, and not proof of production readiness.

## What is stored

The active release `2efd0071-82f9-4854-9d03-a55b29adc04c` has 16,105 rows: 3,221 rows for each of the five cities, from 2018-01-01 through 2026-10-26. One row is one location on one date, so one city covers 3,221 inclusive dates.

The fact table also holds 112,735 rows across 7 release ids, from local withdrawal copies. Its total relation size is 25,681,920 bytes, of which 16,138,240 bytes are the heap. A query pins one release id, so those extra copies are not part of one response.

## How a full-history request runs

A factor or export request takes one `location_id`. The primary key is `(release_id, location_id, date)`. The location-date range for the active release is an index-only scan of that key.

`GET /api/v1/factors/daily` still materializes every matching row for the location, counts them, and then returns one page. The page size default is 100 and the maximum is 1,000. One city at 3,221 rows is 4 pages at 1,000 rows, or 33 pages at the default of 100.

`GET /api/v1/exports/daily-external-factors-ph` loads every matching row, builds the whole file, and then applies the caps. The caps remain 100,000 rows and 50,000,000 bytes. One city at 3,221 rows is inside the row cap.

The serving view joins each request to a check that the pinned release has no unapproved source. On this database that check reads all 16,105 rows of the active release before the location scan returns 3,221 rows. The page limit does not remove that check.

Each API read starts a `psql` process in the local Postgres container. The SQL times below exclude that process. The wall times include it. Both are one warm-cache run for Manila, 2018-01-01 through 2026-10-26. They are not a percentile and they are not a pass mark.

| Observation | Result |
|---|---|
| Factor SQL, page size 1,000, inside Postgres | 27.054 ms. Shared buffers were already in memory. |
| Export SQL, inside Postgres | 14.771 ms. Same cache. The sort used 222 kB. |
| Factor page through `load_factor_page` | 0.750 seconds. Page 1 returned 1,000 rows. `total` was 3,221. |
| CSV through `load_export_rows` and `csv_bytes` | 0.874 seconds. 3,221 rows and 286,098 bytes. |

286,098 bytes is below the 50,000,000-byte cap. Pages 2 through 4 and the other four cities were not timed. Five cities are five requests, because one request takes one location.

## Risks

The database work for one city fits in memory at this size. The cost that grows with a walk of the whole history is the number of API calls: 4 calls at page size 1,000, or 33 at page size 100, and each call starts `psql`. That total was not timed.

The source check reads every row of the pinned release on every page and every export. It grows with the release, not with the page. More cities or more years make that check larger even when the caller asks for one page.

The export builds the file before it refuses an oversized result. Today's one-city file is 286,098 bytes. The 50,000,000-byte cap is still the bound if a later file is much wider.

Later pages use `OFFSET`. At 3,221 rows that offset is small. It was not measured on a larger table.

## Reproducible check

Run this on the local dev database. Record the figures. Do not set a pass threshold, and do not mark the release go.

1. Read `publish.active_dataset_release` for `daily_external_factors_ph`.
2. Count rows, minimum date, and maximum date for that release id, by `location_id`.
3. As `of_api`, `EXPLAIN (ANALYZE, BUFFERS)` the factor query and the export query for one location from that minimum through that maximum, with page size 1,000.
4. Call `load_factor_page` for page 1 and for the last page, and call `load_export_rows` plus `csv_bytes` for the same location and dates. Record the status, the row total, the CSV byte length, and the wall time of each call.
5. Keep the result beside this record. A hosted or repeated run is a separate measurement under OF-042.

## What stays in place

| Limit | Decision |
|---|---|
| Inclusive query date span | The 366-day proposal is reconsidered. It is not enforced. A query may still use any `start_date` on or before `end_date`. |
| Public request rate | 60 requests per minute per client IP stays a proposal. No limiter is installed. |
| Factor page size | Maximum 1,000 rows. Default 100. Unchanged. |
| Export caps | 100,000 rows and 50,000,000 bytes. Unchanged. |

OpenAPI, the API guide, and the download form still say that no date-span maximum is set. That matches the code.

## Required follow-up, not started

OF-001 through OF-049 stay in progress. No ticket is marked done. No 366-day check is added.

| Follow-up | Existing ticket | Work |
|---|---|---|
| Leave the date span uncapped unless a later record chooses a different bound from a repeated measurement. | OF-042 | Not started. The check above is the local procedure. |
| Keep page size 1 to 1,000 and the export ceilings at 100,000 rows and 50,000,000 bytes. | OF-033 | Already enforced. |
| Enforce a per-IP rate only after a hosting record names the client address. Do not count localhost as that address, and do not read a forwarding header before then. | OF-041 | Not started. |
| Run the QA sheet before any case leaves "Not run". | OF-043 | Not started. |
