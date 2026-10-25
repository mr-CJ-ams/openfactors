"""Local load, coverage, and reliability report. The PRD targets stay unclaimed."""

import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from openfactors.ingestion.errors import ConnectorError
from openfactors.ingestion.runs import RunRecord
from openfactors.orchestration.measure import (
    LOCAL_SAMPLES,
    LOCAL_WARMUP,
    build_measurement,
    main,
    sample_durations,
)

NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
LOCATION = "ph-manila"
DAY = "2018-01-01"


def test_percentile_and_empty_sample() -> None:
    report = _report([0.1] * 18 + [0.2, 0.9], rows=[_row()], supported={(LOCATION, DAY)})
    assert report["latency"]["p95_seconds"] == 0.2
    assert report["latency"]["prd_target_claimed"] is False
    assert report["latency"]["hosted_profile_signed_off"] is False
    assert report["latency"]["readers"] == 1
    assert report["latency"]["window_days"] == 31
    assert report["latency"]["page_size"] == 100
    assert report["latency"]["date_span_maximum"] is None
    assert report["latency"]["request_quota"] is None
    assert report["latency"]["exports_included"] is False
    assert report["latency"]["confidence"] is None
    with pytest.raises(ConnectorError, match="A latency sample is required."):
        _report([], rows=[], supported=set())
    with pytest.raises(ConnectorError, match="zero or greater"):
        _report([-0.1], rows=[], supported=set())


def test_coverage_counts_exclusions_and_missing_rows_separately() -> None:
    second = "2018-01-02"
    passed = _report(
        [0.1],
        rows=[_row()],
        supported={(LOCATION, DAY), (LOCATION, second)},
        exclusions=[
            {
                "location_id": LOCATION,
                "date": second,
                "public": True,
                "reason": "documented upstream gap",
            }
        ],
    )
    assert passed["coverage"]["expected"] == 1
    assert passed["coverage"]["present"] == 1
    assert passed["coverage"]["excluded"] == 1
    assert passed["coverage"]["missing"] == 0
    assert passed["coverage"]["meets_threshold"] is True
    assert passed["coverage"]["weather_absent"] == 1

    short = _report(
        [0.1],
        rows=[_row()],
        supported={(LOCATION, DAY), (LOCATION, second)},
    )
    assert short["coverage"]["expected"] == 2
    assert short["coverage"]["missing"] == 1
    assert short["coverage"]["excluded"] == 0
    assert short["coverage"]["meets_threshold"] is False

    empty = _report([0.1], rows=[], supported=set())
    assert empty["coverage"]["meets_threshold"] is False
    wet = _report(
        [0.1],
        rows=[{**_row(), "precipitation_mm": -1}],
        supported={(LOCATION, DAY)},
    )
    assert wet["coverage"]["meets_threshold"] is True
    assert wet["coverage"]["other_violations"] == ["negative_precipitation"]


def test_reliability_plan_does_not_claim_the_thirty_day_target() -> None:
    runs = [
        _run("daily", "succeeded", NOW - timedelta(days=1)),
        _run("daily", "succeeded", NOW - timedelta(days=2)),
        _run("daily", "failed", NOW - timedelta(days=3)),
        _run("backfill", "succeeded", NOW - timedelta(days=1)),
        _run("backfill-dry-run", "succeeded", NOW - timedelta(days=1)),
        _run("daily", "succeeded", NOW - timedelta(days=31)),
        _run("daily", "succeeded", None),
    ]
    report = _report([0.1], rows=[_row()], supported={(LOCATION, DAY)}, runs=runs)
    reliability = report["reliability"]
    assert reliability["observed_attempts"] == 3
    assert reliability["observed_successes"] == 2
    assert reliability["unplaced_runs"] == 1
    assert reliability["sample_meets_threshold"] is False
    assert reliability["prelaunch_sufficient"] is False
    assert reliability["slo_claimed"] is False
    assert reliability["window_days"] == 30
    assert reliability["rollback_threshold"] is None
    assert reliability["missing_telemetry_counts_as_success"] is False
    assert reliability["owner"] == "Founder / Product Lead"

    enough = [
        _run("daily", "succeeded", NOW - timedelta(days=day)) for day in range(1, 31)
    ]
    claimed = _report([0.1], rows=[_row()], supported={(LOCATION, DAY)}, runs=enough)
    assert claimed["reliability"]["sample_meets_threshold"] is True
    assert claimed["reliability"]["slo_claimed"] is False


def test_cached_query_sample_uses_the_local_shape() -> None:
    durations, rows_returned = sample_durations(NOW)
    assert len(durations) == LOCAL_SAMPLES
    assert rows_returned == 31
    assert all(sample >= 0 for sample in durations)
    with pytest.raises(ConnectorError, match="timezone-aware"):
        sample_durations(datetime(2026, 10, 25))
    with pytest.raises(ConnectorError, match="That location is not available."):
        _report([0.1], rows=[], supported=set(), location_id="PH-EXAMPLE")


def test_measure_command_prints_an_unclaimed_report() -> None:
    out = io.StringIO()
    assert main([], out) == 0
    report = json.loads(out.getvalue())
    assert report["latency"]["sample_count"] == LOCAL_SAMPLES
    assert report["latency"]["warmup_count"] == LOCAL_WARMUP
    assert report["latency"]["prd_target_claimed"] is False
    assert report["latency"]["measures_are_null"] is True
    assert report["latency"]["sample_release_published"] is False
    assert report["coverage"]["present"] == 0
    assert report["coverage"]["meets_threshold"] is False
    assert report["coverage"]["missing"] == report["coverage"]["expected"]
    assert report["reliability"]["observed_attempts"] == 0
    assert report["reliability"]["slo_claimed"] is False
    with pytest.raises(SystemExit, match="The measure command does not take arguments."):
        main(["--now"])


def _report(
    durations: list[float],
    *,
    rows: list[dict],
    supported: set[tuple[str, str]],
    exclusions: list[dict] | None = None,
    runs: list[RunRecord] | None = None,
    location_id: str = LOCATION,
) -> dict:
    return build_measurement(
        durations,
        runs or [],
        now=NOW,
        rows=rows,
        supported=supported,
        exclusions=exclusions or [],
        verified_dates={DAY: {"is_public_holiday": False, "holiday_name": None}},
        warmup_count=LOCAL_WARMUP,
        location_id=location_id,
        rows_returned=31,
    )


def _row() -> dict:
    return {
        "location_id": LOCATION,
        "date": DAY,
        "is_public_holiday": False,
        "holiday_name": None,
        "is_weekend": False,
        "precipitation_mm": None,
        "temperature_mean_c": None,
    }


def _run(stream: str, status: str, started: datetime | None) -> RunRecord:
    return RunRecord(
        run_id=f"run-{stream}-{status}-{started}",
        source_id="open-meteo-era5",
        stream=stream,
        location_id=LOCATION,
        interval_start=date(2018, 1, 1),
        interval_end=date(2018, 1, 31),
        code_revision="local",
        status=status,
        started_at=started,
        finished_at=started,
    )
