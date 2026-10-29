# ADR-003 — Budget, access, and scheduler

| Field | Value |
|---|---|
| Decisions | D-05 budget and ownership, D-04 anonymous access, D-07 scheduler |
| Ticket | OF-003 |
| Status | Cap revised on 2026-10-29. The 2026-10-24 baseline is kept below. |
| Public hosting | Not approved |

## Budget and owner

### Ceiling adopted on 2026-10-29

| Item | Decision |
|---|---|
| Monthly recurring ceiling | USD 10 |
| What the ceiling includes | VPS hosting, independent offsite backup storage, monitoring, and any other required recurring service |
| Current recurring spend | USD 0 |
| Purchase | A separate explicit approval is required before any purchase, account, or payment. Before a purchase is proposed, verify the current price, tax, renewal price, payment terms, and any overage. A price copied from an older page is not a current quote. |
| Who pays | The repository owner, only after that separate approval. |
| Operator | Founder / Product Lead, the person who owns this repository. No personal legal name is recorded here. |
| On-call | That same person, when they are at the machine. There is no 24/7 rotation and no paging service. |
| Alert | A failed local run must leave a visible log line and a failed run record. No email or SMS alert is in the current spend. A later monitor has to fit inside the USD 10 ceiling. |
| Environments | Local Docker Compose is the only running environment. dev, test, and staging are separate projects on this computer (`ops/local_stack.py`). Hosted staging and production are not provisioned. |
| Load assumption | One local reader, one `location_id`, a 31-day window, `page_size` 100. OF-042 records that sample on this computer and does not claim the PRD p95 target. A hosted profile is still not signed off. |

The ceiling is a maximum. It does not select a host and it does not start a public URL. Public Beta v0.1 stays no-go until the launch checklist is signed.

GitHub Actions stays on the free hosted runner. A paid runner has to fit inside the same USD 10 ceiling and needs the same separate purchase approval.

### Cap adopted on 2026-10-24, superseded

| Item | Decision then |
|---|---|
| Monthly cost cap | USD 0 |
| Who pays | Nobody. No cloud bill was authorized. |
| Environments | Local Docker Compose only. |

That USD 0 cap is no longer the ceiling. It remains the record of what was adopted on 2026-10-24, and it remains the actual spend until a later purchase is approved.

OF-006, the repository skeleton, started on the 2026-10-24 baseline. Ingestion of real source data still waited on the source registry and on OF-008 at that time.

## D-04 access

Anonymous read-only access. No accounts and no API keys for the public MVP. Rate-limit numbers are not set here. They stay with the later limits decision. There is no public write route.

## D-07 scheduler

The MVP scheduler is a local scheduled command or a manual command with the same arguments. Airflow is not that scheduler. ADR-007 adds a separate local learning project that runs the same daily command. It is not installed in the dev, test, or staging projects, and it is not a production migration.

## Acceptance

| Role | Result |
|---|---|
| Founder / Product Lead | USD 0 cap adopted by instruction on 2026-10-24. USD 10 recurring ceiling adopted by instruction on 2026-10-29. Current recurring spend remains USD 0. |
| Production deploy | Still refused. The ceiling is a spending maximum. A purchase needs a separate approval, and public hosting remains unapproved. |
