# Load, coverage, and reliability

The report is one local command: `python -m openfactors.orchestration.measure`. It is not an HTTP route. Prometheus and Grafana are not installed.

The timed request is the factors query. One reader, one location, a 31-day window, and page size 100. That shape is the local assumption. The page is cached in the process, the measures are null, and the sample release is not published. Exports are not in the sample. The command records the sample count, one discarded warmup call, and the p95. It does not claim the PRD target of one second, because no hosted profile is signed off. No confidence method, date-span maximum, or request quota is set.

Coverage uses the publication rule: present rows must reach 99 percent of the approved location-dates after exclusions. Expected, present, excluded, and missing are separate counts. A present row with null weather stays in the coverage count and is counted again as weather absent. This command starts with no published rows, so the threshold is not met. The publication gate still blocks a candidate that fails the same rule.

Reliability stays a plan. The target is 99 percent of daily runs over a rolling 30 days. Dry-runs and backfills are not scheduled runs. A run with no usable clock is not a success. The owner is Founder / Product Lead. The rollback threshold is unset. A sample on this computer is not a 30-day evaluation, so the target is not claimed.
