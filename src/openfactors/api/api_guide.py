"""Developer guide for the read-only API.

The examples in docs/api-guide.md are the requests this page shows. None of
them send a key or a token.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from openfactors.api.catalog import _bad_escape, _error, _split_target

PAGE_PATH = "/docs"
_ROOT = Path(__file__).resolve().parents[3]
GUIDE_PATH = _ROOT / "docs" / "api-guide.md"
_FENCE = re.compile(r"```curl (\d+)\n(.*?)```", re.DOTALL)
_URL = re.compile(r'"([^"]+)"')
_FORBIDDEN = ("authorization", "bearer", "api-key", " -u ")


def handle_api_guide(
    method: str,
    target: str,
) -> tuple[int, dict[str, str], dict | bytes]:
    path, query = _split_target(target)
    if path != PAGE_PATH:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    from urllib.parse import parse_qs

    parameters = parse_qs(query, keep_blank_values=True)
    if parameters:
        return (
            422,
            {},
            _error(
                "UNKNOWN_QUERY_PARAMETER",
                "Unknown query parameter.",
                {"parameter": sorted(parameters)[0]},
            ),
        )
    try:
        body = render_guide(load_guide()).encode("utf-8")
    except Exception:
        return 500, {}, _error("INTERNAL", "The API guide is unavailable.")
    return 200, {"Content-Type": "text/html; charset=utf-8"}, body


def load_guide() -> str:
    return GUIDE_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def example_requests(text: str) -> list[tuple[int, str, str]]:
    found = []
    for match in _FENCE.finditer(text):
        status = int(match.group(1))
        command = match.group(2).strip()
        lowered = command.lower()
        if any(token in lowered for token in _FORBIDDEN):
            raise ValueError("An example asks for credentials.")
        url_match = _URL.search(command)
        if url_match is None or not command.startswith("curl "):
            raise ValueError("An example has no URL.")
        found.append((status, url_match.group(1).strip(), command))
    if not found:
        raise ValueError("The guide has no example request.")
    return found


def render_guide(text: str) -> str:
    example_requests(text)
    return _document(_markdown(text))


def _markdown(text: str) -> str:
    parts: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            block, index = _fence(lines, index)
            parts.append(block)
            continue
        if line.startswith("### "):
            parts.append(f"<h3>{_text(line[4:])}</h3>")
        elif line.startswith("## "):
            parts.append(f"<h2>{_text(line[3:])}</h2>")
        elif line.startswith("# "):
            parts.append(f"<h1>{_text(line[2:])}</h1>")
        elif line.startswith("- "):
            items, index = _list(lines, index)
            parts.append(items)
            continue
        elif line.strip():
            paragraph, index = _paragraph(lines, index)
            parts.append(paragraph)
            continue
        index += 1
    return "\n".join(parts)


def _fence(lines: list[str], index: int) -> tuple[str, int]:
    index += 1
    block: list[str] = []
    while index < len(lines) and not lines[index].startswith("```"):
        block.append(lines[index])
        index += 1
    if index < len(lines):
        index += 1
    return f"<pre><code>{_text(chr(10).join(block))}</code></pre>", index


def _list(lines: list[str], index: int) -> tuple[str, int]:
    items = []
    while index < len(lines) and lines[index].startswith("- "):
        items.append(f"<li>{_text(lines[index][2:])}</li>")
        index += 1
    return "<ul>\n" + "\n".join(items) + "\n</ul>", index


def _paragraph(lines: list[str], index: int) -> tuple[str, int]:
    chunk = []
    while index < len(lines) and lines[index].strip() and not _special(lines[index]):
        chunk.append(lines[index].strip())
        index += 1
    return f"<p>{_text(' '.join(chunk))}</p>", index


def _special(line: str) -> bool:
    return line.startswith("#") or line.startswith("- ") or line.startswith("```")


def _document(main: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenFactors API guide</title>
<style>
body {{
  font-family: "Segoe UI", sans-serif;
  line-height: 1.5;
  max-width: 46rem;
  margin: 2rem auto;
  padding: 0 1rem;
  color: #1a1a1a;
  background: #fff;
}}
a:focus {{ outline: 3px solid #1a1a1a; outline-offset: 2px; }}
pre {{ overflow-x: auto; }}
</style>
</head>
<body>
<a href="#guide">Skip to guide</a>
<header>
<h1>OpenFactors</h1>
<p><a href="/">Back to catalog</a></p>
</header>
<main id="guide">
{main}
</main>
</body>
</html>
"""


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)
