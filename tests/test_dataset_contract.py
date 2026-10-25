from contracts.check_dataset_contract import (
    CONTRACT_PATH,
    collect_problems,
    field_names,
    load_json,
    manila_date,
)


def test_contract_has_twelve_fields_in_order() -> None:
    contract = load_json(CONTRACT_PATH)
    assert field_names(contract) == [
        "date",
        "location_id",
        "country_code",
        "temperature_mean_c",
        "precipitation_mm",
        "is_public_holiday",
        "holiday_name",
        "is_weekend",
        "weather_source_id",
        "holiday_source_id",
        "source_updated_at",
        "dataset_version",
    ]


def test_manila_date_is_eight_hours_ahead_of_utc() -> None:
    assert manila_date("2018-01-01T16:30:00Z").isoformat() == "2018-01-02"
    assert manila_date("2018-01-01T15:59:59Z").isoformat() == "2018-01-01"


def test_dataset_contract_matches_the_documents_and_cases() -> None:
    assert collect_problems() == []
