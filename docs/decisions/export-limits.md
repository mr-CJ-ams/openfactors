# Export limits

`GET /api/v1/exports/daily-external-factors-ph` builds the whole CSV or Parquet file, then decides. A response above 100,000 rows or 50,000,000 bytes is `422 EXPORT_LIMIT`. The body is the error envelope. No file bytes are sent, and no local export file is renamed into place. The message tells the caller to split the date interval. It does not name a maximum number of days.

50 MB in this ceiling is 50,000,000 bytes. A file of exactly 100,000 rows and exactly 50,000,000 bytes is still sent. One more row, or one more byte, is refused.

No date-span maximum is set. No request-quota number is set. The export route uses the same limiter as the other routes: when no limiter is configured, the request proceeds; when a limiter returns a non-negative number of seconds, the response is `429` with `Retry-After`.

A successful file response adds `ETag`, a quoted SHA-256 of those file bytes. `Link` points at the dataset dictionary for the same release and at each approved source URL. The dictionary and those URLs carry the attribution and license text.

The attribution manifest is JSON. Request it with `Accept: application/vnd.openfactors.export-manifest+json` on the same export URL. It records the release, the filters, the row count, the file size, the file SHA-256, and the approved source attribution. Its own `ETag` is the hash of the manifest bytes. A source missing attribution, license, or an `https` URL is not exported.

When the local server stores an export, it writes the manifest first and renames the data file last. If that publish stops, both the partial file and the manifest are deleted. A withdrawn release still deletes its export directory.
