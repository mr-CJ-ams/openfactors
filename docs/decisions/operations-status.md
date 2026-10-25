# Operations status

The operations report is one local command: `python -m openfactors.orchestration.status`. It is not an HTTP route. Prometheus and Grafana are not installed. No email or SMS is sent.

The report keeps three times apart. Last attempt is the latest run that started, including a failure and a dry-run. Last success is the latest finished run whose status is succeeded, and a dry-run does not count. Last publication is the publication time passed in from the catalog. A successful run does not fill that time, and a failed quality gate does not move it. When no publication time is present, the report says the publication is missing. It does not reuse the attempt time. No freshness threshold is set, so the report does not decide that a publication is too old.

Each run shows its state, the rows received, the rows accepted, and the duration when both clocks are present. Quality failures are counted by gate name. API responses are counted by route and status class. A trace id, a query string, and a Retry-After value are not labels. Alerts in the report carry the run id, the step, and the category. The report is for this process. A new command starts with those times missing.
