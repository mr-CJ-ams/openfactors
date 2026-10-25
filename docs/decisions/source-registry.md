# Source registry — weather and national holidays

| Field | Value |
|---|---|
| Decisions | D-06 redistribution, D-03 holiday meaning |
| Ticket | OF-002 |
| Read on | 2026-10-24 |
| Status | Working baseline for a free, no-ads MVP |
| Public release | Still blocked by OF-005 contract reconciliation |
| Machine copy | `contracts/fixtures/source_registry.json` |

This is a reading of the provider pages below. It is not a counsel opinion. The binding constraint is Open-Meteo's free API: the public product stays non-commercial (no subscriptions, no advertising) while this weather source is used. A paid or ad-supported OpenFactors requires their paid API before any further weather calls.

## Weather — `open-meteo-era5`

| Question | Answer |
|---|---|
| Provider and terms read | Open-Meteo Historical Weather API. Terms: https://open-meteo.com/en/terms . License: https://open-meteo.com/en/license . Docs: https://open-meteo.com/en/docs/historical-weather-api |
| Date read | 2026-10-24 |
| Calling the API for the five pilot points | Yes, on the free non-commercial tier. No API key. Endpoint `https://archive-api.open-meteo.com/v1/archive`. Under 10,000 calls/day, 5,000/hour, and 600/minute. |
| Storing the raw response | Yes. API data is CC BY 4.0, which allows copying the material. |
| Public API of derived daily values | Yes, with the attribution below, and only while OpenFactors is non-commercial. |
| CSV and Parquet of those values | Yes, same conditions. |
| Attribution, copied from their license page | `Weather data by Open-Meteo.com` linking to https://open-meteo.com/ . Also link the license https://creativecommons.org/licenses/by/4.0/ and say the values were adapted to the OpenFactors daily table. |
| Commercial use | Forbidden on the free API. CC BY 4.0 allows commercial redistribution of data already obtained, but new calls for a commercial product use the paid customer API. |
| Takedown | Stop new ingestion and suspend the source. ADR-004 then withdraws every published release that includes it, in one local action, and those release ids return 404. |

**Series to request.** Model `era5` (about 25 km, archive documented from 1940). Daily variables `temperature_2m_mean` (°C) and `precipitation_sum` (mm). `timezone=Asia/Manila` is required for daily values. Do not use the default Best Match mix of models. These are reanalysis grid values, not station observations.

**Underlying dataset citation, from the Open-Meteo historical docs:** Hersbach, H., et al. (2023). ERA5 hourly data on single levels from 1940 to present. ECMWF. https://doi.org/10.24381/cds.adbb2d47

**Probe.** On 2026-10-24 a live request for `2018-01-01` returned one daily row for every pilot city. The service snapped each city point to an ERA5 cell. A reported precipitation of `0.00` is a real zero and stays `0`. A missing field stays null. Evidence: `contracts/fixtures/open_meteo_era5_2018-01-01.json`.

**Stored pull.** On 2026-10-25 the same request was repeated for 2018-01-01 through 2026-10-25, one raw response per pilot city. The cells match the probe. The archive left 2026-10-20 through 2026-10-25 null. Those days stay in the series as null. Evidence: `ingestion/raw/open-meteo-era5/`. This is not a published release.

| location_id | Requested point | ERA5 cell returned | Elevation returned |
|---|---|---|---|
| `ph-manila` | 14.5906, 120.9799 | 14.5, 121.0 | 4 m |
| `ph-baguio` | 16.4119, 120.5933 | 16.5, 120.5 | 1447 m |
| `ph-cebu-city` | 10.3090, 123.8930 | 10.25, 123.75 | 34 m |
| `ph-tacloban` | 11.2430, 125.0081 | 11.25, 125.25 | 16 m |
| `ph-davao-city` | 7.0639, 125.6083 | 7.0, 125.5 | 8 m |

`provider_location_ref` on the location fixture is the snapped cell from this probe, written as latitude and longitude to 0.01 degree. Example: `14.50,121.00`. The grid description records the 0.25 degree resolution, the cell, and the returned elevation. A later response that snaps to a different cell is a mapping change, not a silent overwrite.

## Holidays — `ph-official-gazette`

| Question | Answer |
|---|---|
| Provider and terms read | Presidential proclamations published in the Official Gazette of the Philippines, https://www.officialgazette.gov.ph/ . No holiday vendor API is approved. |
| Date read | 2026-10-24 |
| Calling an API | No third-party holiday API. Dates are transcribed into our calendar from the proclamation. |
| Storing the raw response | Yes for the proclamation PDF or HTML we used, plus the citation. We do not republish the Gazette website. |
| Public API of derived daily values | Yes for national holiday date, name, and proclamation citation. |
| CSV and Parquet of those values | Yes, with the same citation. |
| Attribution | `National holidays as proclaimed in the Official Gazette of the Philippines`, plus the proclamation number and URL on each holiday row. |
| Commercial use | No vendor feed is being resold. The MVP is non-commercial anyway because of the weather source. |
| Takedown | A corrected proclamation is a new release id. Withdrawing an old id follows ADR-004. |

Nager.Date and pholidays.com are not sources. Nager.Date may be used as a private discrepancy check. A disagreement is resolved from the proclamation, not from the API.

## D-03 holiday meaning

- **Authority.** A national regular holiday or special non-working day exists on a date only when a presidential proclamation, or a statute that proclamation cites, gives that date. The Official Gazette is where we read it.
- **Scope.** Nationwide only. City and provincial holidays are out of the MVP. Special working days are not public holidays.
- **Unknown.** A published `is_public_holiday` is true or false only, and only for a year whose calendar status is `complete`. Dates in an incomplete year are omitted from the release. They are not stored as false, and the published flag is not null.
- **Complete year.** A year is `complete` when every nationwide regular holiday and special non-working day in the reviewed proclamations has a concrete date. A movable holiday left as "date to be proclaimed" keeps that year `incomplete`.
- **Two names on one date.** Publish one row. `holiday_name` is the names joined with ` | ` in alphabetical order. `is_public_holiday` is `true` if either name is in scope.

OF-013 reads `contracts/fixtures/holiday_calendar.json`. A year stays incomplete, and every date in it is omitted, when a nationwide regular or special non-working day has no concrete date. Local holidays and special working days do not set the public flag. Years 2018 through 2026 were read on https://www.officialgazette.gov.ph/ on 2026-10-25. Each year includes the annual list, a later amendment when one replaces that list, and the proclamations that set Eid'l Fitr and Eid'l Adha. Shape tests in `contracts/fixtures/holiday_calendar_cases.json` are not proclamations.
