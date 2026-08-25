"""The small slice of Markdown the agent's closing summary actually uses.

The model ends a run with prose, and it writes that prose in Markdown: a heading, some bold
labels, a bullet list, and a table of the letters it drafted. Rendered as plain text -- which
is what the transcript did until now -- the table arrives as a wall of pipes and dashes, and
it is the last thing the operator reads on the page.

Why this rather than the `markdown` package: the input is model output rendered into a
client's UI, so every path has to escape first and emit only tags this file writes. A general
renderer brings raw-HTML passthrough, which would need sanitising on top, and a dependency in
an image whose whole point is a small, auditable surface. The grammar below is a few dozen
lines and covers what the prompt asks the model to produce.

Escaping happens once, up front, on the raw text. Everything after that works on escaped
content and emits a fixed set of tags, so a letter body containing `<script>` renders as
those characters and nothing else.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

_TABLE = "w-full text-xs"
_TH = "border-b rule py-1.5 pr-4 text-left font-medium text-slate-500"
_TD = "border-b border-slate-50 py-1.5 pr-4 align-top"


def _inline(text: str) -> str:
    """Bold and inline code, on already-escaped text."""
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _CODE.sub(r'<code class="mono text-xs">\1</code>', text)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_table(rows: list[str]) -> str:
    header, *body = rows
    out = [f'<div class="overflow-x-auto"><table class="{_TABLE}"><thead><tr>']
    out += [f'<th class="{_TH}">{_inline(cell)}</th>' for cell in _cells(header)]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        out += [f'<td class="{_TD}">{_inline(cell)}</td>' for cell in _cells(row)]
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render(text: str | None) -> Markup:
    """Render the agent's summary. Returns markup that is safe to insert."""
    if not text:
        return Markup("")

    lines = str(escape(text)).replace("\r\n", "\n").split("\n")
    html: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if paragraph:
            html.append(f'<p class="mt-2">{_inline(" ".join(paragraph))}</p>')
            paragraph.clear()
        if bullets:
            html.append('<ul class="mt-2 list-disc space-y-1 pl-5">')
            html.extend(f"<li>{_inline(item)}</li>" for item in bullets)
            html.append("</ul>")
            bullets.clear()
        if table:
            # A single header row and no body is not a table, it is a line with pipes in it.
            html.append(_render_table(table) if len(table) > 1 else f"<p>{_inline(table[0])}</p>")
            table.clear()

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("|"):
            if _TABLE_DIVIDER.match(stripped):
                continue  # the |---|---| row carries no content
            if paragraph or bullets:
                flush()
            table.append(stripped)
            continue
        if table:
            flush()

        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            size = "text-sm" if level <= 2 else "text-xs"
            heading = _inline(stripped.lstrip("#").strip())
            html.append(f'<h3 class="mt-3 {size} font-semibold">{heading}</h3>')
        elif stripped.startswith(("- ", "* ")):
            if paragraph:
                flush()
            bullets.append(stripped[2:])
        else:
            if bullets:
                flush()
            paragraph.append(stripped)

    flush()
    return Markup("".join(html))
