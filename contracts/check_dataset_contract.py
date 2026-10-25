"""OF-009 checks the 12 public fields and the published-row shapes."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from contracts.calendar import manila_date

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "fixtures" / "daily_factor_contract.json"
CASES_PATH = ROOT / "contracts" / "fixtures" / "daily_factor_cases.json"
PROBE_PATH = ROOT / "contracts" / "fixtures" / "open_meteo_era5_2018-01-01.json"
LOCATIONS_PATH = ROOT / "contracts" / "fixtures" / "pilot_locations.json"
LLD_PATH = ROOT / "C_OpenFactors_LLD_Data_Models_Contracts_Notion_v1.md"
API_PATH = ROOT / "D_OpenFactors_API_Specification_Notion_v1.md"
OPENAPI_PATH = ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"

WEATHER_SOURCE = "open-meteo-era5"
HOLIDAY_SOURCE = "ph-official-gazette"
NUMERIC_SCALE = {
    "temperature_mean_c": (7, 3),
    "precipitation_mm": (10, 3),
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def field_names(contract: dict) -> list[str]:
    fields = contract["fields"]
    positions = [field["position"] for field in fields]
    if positions != list(range(1, len(fields) + 1)):
        raise ValueError("Field positions must be 1..n in order.")
    return [field["name"] for field in fields]


def _problems(contract: dict, case: dict, locations: set[str]) -> list[str]:
    problems: list[str] = []
    if case.get("calendar_status") == "incomplete":
        problems.append("incomplete_year_published")
    row = case["row"]
    names = field_names(contract)
    if list(row) != names:
        problems.append("field_order")
        return problems
    local_date = _date_problem(case, row, problems)
    _location_problem(row, locations, problems)
    _decimal_problem(row, "temperature_mean_c", problems)
    _decimal_problem(row, "precipitation_mm", problems)
    if row["precipitation_mm"] is not None and row["precipitation_mm"] < 0:
        problems.append("negative_precipitation")
    if "source_precipitation_mm" in case and case["source_precipitation_mm"] is None:
        if row["precipitation_mm"] == 0:
            problems.append("missing_is_not_zero")
    if case.get("source_temperature_c") is None and "source_temperature_c" in case:
        if row["temperature_mean_c"] == 0:
            problems.append("missing_is_not_zero")
    _holiday_problem(case, row, problems)
    if local_date is not None and row["is_weekend"] is not (local_date.isoweekday() in (6, 7)):
        problems.append("weekend_mismatch")
    if row["weather_source_id"] != WEATHER_SOURCE:
        problems.append("weather_source")
    if row["holiday_source_id"] != HOLIDAY_SOURCE:
        problems.append("holiday_source")
    if row["dataset_version"] != contract["dataset_version"]:
        problems.append("dataset_version")
    retrieved_at = case.get("retrieved_at")
    if retrieved_at is not None and row["source_updated_at"] == retrieved_at:
        problems.append("retrieval_time_substituted")
    _probe_problem(case, row, problems)
    return problems


def _date_problem(case: dict, row: dict, problems: list[str]) -> date | None:
    try:
        local_date = date.fromisoformat(row["date"])
    except (TypeError, ValueError):
        problems.append("date")
        return None
    instant = case.get("instant")
    if instant is not None and local_date != manila_date(instant):
        problems.append("manila_date_mismatch")
    return local_date


def _location_problem(row: dict, locations: set[str], problems: list[str]) -> None:
    if row["country_code"] != "PH":
        problems.append("country_code")
    if row["location_id"] not in locations:
        problems.append("unknown_location")


def _decimal_problem(row: dict, name: str, problems: list[str]) -> None:
    value = row[name]
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Decimal):
        problems.append("decimal_type")
        return
    precision, scale = NUMERIC_SCALE[name]
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -scale:
        problems.append("decimal_scale")
    if value != 0 and max(value.adjusted() + 1, 0) > precision - scale:
        problems.append("decimal_precision")


def _holiday_problem(case: dict, row: dict, problems: list[str]) -> None:
    flag = row["is_public_holiday"]
    name = row["holiday_name"]
    if not isinstance(flag, bool):
        problems.append("holiday_flag_null")
        return
    if flag and case.get("not_a_proclamation") is not True:
        problems.append("holiday_not_a_proclamation")
    if flag:
        if not isinstance(name, str) or not name.strip():
            problems.append("holiday_name_required")
            return
        parts = name.split(" | ")
        if name != " | ".join(parts) or any(not part.strip() for part in parts):
            problems.append("holiday_name_required")
        elif parts != sorted(parts):
            problems.append("holiday_names_unsorted")
    elif name is not None:
        problems.append("holiday_name_forbidden")


def _probe_problem(case: dict, row: dict, problems: list[str]) -> None:
    location_id = case.get("probe_location_id")
    if location_id is None:
        return
    probe = load_json(PROBE_PATH)
    matches = [
        item
        for item in probe["rows"]
        if item["location_id"] == location_id and item["date"] == row["date"]
    ]
    if len(matches) != 1:
        problems.append("probe_row")
        return
    source = matches[0]
    if row["temperature_mean_c"] != source["temperature_2m_mean_c"]:
        problems.append("probe_temperature")
    if row["precipitation_mm"] != source["precipitation_sum_mm"]:
        problems.append("probe_precipitation")
    if row["precipitation_mm"] == 0 and source["precipitation_sum_mm"] != 0:
        problems.append("zero_is_not_missing")


def _api_fields(text: str) -> list[tuple[str, str, bool]]:
    start = text.index("| Position | Field | JSON type | Null allowed |")
    found: list[tuple[str, str, bool]] = []
    for line in text[start:].splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells[0].isdigit():
            continue
        found.append((cells[1].strip("`"), cells[2], cells[3] == "Yes"))
    return found


def _lld_fields(text: str) -> list[tuple[str, str, bool]]:
    start = text.index("| Exact PRD column | PostgreSQL type | Null? |")
    found: list[tuple[str, str, bool]] = []
    for line in text[start:].splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        found.append((cells[0].strip("`"), cells[1].strip("`"), cells[2] == "Yes"))
    return found


def _openapi_required(text: str) -> list[str]:
    start = text.index("    DailyFactor:\n")
    block = text[start:]
    required_at = block.index("      required:\n")
    properties_at = block.index("      properties:\n")
    names: list[str] = []
    for line in block[required_at:properties_at].splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith("- "):
            names.append(stripped[2:].strip())
    return names


def _openapi_nullable(text: str) -> dict[str, bool]:
    start = text.index("    DailyFactor:\n")
    properties_at = text.index("      properties:\n", start)
    schema_end = text.index("\n    Error:\n", properties_at)
    body = text[properties_at:schema_end]
    nullable: dict[str, bool] = {}
    current: str | None = None
    chunk: list[str] = []
    for line in body.splitlines()[1:]:
        if line.startswith("        ") and line.endswith(":") and not line.startswith("          "):
            if current is not None:
                nullable[current] = "type: 'null'" in "\n".join(chunk)
            current = line.strip()[:-1]
            chunk = []
        else:
            chunk.append(line)
    if current is not None:
        nullable[current] = "type: 'null'" in "\n".join(chunk)
    return nullable


def _publish_columns(text: str) -> list[tuple[str, str, bool]]:
    start = text.index("CREATE TABLE publish.daily_external_factors_ph (")
    end = text.index(");", start)
    columns: list[tuple[str, str, bool]] = []
    for line in text[start:end].splitlines()[1:]:
        stripped = line.strip().rstrip(",")
        if not stripped[:1].islower():
            continue
        name, rest = stripped.split(None, 1)
        type_name = rest.split()[0]
        nullable = "NOT NULL" not in rest.split(" CHECK ")[0]
        columns.append((name, type_name, nullable))
    return columns


def cross_layer_problems(contract: dict) -> list[str]:
    problems: list[str] = []
    expected = [
        (field["name"], field["postgres"], field["json_type"], field["nullable"])
        for field in contract["fields"]
    ]
    names = [item[0] for item in expected]
    api = _api_fields(API_PATH.read_text(encoding="utf-8"))
    if [(name, json_type, nullable) for name, _, json_type, nullable in expected] != [
        (name, json_type, nullable) for name, json_type, nullable in api
    ]:
        problems.append("api_spec_fields")
    lld = _lld_fields(LLD_PATH.read_text(encoding="utf-8"))
    if [(name, postgres, nullable) for name, postgres, _, nullable in expected] != lld:
        problems.append("lld_fields")
    openapi_text = OPENAPI_PATH.read_text(encoding="utf-8")
    if _openapi_required(openapi_text) != names:
        problems.append("openapi_required")
    nullable = _openapi_nullable(openapi_text)
    for name, _, _, is_nullable in expected:
        if nullable.get(name) is not is_nullable:
            problems.append(f"openapi_null:{name}")
    publish = _publish_columns(LLD_PATH.read_text(encoding="utf-8"))
    publish_names = [name for name, _, _ in publish]
    if publish_names[0] != "release_id" or publish_names[-1] != "weather_data_status":
        problems.append("publish_internal_columns")
    public = publish[1:-1]
    expected_public = [(name, postgres, is_nullable) for name, postgres, _, is_nullable in expected]
    if public != expected_public:
        problems.append("publish_columns")
    return problems


def approved_locations() -> set[str]:
    document = load_json(LOCATIONS_PATH)
    return {
        location["location_id"]
        for location in document["locations"]
        if location["approved_for_mvp"] is True
    }


def collect_problems() -> list[str]:
    contract = load_json(CONTRACT_PATH)
    cases = load_json(CASES_PATH)
    locations = approved_locations()
    problems = [f"contract:{item}" for item in cross_layer_problems(contract)]
    if len(contract["fields"]) != 12:
        problems.append("contract:field_count")
    for case in cases["valid"]:
        found = _problems(contract, case, locations)
        if found:
            problems.append(f"{case['case_id']}:{','.join(found)}")
    for case in cases["omitted"]:
        if (
            case.get("calendar_status") != "incomplete"
            or case.get("included_in_release") is not False
        ):
            problems.append(f"{case['case_id']}:incomplete_year_not_omitted")
        if "row" in case:
            problems.append(f"{case['case_id']}:incomplete_year_published")
    for case in cases["rejected"]:
        found = _problems(contract, case, locations)
        if case["expect"] not in found:
            problems.append(f"{case['case_id']}:missing {case['expect']}")
    required_cases = {
        "measured_rain",
        "measured_zero",
        "missing_weather",
        "manila_saturday",
        "manila_local_date",
        "one_holiday_name",
        "two_holiday_names",
        "incomplete_year_omitted",
    }
    present = {case["case_id"] for case in cases["valid"]} | {
        case["case_id"] for case in cases["omitted"]
    }
    missing = required_cases - present
    if missing:
        problems.append("missing_cases:" + ",".join(sorted(missing)))
    return problems


def main() -> None:
    problems = collect_problems()
    if problems:
        for problem in problems:
            print(problem)
        raise SystemExit(1)
    print("OF-009 dataset contract ok")


if __name__ == "__main__":
    main()
