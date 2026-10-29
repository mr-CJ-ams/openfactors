# Dictionary and download page

`GET /datasets/daily-external-factors-ph` lists the 12 public fields with type, nullable status, unit, description, and source. The controls are labeled. Tab order is the document order. There is no positive tabindex.

The form asks for one location, an inclusive start date, an inclusive end date, CSV or Parquet, and an optional release id. Leave the release blank to use the active release. Invalid filters stay on the page. The messages match the API, including `start_date must be on or before end_date` and `start_date and end_date span more than 366 calendar days.` The page does not send a file for an invalid form. The page stays HTML. The export API returns 422 for that same span.

When the filters pass, the page prints one pinned release id and a download link that carries that same id. The link is the export API. An unknown or withdrawn release is shown as unavailable, with no download. An export above 100,000 rows or 50,000,000 bytes is still refused by that API. A span over 366 inclusive dates is refused on this page and by that API.

Field text is escaped. If the release list cannot be read, the page returns 503 and does not invent a release.
