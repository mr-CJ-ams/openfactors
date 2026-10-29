# ADR-009 — Public Beta v0.1 date span

| Field | Value |
|---|---|
| Decision | Maximum inclusive query span of 366 calendar days |
| Status | Adopted by the product owner on 2026-10-29. The factor query and both export formats enforce it. |
| Number | ADR-009. ADR-008 remains the 2026-10-26 reconsideration. ADR-005 is unchanged. |
| Public release | No-go. This record does not deploy, and it does not mark a release go. |

ADR-008 left the 366-day idea unimplemented. The owner now adopts that maximum for Public Beta v0.1. Page size, export caps, and the provisional request window stay where ADR-008 left them.

## Counting

`start_date` and `end_date` stay inclusive ISO dates. The existing rule remains: `start_date` must be on or before `end_date`. The factor query and the export both keep every date from `start_date` through `end_date`, including both ends.

The span is the number of calendar dates in that closed interval:

`(end_date - start_date).days + 1`

A span of 366 is allowed. A span of 367 is over the limit. A leap day counts as one date.

| Request | Inclusive dates | Result once enforced |
|---|---|---|
| 2023-01-01 through 2023-12-31 | 365 | Allowed |
| 2024-01-01 through 2024-12-31 | 366 | Allowed |
| 2024-01-01 through 2025-01-01 | 367 | Rejected |

A longer history is several requests. Each request has its own inclusive pair, and each pair is at most 366 dates. The next `start_date` can be the day after the previous `end_date`.

## Rejection, once the check exists

A span over 366 dates uses the existing client-error behavior. Factor and export requests return HTTP 422 and the error envelope, with code `INVALID_DATE_RANGE`. A date that is not an ISO date stays `INVALID_DATE`. A start after the end keeps the message `start_date must be on or before end_date`. The span message is separate: `start_date and end_date span more than 366 calendar days.` The download form returns 422 and shows that span message on the form.

## Where the check runs

`_filters` in `src/openfactors/api/factors.py` counts the inclusive dates after the ISO-date and order checks. CSV and Parquet export call that same function and return 422 before a file is built. The download form in `src/openfactors/api/dataset_page.py` shows the same span message and does not offer a download. That page stays HTML, including for an invalid form. `tests/test_date_span.py` accepts 366 inclusive dates, including the 2024 leap year, and rejects 2024-01-01 through 2025-01-01.

## Acceptance

| Role | Result |
|---|---|
| Founder / Product Lead | Adopted by instruction on 2026-10-29 |
| Enforcement | Present on the factor query, CSV export, and Parquet export. Launch checklist item A7 records the test result. |
