# OpenFactors API

The API is read-only. Authentication is not required. Do not send a key or a token. Accounts and saved workspaces are not part of this API. Monthly economic indicators are not in this dataset. They are not copied onto daily rows. Every example below is a GET, and none of them signs the request.

The machine-readable contract is `GET /openapi.json`. This page is the short guide for those routes. HTML pages such as `/` and `/datasets/daily-external-factors-ph` are not versioned API resources.

The daily refresh is a local command. It is not an HTTP route.

Historical backfill is a local command. It is not an HTTP route.

The operations report is a local command. It is not an HTTP route.

Database restore, raw restore, release rollback, and failed-run recovery are local procedures. They are not HTTP routes.

Write requests are refused. Raw and staging paths are not part of the API.

The load and reliability report is a local command. It is not an HTTP route.

The release checklist is a local command. It is not an HTTP route.

The public beta command is local. It does not deploy, and it is not an HTTP route.

The operator backfill form is a local command. It is not part of the public API.

The join examples are local files. They are not HTTP routes.

## Endpoints

- `GET /api/v1/datasets` lists published datasets. An empty list is 200. A withdrawn release is left out.
- `GET /api/v1/datasets/daily-external-factors-ph` returns the 12-field dictionary, sources, and limitations for the active release or a pinned `release_id`.
- `GET /api/v1/factors/daily` returns one location for an inclusive date range, from one release.
- `GET /api/v1/exports/daily-external-factors-ph` returns those same rows as CSV or Parquet.
- `GET /openapi.json` returns the OpenAPI 3.1 document.

The public row fields, in order, are `date`, `location_id`, `country_code`, `temperature_mean_c`, `precipitation_mm`, `is_public_holiday`, `holiday_name`, `is_weekend`, `weather_source_id`, `holiday_source_id`, `source_updated_at`, and `dataset_version`. `release_id` is not a row column. It is a property of the response.

```curl 200
curl "http://127.0.0.1:8000/api/v1/datasets"
```

```curl 200
curl "http://127.0.0.1:8000/api/v1/datasets/daily-external-factors-ph?release_id=00000000-0000-4000-8000-000000000360"
```

```curl 200
curl "http://127.0.0.1:8000/openapi.json"
```

## Limits

- One `location_id` per factor or export request. An unknown location is 404 with `That location is not available.`
- `start_date` and `end_date` are inclusive ISO dates. `start_date` must be on or before `end_date`.
- No date-span maximum is set. No request quota is set.
- Factor `page` defaults to 1 and must be at least 1. `page_size` defaults to 100 and must be from 1 to 1000.
- An export above 100,000 rows or 50,000,000 bytes is 422 `EXPORT_LIMIT`. The message says to split the date interval. No file bytes are returned.
- `format` is `csv` or `parquet`.

```curl 200
curl "http://127.0.0.1:8000/api/v1/exports/daily-external-factors-ph?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01&format=csv&release_id=00000000-0000-4000-8000-000000000360"
```

## Pagination and release pins

Read the active release once when `release_id` is omitted. The JSON body and the `X-Release-Id` header both carry that id. Send the same `release_id` on later pages so the pages stay on one release.

Rows are ordered by `date`, then `location_id`. A page past the end is 200 with `data` empty and the real `total`. `data_as_of` is the release publication time. It is not the weather date and not the response clock.

The id in these examples is a sample pin. Use the `release_id` from your own first response.

```curl 200
curl "http://127.0.0.1:8000/api/v1/factors/daily?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01"
```

```curl 200
curl "http://127.0.0.1:8000/api/v1/factors/daily?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01&page=2&page_size=100&release_id=00000000-0000-4000-8000-000000000360"
```

## Change history

Schema `1.0.0` is the only public schema. It has twelve fields in a fixed order. No public field is deprecated. A breaking change is a new `dataset_version`. A breaking change is a removed field, a changed meaning, a changed type, a changed order, or a changed nullability rule. `dataset_version` is on every row and in the dictionary.

A change to values is a new `release_id`. The old release stays readable when you pin it, unless it has been withdrawn. An unknown pin and a withdrawn pin both return `That release is not available.` A corrected source value is a new release. It does not edit the old one.

To reproduce an analysis:

- Save the `release_id` from the first response body and from `X-Release-Id`.
- Save `dataset_version` from a row.
- Send that same `release_id` on later pages and on the export.

The active release can move. The pin does not. No schema has been replaced. This note does not list releases.

## Errors

Errors use one JSON object: `error.code`, `error.message`, and `error.trace_id`. A 500 message does not include the internal exception text.

- 200 is a valid result, including an empty page.
- 400 means the query string is malformed.
- 404 means the location or the release is not available. An unknown release and a withdrawn release use the same message: `That release is not available.`
- 405 means the method is not GET. `Allow` is `GET`.
- 422 means a filter, a page, a format, an unknown parameter, or an export cap is invalid. An inverted range returns `INVALID_DATE_RANGE` and `start_date must be on or before end_date`.
- 429 means a configured limiter refused the request. The response includes `Retry-After`. No quota number is configured here.
- 500 includes a trace id.
- 503 means the catalog or the query could not be read.

```curl 422
curl "http://127.0.0.1:8000/api/v1/factors/daily?location_id=ph-manila&start_date=2018-01-02&end_date=2018-01-01"
```
