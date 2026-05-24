from __future__ import annotations

import html
import re


BOLD_PATTERN = re.compile(r"\*\*([^*\n][\s\S]*?[^*\n])\*\*")
CODE_PATTERN = re.compile(r"`([^`\n]+)`")
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"


def markdown_to_telegram_html(text: str) -> str:
    placeholders: list[tuple[str, str]] = []

    def stash(value: str) -> str:
        token = f"\u0000{len(placeholders)}\u0000"
        placeholders.append((token, value))
        return token

    def code(match: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    def bold(match: re.Match[str]) -> str:
        inner = markdown_to_telegram_html(match.group(1))
        return stash(f"<b>{inner}</b>")

    rendered = CODE_PATTERN.sub(code, text)
    rendered = BOLD_PATTERN.sub(bold, rendered)
    rendered = html.escape(rendered, quote=False)

    for token, value in placeholders:
        rendered = rendered.replace(html.escape(token, quote=False), value)

    return rendered


def render_terminal_markdown(text: str, *, enabled: bool) -> str:
    if not enabled:
        return text

    def bold(match: re.Match[str]) -> str:
        return f"{ANSI_BOLD}{match.group(1)}{ANSI_RESET}"

    return BOLD_PATTERN.sub(bold, text)
