# Public beta

The public beta gate is one local command: `python -m openfactors.orchestration.release`. It is not an HTTP route. It does not deploy.

The release checklist is no-go, so the staged rollout is not started. There is no release tag and no public address. Feedback telemetry is not started. The post-deploy smoke is not run.

Rollback stays the local procedure in `docs/decisions/recovery-drill.md`. That procedure is not a public rollback. The adopted recurring ceiling is USD 10 per month in ADR-003. Current recurring spend remains USD 0. That ceiling does not start hosting. The operator remains Founder / Product Lead. A failed local run leaves a log line and a failed run record. There is no paging service, no email alert, and Grafana is not installed. The operations report remains `python -m openfactors.orchestration.status`.

A checklist that says go still does not make this command deploy. Public hosting is not a local action.
