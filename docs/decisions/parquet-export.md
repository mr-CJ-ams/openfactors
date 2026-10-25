# Parquet export

`GET /api/v1/exports/daily-external-factors-ph?format=parquet` returns the same 12 public fields as the factor query and the CSV export, for one location, one inclusive date range, and one pinned release. A holiday name is stored as text, without the spreadsheet apostrophe used in CSV.

`date` is `date32`. `temperature_mean_c` is `decimal128(7, 3)`. `precipitation_mm` is `decimal128(10, 3)`. `source_updated_at` is a UTC timestamp, or null when the source did not provide one. The other fields are strings or booleans. The file metadata stores `dataset_version` and `release_id` for the pinned release.

The row cap, the byte cap, `ETag`, and the attribution manifest are described in `docs/decisions/export-limits.md`.
