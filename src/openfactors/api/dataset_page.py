"""Dictionary and download page for the daily dataset.

The form uses labeled controls. Invalid filters stay on the page with the
same messages as the API. A download link is shown only after those checks
pass, and it carries the one release id printed on the page.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from openfactors.api.catalog import (
    DICTIONARY_PATH,
    CatalogUnavailable,
    _bad_escape,
    _error,
    _release_pin,
    _split_target,
)
from openfactors.api.csv_export import EXPORT_PATH
from openfactors.api.factors import _iso_date, _single
from openfactors.api.page_chrome import document

PAGE_PATH = "/datasets/daily-external-factors-ph"
_DATASET_ID = "daily_external_factors_ph"
_FIELDS = ("location_id", "start_date", "end_date", "format", "release_id")
_ROOT = Path(__file__).resolve().parents[3]
_LOCATIONS_PATH = _ROOT / "contracts" / "fixtures" / "pilot_locations.json"
_UNAVAILABLE = "The dictionary is temporarily unavailable."
_RANGE = "start_date must be on or before end_date"


def handle_dataset_page(
    method: str,
    target: str,
    *,
    load_release: Callable[[str | None], dict | None],
    dictionary: dict | None = None,
    locations: list[tuple[str, str]] | None = None,
    generated_at: str | None = None,
) -> tuple[int, dict[str, str], dict | bytes]:
    path, query = _split_target(target)
    if path != PAGE_PATH:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    parameters = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(parameters) - set(_FIELDS))
    if unknown:
        return (
            422,
            {},
            _error(
                "UNKNOWN_QUERY_PARAMETER",
                "Unknown query parameter.",
                {"parameter": unknown[0]},
            ),
        )
    try:
        document = dictionary if dictionary is not None else _json(DICTIONARY_PATH)
        choices = locations if locations is not None else _location_choices()
        submitted = any(name in parameters for name in _FIELDS)
        values = {name: _value(parameters.get(name)) for name in _FIELDS}
        errors: dict[str, str] = {}
        pinned: dict | None = None
        if submitted:
            errors = _errors(parameters, {item[0] for item in choices})
            if "release_id" not in errors:
                pin = _blank_pin(parameters.get("release_id"))
                release = load_release(pin)
                if release is None or release.get("dataset_id") != _DATASET_ID:
                    errors["release_id"] = "That release is not available."
                elif not errors:
                    pinned = release
                    values["release_id"] = release["release_id"]
        else:
            active = load_release(None)
            if active is not None and active.get("dataset_id") == _DATASET_ID:
                values["release_id"] = active["release_id"]
                pinned = None
        stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = render_dataset_page(
            document,
            choices,
            values,
            errors,
            pinned if submitted else None,
            stamp,
            active_id=values["release_id"] if not submitted else "",
        ).encode("utf-8")
        return 200, {"Content-Type": "text/html; charset=utf-8"}, body
    except CatalogUnavailable:
        body = render_unavailable().encode("utf-8")
        return 503, {"Content-Type": "text/html; charset=utf-8"}, body
    except Exception:
        return 500, {}, _error("INTERNAL", "The dictionary request failed.")


def render_dataset_page(
    dictionary: dict,
    locations: list[tuple[str, str]],
    values: dict[str, str],
    errors: dict[str, str],
    pinned: dict | None,
    generated_at: str,
    *,
    active_id: str,
) -> str:
    download = ""
    if pinned is not None and not errors:
        download = _download(values, pinned)
    active = ""
    if active_id and pinned is None:
        shown = _text(active_id)
        active = (
            f"<p>Active release {shown} is filled in. A download pins the release shown here.</p>"
        )
    generated = _text(generated_at)
    main = f"""<h2>Dictionary</h2>
<p>These 12 columns are the public file.
The field name is the column heading in a download.
The description is the everyday meaning.
A blank weather value was not available. A zero is a real zero.</p>
{_table(dictionary)}
<h2>Download</h2>
<p>Choose one city and the dates you need. You do not need an account.</p>
{active}
{_form(locations, values, errors)}
{download}
<p>Catalog response generated <time datetime="{generated}">{generated}</time>.</p>"""
    return _document(main)


def render_unavailable() -> str:
    return _document(f"<h2>Dictionary</h2>\n<p>{_UNAVAILABLE}</p>")


def _errors(parameters: dict, locations: set[str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    _location_error(parameters.get("location_id"), locations, errors)
    start = _date_error("start_date", parameters.get("start_date"), errors)
    end = _date_error("end_date", parameters.get("end_date"), errors)
    if start is not None and end is not None and start > end:
        errors["end_date"] = _RANGE
    _format_error(parameters.get("format"), errors)
    _release_error(parameters.get("release_id"), errors)
    return errors


def _location_error(values: list[str] | None, locations: set[str], errors: dict[str, str]) -> None:
    text, problem = _single(values)
    if problem == "missing":
        errors["location_id"] = "location_id is required."
    elif problem:
        errors["location_id"] = "location_id must be a single value."
    elif text not in locations:
        errors["location_id"] = "That location is not available."


def _date_error(name: str, values: list[str] | None, errors: dict[str, str]) -> date | None:
    text, problem = _single(values)
    if problem == "missing":
        errors[name] = f"{name} is required."
        return None
    if problem:
        errors[name] = f"{name} must be a single value."
        return None
    parsed = _iso_date(text or "")
    if parsed is None:
        errors[name] = f"{name} must be an ISO date."
        return None
    return parsed


def _format_error(values: list[str] | None, errors: dict[str, str]) -> None:
    text, problem = _single(values)
    if problem == "missing":
        errors["format"] = "format is required."
    elif problem:
        errors["format"] = "format must be a single value."
    elif text not in {"csv", "parquet"}:
        errors["format"] = "format must be csv or parquet."


def _release_error(values: list[str] | None, errors: dict[str, str]) -> None:
    if values is None or values == [""]:
        return
    _pin, invalid = _release_pin(values)
    if invalid:
        errors["release_id"] = "release_id must be a UUID."


def _blank_pin(values: list[str] | None) -> str | None:
    if values is None or values == [""]:
        return None
    pin, invalid = _release_pin(values)
    if invalid:
        return None
    return pin


def _download(values: dict[str, str], release: dict) -> str:
    pinned = release["release_id"]
    query = urlencode(
        {
            "location_id": values["location_id"],
            "start_date": values["start_date"],
            "end_date": values["end_date"],
            "format": values["format"],
            "release_id": pinned,
        }
    )
    href = _text(f"{EXPORT_PATH}?{query}")
    label = "CSV" if values["format"] == "csv" else "Parquet"
    shown = _text(pinned)
    version = _text(release["dataset_version"])
    published = _text(release["published_at"])
    return f"""<h3>Your file</h3>
<p>This download is pinned to one published release, so a later publication does not change it.</p>
<p>Pinned release <code id="pinned_release">{shown}</code>.</p>
<p>Schema version {version}.</p>
<p>Last successful publication <time datetime="{published}">{published}</time>.</p>
<p><a class="download" href="{href}">Download {label}</a> for release {shown}.</p>"""


def _table(dictionary: dict) -> str:
    rows = "\n".join(_field(column) for column in dictionary["columns"])
    return f"""<div class="table-wrap"><table>
<caption>Public fields for {_text(dictionary["dataset_id"])}</caption>
<thead>
<tr>
<th scope="col">Field</th>
<th scope="col">Type</th>
<th scope="col">Nullable</th>
<th scope="col">Unit</th>
<th scope="col">Description</th>
<th scope="col">Source</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table></div>"""


def _field(column: dict) -> str:
    nullable = "Yes" if column["nullable"] else "No"
    unit = column["unit"] if column["unit"] else "None"
    return f"""<tr>
<th scope="row">{_text(column["name"])}</th>
<td>{_text(column["logical_type"])}</td>
<td>{nullable}</td>
<td>{_text(unit)}</td>
<td>{_text(column["description"])}</td>
<td>{_text(column["source"])}</td>
</tr>"""


def _form(
    locations: list[tuple[str, str]],
    values: dict[str, str],
    errors: dict[str, str],
) -> str:
    summary = _summary(errors)
    first = next((name for name in _FIELDS if name in errors), "")
    options = ['<option value="">Choose a location</option>']
    for location_id, display_name in locations:
        selected = " selected" if values["location_id"] == location_id else ""
        label = _text(f"{display_name} ({location_id})")
        options.append(f'<option value="{_text(location_id)}"{selected}>{label}</option>')
    radios = _radios(values, errors, first)
    release_help = (
        "Leave blank to use the active release. The page pins one release before the download."
    )
    limit_help = (
        "An export above 100,000 rows or 50,000,000 bytes is refused. "
        "No maximum number of days is set."
    )
    return f"""{summary}<form id="download" method="get" action="{PAGE_PATH}">
<p>{_label("location_id", "Location")}
{_control("location_id", errors, first, _select(options))}</p>
<p>{_label("start_date", "Start date")}
{_control("start_date", errors, first, _date_input("start_date", values))}</p>
<p>{_label("end_date", "End date")}
{_control("end_date", errors, first, _date_input("end_date", values))}</p>
<fieldset>
<legend>Format</legend>
{_format_alert(errors)}
{radios}
</fieldset>
<p>{_label("release_id", "Release")}
{_control("release_id", errors, first, _release_input(values))}
<span id="release_help">{release_help}</span></p>
<p class="help" id="download_help">{limit_help}</p>
<button type="submit">Review download</button>
</form>"""


def _select(options: list[str]) -> str:
    joined = "\n".join(options)
    return f'<select id="location_id" name="location_id">\n{joined}\n</select>'


def _date_input(name: str, values: dict[str, str]) -> str:
    return f'<input id="{name}" name="{name}" type="date" value="{_text(values[name])}">'


def _release_input(values: dict[str, str]) -> str:
    value = _text(values["release_id"])
    return (
        f'<input id="release_id" name="release_id" type="text" spellcheck="false" value="{value}">'
    )


def _label(name: str, text: str) -> str:
    return f'<label for="{name}">{text}</label>'


def _control(name: str, errors: dict[str, str], first: str, control: str) -> str:
    described = ["release_help"] if name == "release_id" else []
    invalid = ""
    error = ""
    if name in errors:
        described.append(f"{name}_error")
        invalid = ' aria-invalid="true"'
        error = f'<p id="{name}_error" role="alert">{_text(errors[name])}</p>'
    focus = " autofocus" if name == first else ""
    described_attr = ""
    if described:
        described_attr = f' aria-describedby="{" ".join(described)}"'
    marked = control.replace(">", f"{invalid}{described_attr}{focus}>", 1)
    return marked + error


def _radios(values: dict[str, str], errors: dict[str, str], first: str) -> str:
    invalid = ""
    if "format" in errors:
        invalid = ' aria-invalid="true" aria-describedby="format_error"'
    focus = " autofocus" if first == "format" else ""
    csv_checked = " checked" if values["format"] == "csv" else ""
    parquet_checked = " checked" if values["format"] == "parquet" else ""
    csv = (
        f'<label for="format_csv"><input id="format_csv" type="radio" name="format" '
        f'value="csv"{csv_checked}{invalid}{focus}> CSV</label>'
    )
    parquet = (
        f'<label for="format_parquet"><input id="format_parquet" type="radio" '
        f'name="format" value="parquet"{parquet_checked}{invalid}> Parquet</label>'
    )
    return f"{csv}\n{parquet}"


def _format_alert(errors: dict[str, str]) -> str:
    if "format" not in errors:
        return ""
    return f'<p id="format_error" role="alert">{_text(errors["format"])}</p>'


def _summary(errors: dict[str, str]) -> str:
    if not errors:
        return ""
    items = []
    for name in _FIELDS:
        if name not in errors:
            continue
        target = "format_csv" if name == "format" else name
        items.append(f'<li><a href="#{target}">{_text(errors[name])}</a></li>')
    joined = "\n".join(items)
    return f'<div role="alert"><p>The download needs a correction.</p><ul>\n{joined}\n</ul></div>'


def _value(values: list[str] | None) -> str:
    if values is None or len(values) != 1:
        return ""
    return values[0]


def _location_choices() -> list[tuple[str, str]]:
    document = _json(_LOCATIONS_PATH)
    return [
        (item["location_id"], item["display_name"])
        for item in document["locations"]
        if item["approved_for_mvp"] is True
    ]


def _document(main: str) -> str:
    return document(
        title="OpenFactors dictionary",
        main_id="dictionary",
        skip="Skip to dictionary",
        main=main,
        current="/datasets/daily-external-factors-ph",
    )


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
