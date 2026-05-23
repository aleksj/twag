from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .subconscious_agent import (
    DEFAULT_SUBCONSCIOUS_BASE_URL,
    NYTW_AGENT_SYSTEM_PROMPT,
)


def build_query_tool(tool_url: str, *, tool_token: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": "function",
        "name": "query_nytw_clickhouse",
        "description": (
            "Run one read-only ClickHouse SQL query against the remote NYTechWeek "
            "ClickHouse tables and return JSON rows."
        ),
        "url": tool_url.rstrip("/") + "/query",
        "method": "POST",
        "timeout": 30,
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "A single read-only SQL statement using nytw_events, "
                        "nytw_hosts, nytw_event_hosts, or nytw_manifest."
                    ),
                }
            },
            "required": ["sql"],
        },
    }
    if tool_token:
        tool["headers"] = {"X-Tool-Token": tool_token}
    return tool


def build_run_payload(
    *,
    question: str,
    tool_url: str,
    engine: str = "tim-gpt",
    tool_token: str | None = None,
) -> dict[str, Any]:
    return {
        "engine": engine,
        "input": {
            "instructions": f"{NYTW_AGENT_SYSTEM_PROMPT}\n\nUser question: {question}",
            "tools": [build_query_tool(tool_url, tool_token=tool_token)],
        },
        "options": {"timeout": 1800},
    }


def create_run(payload: dict[str, Any], *, api_key: str, base_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/runs",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Subconscious runs API error {exc.code}: {detail}") from exc


def env_api_key() -> str:
    api_key = os.getenv("SUBCONSCIOUS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("SUBCONSCIOUS_API_KEY is required")
    return api_key


def env_base_url() -> str:
    return (
        os.getenv("SUBCONSCIOUS_BASE_URL", DEFAULT_SUBCONSCIOUS_BASE_URL).strip()
        or DEFAULT_SUBCONSCIOUS_BASE_URL
    )
