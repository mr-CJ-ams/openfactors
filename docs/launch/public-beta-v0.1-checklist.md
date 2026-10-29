# Public Beta v0.1 launch checklist

Public Beta v0.1 is **NO-GO**. This file does not approve a host, a purchase, a public address, or a release tag.

ADR-003 adopts a recurring ceiling of USD 10 per month for hosting, offsite backup storage, monitoring, and any other required recurring service. Current recurring spend is USD 0. The ceiling does not authorize a purchase. No price below is a quote taken from an order page on the day this file was written. Four owner decisions are recorded: the ceiling (I2), the 366-day span (A5, enforced at A7), the restore trust limits (F5), and the 24-hour offsite RPO (D3, with the copy still NOT TESTED at D1, D2, and D4).

Open-Meteo's free API allows this product only while it stays non-commercial: no subscriptions and no advertising. CC BY 4.0 is not permission to keep calling that free API for a paid or ad-supported product. No new dataset, city, frontend, Spark, Kafka, Kubernetes, or Redis work is part of this beta.

Each item states where the evidence has to come from:

- **Local** means this computer or the existing GitHub Actions job already checked it.
- **Host** means the check runs on the future serving machine, which is Linux.
- **Manual** means an account, a purchase, or a written decision by the product owner.
- **Later** means it can wait until after a public beta.

## A. Dataset and API readiness

### A1. Five-city dataset is published locally

- **Complete:** The active local release covers `ph-manila`, `ph-baguio`, `ph-cebu-city`, `ph-davao-city`, and `ph-tacloban`, with national holidays for complete years 2018 through 2026 and ERA5 daily temperature and precipitation.
- **Why:** The beta has nothing else to serve. ADR-001 fixed these cities.
- **Verify:** `python -m openfactors.orchestration.accept` still describes that stored range. The catalog page lists the same geography.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local

### A2. The October 2026 weather gap stays visible

- **Complete:** Days 2026-10-20 through 2026-10-25 stay null. The advertised start date stays 2018-01-01.
- **Why:** Shrinking the range would hide a known hole.
- **Verify:** The dataset dictionary and catalog gap list include that hole.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local

### A3. The read API matches its contract

- **Complete:** Catalog, dictionary, factor query, CSV export, Parquet export, OpenAPI, and the error envelope behave as the decision records describe. Page size is 1 to 1000. Export caps are 100,000 rows and 50,000,000 bytes.
- **Why:** Callers need one documented shape.
- **Verify:** GitHub Actions `ci` on `main`, including the dataset contract and the OpenAPI diff.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local

### A4. Provisional limits are exercised through the real proxy

- **Complete:** A non-loopback client is limited to 60 requests per rolling 60 seconds. The proxy is the only trusted peer and sends one forwarding chain. The 5-second statement timeout, 4 concurrent `psql` processes, and export caps still apply. ADR-008 does not treat these numbers as a hosted load profile.
- **Why:** A client that can reach port 8000 directly, or a proxy that is not trusted, makes every caller look like loopback and removes the limit.
- **Verify:** From outside the machine, send the 61st request inside 60 seconds and receive 429 with `Retry-After`. A direct connection to port 8000 from the internet fails.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

### A5. The date-span rule is a written decision

- **Complete:** The owner adopted a maximum of 366 inclusive calendar days. The count is `(end_date - start_date).days + 1`. A span of 366 dates is allowed. A span of 367 dates is over the limit. A longer history is more than one request. The rule, the examples, and the 422 `INVALID_DATE_RANGE` behavior are in ADR-009.
- **Why:** Callers need one bound, and the stored history is longer than one request under that bound.
- **Verify:** ADR-009 states the count, the 366-date example, the 367-date example, and the multiple-request rule.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Manual decision, recorded on 2026-10-29

### A7. The API rejects a span over 366 dates

- **Complete:** Factor queries, CSV export, and Parquet export reject an inclusive span of 367 or more dates with HTTP 422 and code `INVALID_DATE_RANGE`. A span of 366 dates still passes that check. The download form shows the same message and does not offer a download. That page stays HTML, as it does for every other invalid form. Existing ISO-date and start-before-end checks stay as they are.
- **Why:** ADR-009 is the policy. The JSON routes count inclusive dates before they read a release.
- **Verify:** `tests/test_date_span.py`. `2024-01-01` through `2024-12-31` returns 200 on the factor query and on CSV and Parquet export. `2024-01-01` through `2025-01-01` returns 422 `INVALID_DATE_RANGE` on those routes. The download page shows the same message and does not offer a download. That page stays HTML, which is its existing response for an invalid form.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local. Focused tests passed with this change.

### A6. The public API has no accounts

- **Complete:** Anonymous read-only access. No API keys and no public write route.
- **Why:** ADR-003 and the no-accounts decision define the beta.
- **Verify:** The public routes reject POST, PUT, PATCH, and DELETE with 405. No login page is served.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local

## B. Hosting and HTTPS

### B1. One HTTPS name serves the API

- **Complete:** Port 443 presents a certificate from a public CA. Port 80 is used for issuance and then redirects to HTTPS. The API process stays on `127.0.0.1:8000`.
- **Why:** Callers must not send queries to a plaintext public port.
- **Verify:** A browser or `curl` to the public name shows HTTPS, and the certificate name matches that host.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host, after a manual purchase

### B2. Postgres and the API are not on the public interface

- **Complete:** On the serving machine, Postgres listens on `127.0.0.1` only. The API bind host is `127.0.0.1`. `OPENFACTORS_API_TRUSTED_PROXIES` is the proxy on that same machine.
- **Why:** The serving-only note says a public database port, or an untrusted proxy, breaks the beta boundary.
- **Verify:** From another computer, connections to ports 5432 and 8000 on the public address fail. Only 443 and the certificate port 80 answer.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

### B3. A separate website hostname

- **Complete:** A custom domain, if wanted, points at the same HTTPS service.
- **Why:** The provider hostname is enough for a small beta.
- **Verify:** DNS and the certificate include that name.
- **Result:** NOT TESTED
- **Blocks launch:** No
- **Where:** Later

## C. Production database and snapshot delivery

### C1. Serving restore code is on main

- **Complete:** `restore-accepted staging <release-id>` is the staging create path. It checks the pinned signature and the dump SHA-256 before `CREATE DATABASE`. Staging promotion accepts only the catalog comment `openfactors-authenticated`. `dev` and `test` restore stay unsigned.
- **Why:** An unsigned dump must not become the serving candidate.
- **Verify:** `main` contains `ops/publish_snapshot.py` function `restore_accepted`, and GitHub Actions `ci` passed on the merge of pull request 11.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local. The Ubuntu CI run is the Linux check of this code. It is not a production restore.

### C2. The first serving copy is an authenticated restore

- **Complete:** On the serving host, root creates `/var/openfactors/restore` once, mode `0700`. Root creates `/var/openfactors/pin` and the allowed-signers file outside the acceptance user's tree. The first candidate is created with `restore-accepted staging <release-id>` as root on Linux. That command is not run on this development computer.
- **Why:** The pin and the private copy are the trust boundary recorded in the snapshot decisions.
- **Verify:** The command prints a candidate whose catalog comment is `openfactors-authenticated`. A second attempt with `restore-candidate staging` stops before it touches the database. An unmarked candidate is refused by promotion.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

### C3. Promotion and rollback are drilled on that host

- **Complete:** `promote-candidate staging` runs only for that authenticated candidate. `rollback-serving` returns the previous generation. Cleanup still drops a candidate only when its comment is `openfactors-demoted`.
- **Why:** The on-cluster previous generation is the fast rollback. It is not the offsite backup.
- **Verify:** After a drill promotion, the public name serves the new release id. After rollback, it serves the previous release id. `openfactors_dev` is unchanged.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

### C4. The serving machine does not run the batch stack

- **Complete:** Ingestion, dbt, Silo, and the learning Airflow project stay on the development computer. The serving machine runs the HTTPS proxy, the API, and PostgreSQL.
- **Why:** The serving-only note keeps batch work off the small host.
- **Verify:** The host process list shows those three serving processes and does not show dbt, Silo, or Airflow.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

## D. Independent encrypted offsite backup and tested recovery

### D1. An encrypted copy lives off both disks

- **Complete:** An encrypted copy of the raw archive and of the signed snapshot exists somewhere that is neither this development computer nor the serving-machine disk.
- **Why:** ADR-004 and the serving note say losing the only disk means ingesting again. The on-cluster previous generation is the same machine.
- **Verify:** The copy's location is written down. Its size is at least the snapshot size. The development computer and the serving machine can both be powered off without deleting that copy.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Manual. Any storage price must fit inside the same USD 10 monthly total. No current price was checked.

### D2. That copy restores onto a clean database

- **Complete:** The offsite snapshot is restored under a new database name. The release id and the fact count match the manifest. `openfactors_dev` is not replaced.
- **Why:** A backup that has never been read back is not a backup.
- **Verify:** Record the release id, the row count, and the machine or database name used for the test. The local `restore-check` drill does not satisfy this item.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host or another clean machine

### D3. The recovery point objective is written

- **Complete:** The owner adopted a maximum offsite backup age of 24 hours. Freshness starts at the latest backup that was stored offsite and then verified. The scope is the raw archive and the signed published snapshot. A miss leaves a visible log line and a failed backup record. This is an RPO. A hosted recovery time is still unset. The local drill still prints null `rpo_target_seconds` and `rto_target_seconds`. The statement is in ADR-004.
- **Why:** The operator needs a freshness rule before an offsite copy exists, and a local drill time is a different measurement.
- **Verify:** ADR-004 states 24 hours, the verification rule, the file scope, and the late-backup response.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Manual decision, recorded on 2026-10-29

### D4. A verified offsite backup is at most 24 hours old

- **Complete:** The latest successful verified offsite copy of the raw archive and the signed snapshot is no older than 24 hours. A missing or older copy leaves the failed backup record from ADR-004.
- **Why:** The RPO in D3 is a policy. A copy that has not been verified, or that is older than 24 hours, misses it.
- **Verify:** Record the verification time in UTC, the checksum of the raw archive, and the release id from the scratch restore in D2. The age at that check is at most 24 hours.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Manual, after D1 and D2. The local `restore-check` drill does not satisfy this item.

## E. Monitoring, logging, and alerting

### E1. The operator can see a public outage

- **Complete:** The owner has one way to learn that the public HTTPS API stopped answering. A daily manual check, or a monitor that stays inside the USD 10 total, is enough. A paging service, SMS, and Grafana are not required.
- **Why:** ADR-003 has no 24/7 rotation. A public URL still needs a person who can notice it is down.
- **Verify:** Stop the API on purpose, confirm the chosen check reports the failure, then start it again. Do this before the address is announced.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Manual, on the host

### E2. Logs exist and do not contain passwords

- **Complete:** The proxy and the API write a failure line. Password files and connection strings are not in those logs.
- **Why:** The local rule is a visible failed run. A public host needs the same evidence without leaking `of_api`.
- **Verify:** Cause one failed request and find the line. Search the log for the database password and find no copy.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

### E3. Dashboards and paging

- **Complete:** Grafana, email, or SMS paging.
- **Why:** Useful later. ADR-003 leaves them out of the current alert budget.
- **Verify:** Not part of this beta.
- **Result:** NOT TESTED
- **Blocks launch:** No
- **Where:** Later

## F. Security and access controls

### F1. The serving database role is read-only

- **Complete:** On the serving database, `of_api` can read the published views and cannot read `control`, `staging`, `core`, or `quality`, and cannot insert, update, or delete.
- **Why:** The public process uses that role.
- **Verify:** Repeat the role checks from `docs/decisions/security-checklist.md` against the serving database.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host. The same checks PASS on the local test database.

### F2. The API verifies the database certificate

- **Complete:** `OPENFACTORS_API_PGHOST` is set, `OPENFACTORS_API_PGSSLMODE` is `verify-full`, and `OPENFACTORS_API_PGSSLROOTCERT` points at the CA that signed the server certificate.
- **Why:** Local `sslmode=require` encrypts the session and does not verify the certificate. The serving layout does not use that mode.
- **Verify:** The API answers a catalog request with those variables set. A wrong CA file makes the API refuse the database.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

### F3. Host secrets stay off git and off other accounts

- **Complete:** The password directory is mode `0700` and owned by the API user. The pin file is mode `0440` and owned by root. Neither file is committed. The host check also covers the ACL policy, the restricted service identities, and who can read or replace the pin and the accepted snapshots.
- **Why:** ADR-010 accepts that mode bits miss some ACLs. The host still has to be examined.
- **Verify:** `python tools/scan_secrets.py` stays clean. On the host, record `stat` and `getfacl` for the pin, the accepted directory, and the private restore directory, plus the identities that can write them.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host. The repository secret scan PASS is separate and already true.

### F4. Public writes stay closed

- **Complete:** POST, PUT, PATCH, and DELETE on the public paths return 405.
- **Why:** The beta is read-only.
- **Verify:** The security checklist tests, and one request against the public URL during H2.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local. The public request is part of H2.

### F5. Known trust limits are accepted in writing

- **Complete:** The owner conditionally accepts the restore trust limits in ADR-010. Root and authorized PostgreSQL administrators are trusted privileged identities. Staging promotion relies on the catalog comment `openfactors-authenticated` and does not read the signature again. POSIX mode checks do not detect every ACL or extra capability. F1, F2, and F3 remain the host verification.
- **Why:** The beta can name its trust boundary while the serving host is still unexamined.
- **Verify:** ADR-010 contains the three limits and says the host checks are not done. F1, F2, and F3 stay NOT TESTED.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Manual decision, recorded on 2026-10-29. This PASS is the policy only.

## G. Data licensing and attribution

### G1. The public beta stays non-commercial

- **Complete:** The public site and the host account do not sell subscriptions and do not run advertising. No new Open-Meteo calls are made for a paid product.
- **Why:** The source registry binds the free weather API to that rule. This checklist does not treat commercial redistribution as already granted.
- **Verify:** Read the live pages and the host plan. There is no price, no ad slot, and no paid API key in use.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Manual, on the host

### G2. Attribution is already in the local product

- **Complete:** Weather credit is `Weather data by Open-Meteo.com` with links to the provider and to CC BY 4.0, plus a statement that the values were adapted. ERA5 is cited. Holiday rows cite the Official Gazette proclamation.
- **Why:** The source registry requires those words on the public dataset.
- **Verify:** The dataset dictionary, catalog page, and export manifest in the local API.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local. Seeing them on the public URL is part of H2.

### G3. A later commercial product is a new decision

- **Complete:** Paid or ad-supported use of new weather calls waits for the Open-Meteo paid API and a new decision.
- **Why:** The free tier does not cover that step.
- **Verify:** This paragraph and `docs/decisions/source-registry.md`.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local

## H. Public API documentation and user testing

### H1. The public URL serves the guide

- **Complete:** `/`, `/docs`, the dataset page, and `/openapi.json` answer on the HTTPS name.
- **Why:** A beta caller needs the same guide the local server already has.
- **Verify:** Open each path in a browser over HTTPS.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Host

### H2. Someone other than the author completes one task

- **Complete:** That person, without a local checkout, requests one city and a short date range, downloads CSV or Parquet, and can point at the weather and holiday attribution.
- **Why:** The author already knows the local paths. A beta fails if only the author can use it.
- **Verify:** Save the request URL, the release id, and whether the attribution was visible. Also send one POST and confirm 405.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Manual, against the host

## I. Budget and ongoing operating costs

### I1. Current recurring spend

- **Complete:** Recurring hosting spend is USD 0.
- **Why:** The ceiling in I2 is a maximum. No invoice has been opened for this project.
- **Verify:** No VPS, domain, or database invoice exists for this project.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Manual statement from the owner. No billing page was opened for this file.

### I2. The recurring ceiling is USD 10 per month

- **Complete:** ADR-003 adopts a maximum of USD 10 per month for VPS hosting, independent offsite backup storage, monitoring, and any other required recurring service. The 2026-10-24 cap of USD 0 is kept there as superseded history. Current recurring spend remains USD 0. Any purchase needs a separate explicit approval, after the current price, tax, renewal price, payment terms, and overage are read. Historical VPS prices are not current quotes.
- **Why:** The operator needs a ceiling before comparing a host, and a ceiling is a different act from paying.
- **Verify:** ADR-003 states USD 10, USD 0 current spend, the included services, and the separate purchase approval.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Manual decision, recorded on 2026-10-29

### I3. The chosen bill is read on the day of purchase

- **Complete:** The order page shows a recurring total at or under USD 10 per month, including any tax the page shows, and the plan cannot add an uncapped overage. Renewal length is visible.
- **Why:** ADR-006 recorded provider pages on 2026-10-26 and rejected both zero-cost configurations. `docs/decisions/serving-only-vps.md` recorded an OVHcloud VPS-1 display price from USD 4.54 per month on 2026-10-27. Those figures are not today's price, and tax and contract length were not on a completed order.
- **Verify:** Save the order page, the date, the monthly total, and the renewal term. Stop before paying if the total is above USD 10 or if overage is uncapped.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Manual

### I4. CI stays on the free GitHub runner

- **Complete:** The `ci` workflow uses GitHub-hosted runners and does not deploy.
- **Why:** A paid runner would be another bill.
- **Verify:** `.github/workflows/ci.yml` has no self-hosted or larger paid runner label.
- **Result:** PASS
- **Blocks launch:** No
- **Where:** Local

## J. Final GO/NO-GO approval

### J1. The owner signs go only after every blocker passes

- **Complete:** Every item above that blocks launch is PASS. The owner writes the date, the public URL, and the word GO. Until that sentence exists, the decision remains NO-GO. `python -m openfactors.orchestration.release` does not deploy and does not create that sentence.
- **Why:** A checklist file is not a launch.
- **Verify:** Read this file. Every **Blocks launch: Yes** row shows PASS. The GO sentence is present.
- **Result:** NOT TESTED
- **Blocks launch:** Yes
- **Where:** Manual

## Count

| | |
|---|---|
| Blocks launch, not yet PASS | 19 |
| Already PASS | 15 |
| Optional after the beta | 2 |

The 19 blockers are A4, B1, B2, C2, C3, C4, D1, D2, D4, E1, E2, F1, F2, F3, G1, H1, H2, I3, and J1. B3 and E3 do not block.

Already PASS: A1, A2, A3, A5, A6, A7, C1, D3, F4, F5, G2, G3, I1, I2, I4.

A5, D3, F5, and I2 are PASS for the written decision. A7 is PASS for the local span check. D1, D2, D4, F1, F2, and F3 stay open. I3 stays NOT TESTED. Public Beta v0.1 remains NO-GO.
