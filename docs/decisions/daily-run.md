# Daily run

The daily refresh is one local command: `python -m openfactors.orchestration.daily`. Airflow is not installed. The command is not an HTTP route, and it does not take a date interval. A historical interval is `python -m openfactors.orchestration.backfill`.

One attempt covers the current Asia/Manila date. The steps run in this order: weather fetch, holiday fetch, transform, validate, publish. Publish does not run when an earlier step fails. A second attempt for a date that already succeeded does not run the steps again. A date that failed can be attempted again. Two attempts for the same process do not run at the same time.

Transient timeout, rate-limit, upstream HTTP, and network failures retry up to the connector attempt limit, which is 4. That number is the existing connector budget, not a request quota and not a date-span maximum. Config and schema failures are not retried. A failed attempt writes one log line with the run id, step, and category, plus a failed run record. The log line does not include the exception text. No email or SMS is sent.

Starting the command by itself does not fetch weather and does not publish. It records a failed attempt. A caller supplies the five steps. The scheduled-run test supplies them and checks order, retries, reruns, and the failure record.
