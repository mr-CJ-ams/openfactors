"""Local operator form for backfill. It is not part of the public API.

The form uses the same request as the backfill command. Dry-run is the
default. A named operator is required. The public catalog does not serve
this page.
"""

from __future__ import annotations

import html
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.runs import MemoryRunLog
from openfactors.orchestration.backfill import (
    BackfillAudit,
    BackfillRequest,
    _closed_steps,
    _locations,
    _source_ids,
    parse_backfill_args,
    run_backfill,
)

_HOST = "127.0.0.1"
_MAX_BODY = 65536


def render_form() -> str:
    """Return the operator form. Dry-run starts checked."""
    sources = "\n".join(
        f'<option value="{html.escape(item)}">{html.escape(item)}</option>'
        for item in sorted(_source_ids(None))
    )
    locations, _start = _locations(None)
    choices = ["all", *sorted(locations)]
    location_options = "\n".join(
        f'<option value="{html.escape(item)}">{html.escape(item)}</option>' for item in choices
    )
    return _document(
        f"""
<h1>Operator backfill</h1>
<p>This form is not the public API. Dry-run is on, so submitting the form does not publish.</p>
<form method="post" action="/">
<p><label for="source">Source</label><br><select id="source" name="source">{sources}</select></p>
<p><label for="location_id">Location</label><br>
<select id="location_id" name="location_id">{location_options}</select></p>
<p><label for="start_date">Start date</label><br>
<input id="start_date" name="start_date" required></p>
<p><label for="end_date">End date</label><br>
<input id="end_date" name="end_date" required></p>
<p><label for="operator">Operator</label><br>
<input id="operator" name="operator" required></p>
<p><label for="reason">Reason</label><br>
<input id="reason" name="reason" required></p>
<p><label for="dry_run"><input id="dry_run" name="dry_run" type="checkbox" value="on" checked>
Dry-run</label></p>
<button type="submit">Submit</button>
</form>
"""
    )


def request_from_fields(fields: Mapping[str, list[str]]) -> BackfillRequest:
    """Build one backfill request. A missing dry-run field stays a dry-run."""
    argv = [
        "--source",
        _one(fields, "source"),
        "--location-id",
        _one(fields, "location_id"),
        "--start-date",
        _one(fields, "start_date"),
        "--end-date",
        _one(fields, "end_date"),
        "--operator",
        _one(fields, "operator"),
        "--reason",
        _one(fields, "reason"),
    ]
    if "code_revision" in fields:
        argv.extend(["--code-revision", _one(fields, "code_revision")])
    dry_values = fields.get("dry_run", ["on"])
    if len(dry_values) != 1:
        raise ConnectorError(ErrorCategory.CONFIG, "dry_run must be a single value.")
    if dry_values[0] != "off":
        argv.append("--dry-run")
    return parse_backfill_args(argv)


def submit_form(
    fields: Mapping[str, list[str]],
    *,
    now: datetime,
    steps: Mapping | None = None,
    runs: MemoryRunLog | None = None,
    audits: list[BackfillAudit] | None = None,
) -> tuple[BackfillRequest, object]:
    """Run one form submission through the backfill command."""
    request = request_from_fields(fields)
    record = run_backfill(
        request,
        _closed_steps() if steps is None else steps,
        runs=runs or MemoryRunLog(),
        audits=[] if audits is None else audits,
        alerts=[],
        now=now,
    )
    return request, record


def handle_operator(
    method: str,
    target: str,
    body: bytes = b"",
    *,
    now: datetime | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Serve only the operator form. Every other path is unavailable."""
    path = target.split("?", 1)[0]
    if path != "/":
        page = _document("<p>That path is not available.</p>")
        return 404, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")
    if "?" in target:
        page = _document("<p>Unknown query parameter.</p>")
        return 422, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")
    if method == "GET":
        page = render_form()
        return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")
    if method != "POST":
        page = _document("<p>Only GET and POST are available.</p>")
        return (
            405,
            {"Allow": "GET, POST", "Content-Type": "text/html; charset=utf-8"},
            page.encode("utf-8"),
        )
    fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    current = datetime.now(UTC) if now is None else now
    try:
        request, record = submit_form(fields, now=current)
    except ConnectorError as exc:
        page = _result(str(exc))
        return 422, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")
    if request.dry_run and record.status == "succeeded":
        message = "Dry-run accepted."
    else:
        message = "The backfill did not run."
    page = _result(
        message,
        source=request.source_id,
        location=request.location_id,
        start=request.start_date.isoformat(),
        end=request.end_date.isoformat(),
    )
    return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")


def operator_server(host: str = _HOST, port: int = 0) -> ThreadingHTTPServer:
    """Bind the form to this computer. Any other address is refused."""
    if host != _HOST:
        raise ConnectorError(ErrorCategory.CONFIG, "The operator form listens on this computer.")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._respond("GET", b"")

        def do_POST(self) -> None:  # noqa: N802
            length = self._length()
            if length is None:
                self._write(400, _document("<p>The form is too large.</p>").encode("utf-8"))
                return
            self._respond("POST", self.rfile.read(length))

        def do_PUT(self) -> None:  # noqa: N802
            self._respond("PUT", b"")

        def _length(self) -> int | None:
            raw = self.headers.get("Content-Length", "0")
            try:
                length = int(raw)
            except ValueError:
                return None
            if length < 0 or length > _MAX_BODY:
                return None
            return length

        def _respond(self, method: str, body: bytes) -> None:
            status, headers, payload = handle_operator(method, self.path, body)
            self._write(status, payload, headers)

        def _write(
            self,
            status: int,
            payload: bytes,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            sent = headers or {"Content-Type": "text/html; charset=utf-8"}
            for name, value in sent.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None, out=None) -> int:
    """Print the form. `--serve` listens on this computer and nowhere else."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--serve"]:
        server = operator_server()
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    if args:
        raise SystemExit("The operator form takes no arguments, or only --serve.")
    print(render_form(), file=out or sys.stdout)
    return 0


def _one(fields: Mapping[str, list[str]], name: str) -> str:
    values = fields.get(name)
    if not values:
        raise ConnectorError(ErrorCategory.CONFIG, f"{name} is required.")
    if len(values) != 1:
        raise ConnectorError(ErrorCategory.CONFIG, f"{name} must be a single value.")
    return values[0]


def _result(
    message: str,
    *,
    source: str = "",
    location: str = "",
    start: str = "",
    end: str = "",
) -> str:
    detail = ""
    if source:
        detail = (
            f"<p>{html.escape(source)} {html.escape(location)} "
            f"{html.escape(start)} {html.escape(end)}</p>"
        )
    return _document(f"<p>{html.escape(message)}</p>{detail}")


def _document(main: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Operator backfill</title>
<style>
body {{
  font-family: "Segoe UI", sans-serif;
  line-height: 1.5;
  max-width: 42rem;
  margin: 2rem auto;
  padding: 0 1rem;
  color: #1a1a1a;
  background: #fff;
}}
a:focus, button:focus, input:focus, select:focus {{
  outline: 3px solid #1a1a1a;
  outline-offset: 2px;
}}
</style>
</head>
<body>
<a href="#backfill">Skip to form</a>
<main id="backfill">
{main}
</main>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
