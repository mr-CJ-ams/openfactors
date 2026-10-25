# Catalog page

`GET /` is the published-dataset catalog. It is HTML, served by the same local API process. It is not an OpenAPI path.

Each published dataset shows its id, schema version, and active release, then the grain, geography, inclusive coverage dates, last successful publication, license summary, attribution, source links, and the same gap list as the dataset dictionary. The page clock is labeled `Catalog response generated` and is not the publication time. A row without a publication time is left out. The page does not render a failed ingestion time.

An empty catalog says that no published dataset is available. If the catalog cannot be read, the page returns 503 and does not invent a dataset or a publication time. Text from the catalog is escaped.

Weather and holidays are contextual factors, not proof of causation for business outcomes. Each published dataset links to its dictionary and download page.
