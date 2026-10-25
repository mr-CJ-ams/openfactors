# Security checklist

These checks were run on this computer. The API process listens on `127.0.0.1`. It is not a public TLS endpoint. Postgres and object storage require TLS. No request quota is set.

| Check | Result |
|---|---|
| Public database role | `of_api` can read the serving views. It cannot read `control`, `staging`, `core`, or `quality`, and it cannot insert, update, or delete. |
| Hidden paths | Requests for raw, staging, and control paths return 404. The response does not include the query value. |
| Write methods | POST, PUT, PATCH, and DELETE on the public paths return 405 and do not call the readers. |
| TLS | `pg_hba.conf` rejects connections without SSL. A plaintext database connection is refused. Object storage is checked over HTTPS. |
| Address | Published database and object-storage ports are bound to `127.0.0.1`. The API default host is `127.0.0.1`. Host database connections use a password, not trust. |
| Secrets | `tools/scan_secrets.py` finds none in the tracked tree. A handler exception is not copied into the response. |
| Throttling | With no limiter, a request is not 429. A configured limiter returns 429 and `Retry-After`. That number is the limiter's, not a product quota. |
