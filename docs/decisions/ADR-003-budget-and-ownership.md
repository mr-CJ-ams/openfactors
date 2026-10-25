# ADR-003 — Budget, access, and scheduler

| Field | Value |
|---|---|
| Decisions | D-05 budget and ownership, D-04 anonymous access, D-07 scheduler |
| Ticket | OF-003 |
| Status | Working baseline, adopted 2026-10-24 |
| Public hosting | Not approved |

## Budget and owner

| Item | Decision |
|---|---|
| Monthly cost cap | USD 0 |
| Who pays | Nobody. No cloud bill is authorized. |
| Operator | Founder / Product Lead, the person who owns this repository. No personal legal name is recorded here. |
| On-call | That same person, when they are at the machine. There is no 24/7 rotation and no paging service. |
| Alert | A failed local run must leave a visible log line and a failed run record. No email or SMS alert is in budget. |
| Environments | Local Docker Compose only. dev, test, and staging are separate projects on this computer (`ops/local_stack.py`). Hosted staging and production are not provisioned. |
| Load assumption | One local reader, one `location_id`, a 31-day window, `page_size` 100. OF-042 records that sample on this computer and does not claim the PRD p95 target. A hosted profile is still not signed off. |

GitHub Actions may be used later only while it stays inside this USD 0 cap. A paid runner, a hosted database, or a public URL is a new ADR that raises the cap first.

OF-006, the repository skeleton, may start on this baseline. Ingestion of real source data still waits on the source registry and on OF-008.

## D-04 access

Anonymous read-only access. No accounts and no API keys for the public MVP. Rate-limit numbers are not set here. They stay with the later limits decision. There is no public write route.

## D-07 scheduler

The MVP scheduler is a local scheduled command or a manual command with the same arguments. Airflow is not installed. Add it only if a later ADR shows this command cannot meet the daily run.

## Acceptance

| Role | Result |
|---|---|
| Founder / Product Lead | Adopted by instruction on 2026-10-24 |
| Production deploy | Still refused while the cap is USD 0 |
