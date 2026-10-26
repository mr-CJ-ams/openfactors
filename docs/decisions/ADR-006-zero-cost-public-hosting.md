# ADR-006 — Zero-cost public hosting proposal

| Field | Value |
|---|---|
| Decision | Public serving under a USD 0 recurring cap. Temporary id ADR-HOST-00 is closed by this number. |
| Source proposal | `H_OpenFactors_ADR_Zero_Cost_Public_Hosting_v0.1.md` |
| Status | Proposed on 2026-10-26. Not approved. |
| Number | ADR-006. ADR-002 is unused in this repository and is not taken. ADR-005 is unchanged. |
| Public release | No-go. No host is selected. No public URL is created. |

This record compares provider terms. It does not deploy, does not set a date span or a rate number, and does not state a recovery-time target.

## What this repository actually runs

The public process is the Python standard-library server in `src/openfactors/api/catalog.py`. It listens on 127.0.0.1:8000 unless `OPENFACTORS_API_BIND_HOST` is set. It is not FastAPI, and there is no React app. The catalog, dictionary, and API guide are HTML from that same process.

The API reads Postgres as `of_api` through the `psql` client (`ops/api_read.py`), not through Docker Compose exec. Local reads use `sslmode=require`: the session is encrypted and the self-signed certificate is not verified. A configured host must use `sslmode=verify-full` and `OPENFACTORS_API_PGSSLROOTCERT`. Migrations, ingestion, dbt, and promotion still use their own roles and Compose. Dev Postgres is `postgres:16.10` on 127.0.0.1:55430. Object storage is the local `pgsty/silo` container, also on 127.0.0.1. Migrations are `ops/migrations/0001` through `0011`. Promotion is `ops/promote.py`. `python -m openfactors.orchestration.release` records the public beta as not deployed. No host is selected.

CI is `.github/workflows/ci.yml`. It lints, scans secrets, checks the contract, diffs OpenAPI, installs the PostgreSQL client, starts empty dev and test databases, and runs pytest. It does not deploy. Secrets for the local stack live under `ops/local/runtime/`, which is gitignored.

The five city names and coordinates are already in ADR-001 and `contracts/fixtures/pilot_locations.json`. The product owner confirmed those cities on 2026-10-25. PSGC codes are not in that ADR. This record does not add them.

## Terms checked on 2026-10-26

Prices and quotas below are copied from the provider page on that date. They are not a benchmark of this dataset, and they can change. An account screen was not opened, so a documented policy is not proof that this GitHub user has that plan with billing disabled.

| Provider page | What the page said |
|---|---|
| [GitHub plans](https://docs.github.com/en/get-started/learning-about-github/githubs-plans) | GitHub Free includes GitHub Pages in public repositories. |
| [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits) | Static sites. Published site at most 1 GB. Soft bandwidth 100 GB per month. Not for a commercial SaaS. Rate limits may return 429. |
| [GitHub Pages HTTPS](https://docs.github.com/en/pages/getting-started-with-github-pages/securing-your-github-pages-site-with-https) | `github.io` sites created after 2016-06-15 are HTTPS. Enforce HTTPS is a setting. |
| [Cloudflare Pages limits](https://developers.cloudflare.com/pages/platform/limits/) | Free plan: 500 builds per month, one build at a time, 20,000 files, 25 MiB per file. Page dated 2026-09-05. |
| [Cloudflare Pages pricing](https://developers.cloudflare.com/pages/functions/pricing/) | Static asset requests are free and unlimited. Pages Functions count against Workers. Page dated 2026-09-08. |
| [Render free](https://render.com/docs/free) | Free web services can run a Python process and spin down after 15 minutes without inbound traffic. 750 free instance hours per workspace per month; at zero hours, free web services suspend until next month. Bandwidth past the included amount is billed if a payment method is on the account; otherwise free services suspend. Free Render Postgres expires 30 days after creation. The page says not to use free instances for production. |
| [Neon plans](https://neon.com/docs/introduction/plans) | Free plan is USD 0 per month: 100 CU-hours per project, scale to zero after 5 minutes, 1 GB storage per project, 6-hour history window. Launch and Scale are paid usage. |
| [Neon free-plan FAQ](https://neon.com/faqs/free-plan-limits-and-quotas) | On the Free plan, exhausting compute or transfer suspends compute. Storage writes that would grow the database fail. The FAQ says Neon does not bill overages on the Free plan. |
| [Fly.io pricing](https://fly.io/docs/about/pricing/) | New organizations have no free tier. The smallest always-on machine is USD 2.19 per 30 days. The trial is up to 2 machine hours or 7 days, whichever comes first. |
| [Fly.io cost management](https://fly.io/docs/about/cost-management) | A free allowance does not cap the bill. Usage past an allowance is billed. |

## Two configurations, neither selected

Configuration A is Cloudflare Pages for static files, a Render free web service for the Python process, and Neon Free for Postgres.

Configuration B is a new Fly.io organization running the API and a database.

The local Docker stack in ADR-003 stays the only approved runtime. It is not one of the two public configurations.

| Gate | A. Pages + Render + Neon | B. New Fly.io organization |
|---|---|---|
| Runs the current server and OpenAPI routes | FAIL. The server is not FastAPI, and it reaches Postgres through local Docker. Neither host can run that path unchanged. | FAIL. Same process. The trial also ends. |
| Postgres roles match `of_api` read-only | UNKNOWN. Neon is Postgres, but this code has no hosted connection. | UNKNOWN. Not tried. |
| Immutable raw archive and a tested restore | UNKNOWN. No hosted archive was chosen. Neon’s 6-hour history is a vendor feature, not a measured restore of this dataset. | UNKNOWN. Not tried. |
| Scheduler and secrets stay off the public process | UNKNOWN. GitHub Actions runs CI only. | UNKNOWN. |
| Public HTTPS | UNKNOWN for the API. Cloudflare documents HTTPS for Pages assets. Render documents managed TLS on free web services. | FAIL for a lasting URL. The new-account trial is not a lasting plan. |
| Date span and request rate | UNKNOWN. No hosted measurement exists, so no number is proposed. | UNKNOWN. Same. |
| Hosted load | UNKNOWN. No hosted run was recorded. | UNKNOWN. |
| Hard USD 0, with no path that bills | FAIL. Render’s own page says extra bandwidth is billed when a payment method is present. | FAIL. The smallest always-on machine has a price, and a free allowance does not cap the bill. |
| Non-commercial redistribution | UNKNOWN against each host’s acceptable-use rules. The Open-Meteo term in the source registry is unchanged. | UNKNOWN. |
| Owner can monitor and recover | UNKNOWN. No paging channel was configured. | UNKNOWN. |

A FAIL on hard spend or on running the current server is enough to reject that configuration. UNKNOWN is not a pass. No configuration is approved.

GitHub Pages, read alone, also fails the live API gate. Its documented product is a static site. This API is a process.

## Gates that stay open

| Gate | State |
|---|---|
| G0 repository sync | Pending. This comparison is local until a commit is requested. Secrets under `ops/local/runtime/` stay untracked. |
| G1 five-city registry | Names and coordinates are in ADR-001. PSGC codes are not recorded. |
| G2 hosting approval | Pending. This file is the comparison, not the approval. |
| G3 staging on a host | Pending. |
| G4 hosted performance | Pending. |
| G5 hosted restore | Pending. No recovery-time number is set. |
| G6 paging | Pending. No paging service is configured. |
| G7 QA sheet | Not run. |
| G8 signatures | Pending. No signature file is created. |

## What a later approval would still require

A later ADR can name a host only after a proof on that host, an account screen that shows no billable overage, and a separate decision for any code change that replaces the local `psql` path. Until then the public release command stays not deployed.
