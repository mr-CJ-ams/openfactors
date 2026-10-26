# Example joins

The examples are local files. They are not HTTP routes.

`examples/join_factors.ipynb` joins sample orders to one pinned factor release. `examples/join_factors.sql` is the same join against `publish.accessible_factors`, with one release pin. The orders are sample data, not a measured business result. A sample day with no factor row is left out. The join does not show that weather or a holiday caused the orders.

The notebook runs in the test suite without a notebook server. The SQL runs as the public role when the local database is up. `python -m examples.join_factors` runs that SQL against the active local release.
