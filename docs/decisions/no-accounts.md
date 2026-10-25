# No accounts

Accounts and saved workspaces are not part of the MVP. The public API does not have a login, a password, or a workspace.

The OpenAPI document lists four GET paths: the catalog, the dictionary, the factor query, and the export. `/openapi.json` is the contract document. HTML pages are `/`, `/docs`, and `/datasets/daily-external-factors-ph`.

A request for a login, account, signup, or workspace path returns 404. Those responses do not set a cookie.
