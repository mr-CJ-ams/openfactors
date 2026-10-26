"""Fail the CI run when a required database test does not pass."""

from __future__ import annotations

import os

import pytest

_ALLOWED_SKIP = "test_live_2018_01_01_matches_recorded_probe"
_REQUIRED = (
    "test_api_read_cannot_read_raw_or_admin_tables",
    "test_api_read_matches_the_compose_catalog_row",
    "test_api_read_verify_full_accepts_the_local_ca",
    "test_api_read_verify_full_rejects_an_untrusted_file",
    "test_api_role_can_read_only_the_serving_view",
    "test_candidate_schema_snapshot",
    "test_current_spine_reports_coverage_and_publish_stays_empty",
    "test_dev_database_enforces_dimensions_revisions_and_sources",
    "test_dev_database_holds_locations_and_the_manila_spine",
    "test_dev_database_persists_a_run_and_rejects_manifest_changes",
    "test_example_command_joins_the_active_local_release",
    "test_isolated_restore_and_release_rollback",
    "test_live_candidate_passes_and_published_rows_stay_put",
    "test_live_catalog_lists_only_the_active_published_release",
    "test_live_catalog_page_matches_the_published_list",
    "test_live_csv_uses_the_pinned_release",
    "test_live_dictionary_page_lists_the_public_fields",
    "test_live_manifest_is_immutable_and_publish_stays_empty",
    "test_live_parquet_matches_the_pinned_release",
    "test_live_pin_stays_stable_when_a_newer_release_is_active",
    "test_live_pin_survives_a_newer_release_and_fails_after_withdrawal",
    "test_local_catalog_serves_the_published_manila_day",
    "test_one_weather_revision_and_one_holiday_broadcast",
    "test_pointer_switch_failure_and_rollback_keep_a_whole_release",
    "test_public_role_cannot_read_raw_data_or_connect_without_tls",
    "test_sql_join_runs_on_the_pinned_release",
    "test_staging_models_parse_units_and_rebuild_the_same_rows",
    "test_suspended_source_withdraws_releases_and_purges_exports",
    "test_withdrawal_does_not_activate_a_release_with_missing_rows",
)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.environ.get("OPENFACTORS_REQUIRE_DATABASE") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        session.exitstatus = 1
        return
    skipped = [
        report.nodeid
        for report in reporter.stats.get("skipped", [])
        if _ALLOWED_SKIP not in report.nodeid
    ]
    passed = {report.nodeid for report in reporter.stats.get("passed", [])}
    missing = [name for name in _REQUIRED if not any(name in nodeid for nodeid in passed)]
    if not skipped and not missing:
        return
    reporter.write_sep("!", "Required database tests did not all pass")
    for nodeid in skipped:
        reporter.write_line(f"skipped: {nodeid}")
    for name in missing:
        reporter.write_line(f"not passed: {name}")
    if exitstatus == 0:
        session.exitstatus = 1
