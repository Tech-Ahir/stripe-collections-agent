"""The renderer for the agent's closing summary.

Its input is model output displayed in a client's UI, so the escaping tests here matter more
than the formatting ones: a letter body quoted into a summary must never become markup.
"""

from __future__ import annotations

from app.web.markdown import render

SUMMARY = """## Summary

**Letters drafted this run (queued for human approval): 7**

| Invoice | Customer | Amount | Tone |
|---|---|---|---|
| ZBKNBZIF-0001 | Agent Test 1 | $57,000.00 | friendly |
| JZBOGW8K-0001 | Ferrolux Metals | $23,400.00 | final |

**Deliberately left alone:**
- **Delta Fabrication** - no valid email on file, so no letter was drafted.
- **Acme Industries** - only 3 days late with a clean history.
"""


def test_the_table_becomes_a_table():
    html = str(render(SUMMARY))

    assert "<table" in html and html.count("<tr>") == 3, "two letters, plus the header row"
    assert "<th" in html and "Invoice" in html
    assert "Ferrolux Metals" in html
    assert "|---|" not in html and "| ZBKNBZIF-0001 |" not in html, "no raw pipes survive"


def test_headings_bold_and_bullets_render():
    html = str(render(SUMMARY))

    assert "<h3" in html and "Summary" in html
    assert "<strong>Letters drafted this run (queued for human approval): 7</strong>" in html
    assert html.count("<li>") == 2


def test_html_in_the_model_output_is_shown_not_executed():
    """The summary quotes letter bodies, and a letter body is attacker-adjacent text."""
    html = str(render("A letter said <script>alert('x')</script> and **that** was that."))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>that</strong>" in html, "escaping first must not break formatting after"


def test_html_inside_a_table_cell_is_escaped_too():
    html = str(render("| A | B |\n|---|---|\n| <img src=x onerror=y> | ok |"))

    assert "<img" not in html
    assert "&lt;img" in html


def test_a_lone_pipe_line_is_not_treated_as_a_table():
    html = str(render("Send it | or don't"))

    assert "<table" not in html


def test_plain_prose_still_reads_as_paragraphs():
    html = str(render("First thought.\n\nSecond thought."))

    assert html.count("<p") == 2


def test_nothing_renders_as_nothing():
    assert str(render(None)) == ""
    assert str(render("")) == ""
