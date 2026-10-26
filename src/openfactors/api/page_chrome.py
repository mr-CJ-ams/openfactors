"""Shared page frame for the public catalog, download page, and guide.

The frame is local HTML. It does not load a font, a script, or a remote image.
"""

from __future__ import annotations

CAVEAT = (
    "Weather and holidays are contextual factors, not proof of causation for business outcomes."
)

_NAV = (
    ("/", "Catalog"),
    ("/datasets/daily-external-factors-ph", "Download"),
    ("/docs", "For developers"),
)

_STYLE = """
:root {
  color-scheme: light;
  --ink: #1c2430;
  --muted: #3e4c59;
  --paper: #f3f0e8;
  --card: #fffcf7;
  --line: #ddd4c6;
  --teal: #0f3d4c;
  --link: #0b4f6c;
  --alert: #9a3412;
}
body {
  margin: 0;
  font-family: "Segoe UI", "Helvetica Neue", sans-serif;
  font-size: 1.05rem;
  line-height: 1.55;
  color: var(--ink);
  background: var(--paper);
}
.skip {
  position: absolute;
  left: 0.75rem;
  top: 0.75rem;
  padding: 0.4rem 0.7rem;
  background: var(--card);
  transform: translateY(-150%);
}
.skip:focus {
  transform: none;
  outline: 3px solid var(--teal);
  outline-offset: 2px;
}
.wrap {
  max-width: 52rem;
  margin: 0 auto;
  padding: 1.25rem 1.25rem 3rem;
}
.site {
  border-top: 6px solid var(--teal);
  padding-top: 1.1rem;
}
h1, h2, h3 {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  font-weight: 600;
  line-height: 1.25;
  letter-spacing: 0;
}
h1 { font-size: 2.15rem; margin: 0.2rem 0 0.4rem; }
.wordmark {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  font-size: 1.35rem;
  font-weight: 650;
  margin: 0.2rem 0;
}
h2 { font-size: 1.55rem; margin: 2rem 0 0.6rem; }
h3 { font-size: 1.35rem; margin: 0 0 0.4rem; }
h4 {
  margin: 1.15rem 0 0.35rem;
  font-size: 0.95rem;
  letter-spacing: 0.01em;
}
.purpose, .caveat, footer p, .help { color: var(--muted); }
nav { display: flex; flex-wrap: wrap; gap: 0.4rem 0.5rem; margin: 1rem 0 0; }
nav a {
  color: var(--teal);
  text-decoration: none;
  border: 1px solid var(--line);
  background: var(--card);
  border-radius: 999px;
  padding: 0.35rem 0.75rem;
}
nav a[aria-current="page"] { background: var(--teal); color: #fff; border-color: var(--teal); }
a { color: var(--link); }
a:focus, button:focus, input:focus, select:focus {
  outline: 3px solid var(--teal);
  outline-offset: 2px;
}
main {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0.25rem 1.25rem 1.4rem;
  margin-top: 1.25rem;
}
article + article { margin-top: 1.5rem; }
.facts { display: grid; gap: 0.85rem; margin: 1rem 0; padding: 0; }
.facts div {
  margin: 0;
  padding-bottom: 0.7rem;
  border-bottom: 1px solid var(--line);
}
.facts dt { font-size: 0.85rem; font-weight: 650; color: var(--muted); }
.facts dd { margin: 0.15rem 0 0; }
code, pre {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.92em;
}
code { word-break: break-all; }
pre {
  background: #f7f4ee;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.9rem 1rem;
  overflow-x: auto;
}
.table-wrap { overflow-x: auto; margin: 0.5rem 0 1rem; }
table { width: 100%; border-collapse: collapse; }
caption { text-align: left; font-weight: 650; margin-bottom: 0.5rem; }
th, td {
  text-align: left;
  vertical-align: top;
  padding: 0.65rem 0.7rem;
  border-bottom: 1px solid var(--line);
}
th { background: #f7f4ee; }
form p, fieldset { margin: 0 0 1rem; }
label, legend { display: block; font-weight: 650; margin-bottom: 0.35rem; }
input[type="date"], input[type="text"], select {
  width: min(100%, 28rem);
  font: inherit;
  color: var(--ink);
  background: #fff;
  border: 1px solid #c9c1b4;
  border-radius: 8px;
  padding: 0.55rem 0.7rem;
}
#release_id { font-family: ui-monospace, Consolas, monospace; }
fieldset { border: 1px solid var(--line); border-radius: 8px; padding: 0.8rem 1rem 0.4rem; }
fieldset label { font-weight: 450; }
button, .download {
  display: inline-block;
  font: inherit;
  font-weight: 650;
  text-decoration: none;
  background: var(--teal);
  color: #fff;
  border: 0;
  border-radius: 8px;
  padding: 0.7rem 1.1rem;
}
[role="alert"] {
  background: #fff6ee;
  border: 1px solid var(--alert);
  border-radius: 8px;
  padding: 0.15rem 0.9rem;
  margin: 0 0 1rem;
}
footer { margin-top: 1.25rem; }
@media (min-width: 42rem) {
  .facts { grid-template-columns: 1fr 1fr; }
}
"""


def document(
    *,
    title: str,
    main_id: str,
    skip: str,
    main: str,
    current: str,
    heading: bool = True,
) -> str:
    links = []
    for href, label in _NAV:
        current_attr = ' aria-current="page"' if href == current else ""
        links.append(f'<a href="{href}"{current_attr}>{label}</a>')
    navigation = "\n".join(links)
    brand = "<h1>OpenFactors</h1>" if heading else '<p class="wordmark">OpenFactors</p>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_STYLE}</style>
</head>
<body>
<a class="skip" href="#{main_id}">{skip}</a>
<div class="wrap">
<header class="site">
{brand}
<p class="purpose">Daily weather and public holidays for five Philippine cities.</p>
<p class="caveat">{CAVEAT}</p>
<nav aria-label="Pages">
{navigation}
</nav>
</header>
<main id="{main_id}">
{main}
</main>
<footer>
<p>No account or key is required. Weather figures are from Open-Meteo.
Holiday names are transcribed from the Official Gazette of the Philippines.</p>
</footer>
</div>
</body>
</html>
"""
