# Public API resource limits

These limits protect one API process. They are provisional until staging measurements. They are not a hosted load profile, and they do not approve public hosting. Public Beta v0.1 stays no-go.

| Limit | Value | Why |
|---|---|---|
| Request window | 60 requests per rolling 60 seconds for one resolved client. | This is the owner-selected starting window. A refused request keeps the existing 429 `RATE_LIMITED` body and `Retry-After`. Rejected requests do not extend the window. |
| Loopback | Unlimited. | Local tests and `python -m openfactors.api.catalog` on this computer talk to `127.0.0.1`. Counting that peer would make the local server refuse itself. |
| Client address | The TCP peer. | `Forwarded` and `X-Forwarded-For` are ignored unless that peer is listed in `OPENFACTORS_API_TRUSTED_PROXIES`. The list is empty unless it is set. A trusted peer must send one valid chain. The client is the nearest address in that chain that is not itself a trusted proxy. A missing, doubled, or non-IP chain is a malformed request. |
| Tracked clients | At most 4,096 addresses that still have a request inside the window. | This bounds memory. It is not a second product quota. A full table of active clients is refused for 60 seconds instead of dropping one of them. |
| SQL timeout | `statement_timeout = 5s` on API reads only. | One API statement cannot hold a session longer than this. Admin, ingestion, dbt, and promotion sessions do not set it. |
| API processes | 4 `psql` processes. | A fifth request gets the existing 503 body and does not wait. The slot is held only while that process runs. |
| Process deadline | 10 seconds, then the process is killed and reaped. | The 5-second statement limit does not cover a `psql` process that never starts. Ten seconds leaves room for startup and for a statement that uses its full 5 seconds. The slot is released after the process is gone. |
| Socket timeout | 30 seconds on the accepted connection. | A client that stops sending cannot occupy the API thread forever. This is outside the SQL timeout. |
| Export cleanup | On catalog startup, delete only recognized unfinished files. | A file named `.{filename}.partial` or `.{filename}.manifest.partial` inside a UUID folder of the configured export directory is unfinished. Completed files, other names, other directories, and symbolic links stay. |

A date-span maximum is still not set.

Temporary PostgreSQL password files left by a killed process are a separate follow-up. This change does not scan the system temporary directory.
