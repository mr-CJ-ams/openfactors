# ADR-010 — Conditional acceptance of restore trust limits

| Field | Value |
|---|---|
| Decision | Policy acceptance of the documented restore trust limits for a restricted Public Beta |
| Status | Adopted by the product owner on 2026-10-29. Production checks are not done. |
| Number | ADR-010 |
| Public release | No-go. This record does not deploy, and it does not mark a release go. |

The limits below are already described in `docs/decisions/snapshot-transfer-acceptance.md`. This record accepts them as the trust boundary for Public Beta v0.1. It does not record a passed security review of a serving host.

## Accepted boundary

Root and authorized PostgreSQL administrators are trusted privileged identities. An authorized administrator includes the role that can run catalog SQL on the serving cluster.

Staging promotion relies on the catalog comment `openfactors-authenticated`. It does not read the snapshot signature again. Rollback and cleanup do not read the accepted directory.

POSIX mode checks do not detect every ACL or extra capability. A default ACL or an access ACL can grant write that the mode bits hide.

## Still required before launch

On the serving host, before any public name is used, the operator verifies:

- ownership and mode of the signing pin, the accepted snapshot directory, and the private restore directory
- the ACL policy on those paths
- the restricted service identities for transfer, acceptance, restore, and the public API
- who can read or replace the pin and the accepted snapshot

Those checks are launch checklist items F1, F2, and F3. This decision leaves them NOT TESTED. A policy acceptance is a separate result from a host that has been examined.

## Acceptance

| Role | Result |
|---|---|
| Founder / Product Lead | Conditionally accepted by instruction on 2026-10-29 |
| Serving-host verification | Not done |
