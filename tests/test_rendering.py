from __future__ import annotations

from twag_clickhouse.rendering import (
    ANSI_BOLD,
    ANSI_RESET,
    markdown_to_telegram_html,
    render_terminal_markdown,
)


def test_markdown_to_telegram_html_converts_bold_and_escapes_text() -> None:
    rendered = markdown_to_telegram_html("**AI & Agents** — use `more` <now>")

    assert rendered == "<b>AI &amp; Agents</b> — use <code>more</code> &lt;now&gt;"


def test_render_terminal_markdown_converts_bold_when_enabled() -> None:
    rendered = render_terminal_markdown("**Title** — details", enabled=True)

    assert rendered == f"{ANSI_BOLD}Title{ANSI_RESET} — details"


def test_render_terminal_markdown_leaves_markdown_when_disabled() -> None:
    assert render_terminal_markdown("**Title**", enabled=False) == "**Title**"
