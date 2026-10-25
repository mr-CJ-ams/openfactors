# CSV export

`GET /api/v1/exports/daily-external-factors-ph?format=csv` returns the same 12 public fields as the factor query, for one location, one inclusive date range, and one pinned release. The file is UTF-8 without a byte-order mark. Lines end in LF. A null is an empty cell. Booleans are `true` or `false`. Numbers use a dot and no thousands separator. Dates are `YYYY-MM-DD`. `source_updated_at` is the ISO timestamp already stored on the row, or empty when the source did not provide one.

`holiday_name` is the only free-text field taken from outside the product. If that text starts with `=`, `+`, `-`, `@`, a tab, or a carriage return, the cell is prefixed with one apostrophe so a spreadsheet does not treat it as a formula. The apostrophe is not part of the holiday name. A reader removes one leading apostrophe when the next character is one of those six. Other fields are not prefixed. A name that already starts with an apostrophe plus one of those six characters is written unchanged, and reading it back drops that apostrophe.

`format=parquet` is described in `docs/decisions/parquet-export.md`. The row cap, the byte cap, `ETag`, and the attribution manifest are described in `docs/decisions/export-limits.md`.
