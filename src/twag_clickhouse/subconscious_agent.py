from __future__ import annotations

import json
import os
import re
from html import unescape
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .client import ClickHouseService
from .config import ClickHouseConfig
from .senso import SensoConfig, SensoService, format_senso_answer


DEFAULT_SUBCONSCIOUS_BASE_URL = "https://api.subconscious.dev/v1"
DEFAULT_SUBCONSCIOUS_MODEL = "subconscious/tim-qwen3.6-27b"

FORBIDDEN_SQL = re.compile(
    r"\b("
    r"alter|attach|create|delete|detach|drop|grant|insert|kill|optimize|"
    r"rename|replace|revoke|set|system|truncate|update|use"
    r")\b",
    re.IGNORECASE,
)

READ_ONLY_START = re.compile(r"^\s*(select|with|show|describe|desc|explain)\b", re.IGNORECASE)
LIMIT_PATTERN = re.compile(r"\blimit\b", re.IGNORECASE)
NYTW_TABLE_PATTERN = re.compile(r"\bnytw_(events|hosts|event_hosts|manifest)\b", re.IGNORECASE)
PLANNING_LEAK_PATTERN = re.compile(
    r"\b("
    r"the user is asking|i need to|i should|i will|let'?s|query:|"
    r"clickhouse|sql query|tool call|execute"
    r")\b",
    re.IGNORECASE,
)
EVENT_LIST_COMMAND_PATTERN = re.compile(
    r"\b(top|best|recommend|show|find|list|shortlist)\b",
    re.IGNORECASE,
)
EVENT_WORD_PATTERN = re.compile(r"\bevents?\b", re.IGNORECASE)
NYTW_EXPLICIT_PATTERN = re.compile(
    r"\b(ny\s*tech\s*week|nytw|techweek|tech\s*week)\b",
    re.IGNORECASE,
)
NYTW_EVENT_DATA_PATTERN = re.compile(
    r"\b(events?|hosts?|rsvp|venue|venues|neighborhood|capacity)\b",
    re.IGNORECASE,
)
NYTW_LOCATION_PATTERN = re.compile(
    r"\b(soho|tribeca|brooklyn|manhattan|williamsburg)\b",
    re.IGNORECASE,
)
COUNT_PATTERN = re.compile(r"\b(how many|count|total|number of)\b", re.IGNORECASE)
MORE_RESULTS_PATTERN = re.compile(
    r"^\s*(more|next|show more|more results|next results|continue)\s*$",
    re.IGNORECASE,
)

NYTW_AGENT_SYSTEM_PROMPT = """
You are NYTechWeek ClickHouse Agent, a data analyst for the NY Tech Week 2026
dataset loaded into ClickHouse.

Use the query_nytw_clickhouse tool whenever the user asks for facts, counts,
rankings, filtering, recommendations, or analysis that depends on the data.
Do not invent event data. Query the database first, then answer from the rows.

Available ClickHouse tables:

nytw_events:
- event_id, title, event_date, day, start_time, end_time, start_at, end_at
- host, neighborhood, venue_name, venue_address
- rsvp_url, public_short_url, google_maps
- visibility, guest_action, fetch_status, at_capacity, is_capped, canceled
- owner_count, going_guest_count, total_guest_count, approved_guest_count
- max_capacity, remaining_capacity, badges, owner_ids
- canceled_at, canceled_by, cancellation_message
- description, markdown_body, frontmatter_json, raw_markdown

nytw_hosts:
- user_id, name, bio, bio_visibility, photo, is_managed, on_partiful
- socials_json, tags, raw_json

nytw_event_hosts:
- event_id, user_id, host_position, is_platform_admin

nytw_manifest:
- event_id, url, title, host, date_time, neighborhood, badges, source, raw_json

Important query rules:
- Prefer live events: fetch_status = 'ok' AND NOT canceled.
- Exclude the platform admin host when ranking real hosts:
  is_platform_admin = false.
- Always include enough identifying context in final answers: title, date/time,
  neighborhood or venue, and RSVP URL when listing events.
- Keep SQL read-only. Use SELECT/WITH/SHOW/DESCRIBE/EXPLAIN only.
- Keep result sets small. Use LIMIT unless the user explicitly asks for an
  aggregate count.

Answer contract:
- Final answers only. Never reveal SQL planning, scratch work, tool-use notes,
  hidden reasoning, or implementation details.
- Never print SQL unless the user explicitly asks for SQL.
- For "top N", "best N", "recommend N", "list N", or event-search questions,
  answer with exactly N bullets when N is stated, otherwise at most 5 bullets.
- Each event bullet must be one compact line:
  **Title** — date/time — venue or neighborhood — why it matches — RSVP URL.
- For counts, answer in one sentence.
- If no strong matches are found, say that directly and give the closest
  alternatives in compact bullets.
""".strip()

RETRY_AFTER_PLANNING_PROMPT = """
Your previous response exposed planning instead of using the tool.
Do not explain your process. Call query_nytw_clickhouse now, then return only
the final concise answer following the answer contract.
""".strip()

FINAL_FORMAT_PROMPT = """
Rewrite the answer for the user.
Use only the tool results already provided in this conversation.
Do not mention SQL, tools, or reasoning.
Follow the answer contract exactly.
If fewer rows are provided than requested, show all provided rows without
apologizing. Do not invent missing events.
""".strip()

QUERY_TOOL = {
    "type": "function",
    "function": {
        "name": "query_nytw_clickhouse",
        "description": (
            "Run one read-only ClickHouse SQL query against the NYTechWeek "
            "tables and return JSON rows."
        ),
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
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class SubconsciousConfig:
    api_key: str
    model: str = DEFAULT_SUBCONSCIOUS_MODEL
    base_url: str = DEFAULT_SUBCONSCIOUS_BASE_URL
    max_tokens: int = 1200

    @classmethod
    def from_env(cls) -> "SubconsciousConfig":
        api_key = os.getenv("SUBCONSCIOUS_API_KEY", "").strip()
        if not api_key:
            raise ValueError("SUBCONSCIOUS_API_KEY is required")

        return cls(
            api_key=api_key,
            model=os.getenv("SUBCONSCIOUS_MODEL", DEFAULT_SUBCONSCIOUS_MODEL).strip()
            or DEFAULT_SUBCONSCIOUS_MODEL,
            base_url=os.getenv(
                "SUBCONSCIOUS_BASE_URL",
                DEFAULT_SUBCONSCIOUS_BASE_URL,
            ).strip()
            or DEFAULT_SUBCONSCIOUS_BASE_URL,
            max_tokens=int(os.getenv("SUBCONSCIOUS_MAX_TOKENS", "1200")),
        )


class UnsafeQueryError(ValueError):
    pass


def validate_nytw_query(sql: str) -> str:
    normalized = sql.strip()
    if not normalized:
        raise UnsafeQueryError("SQL query is empty")

    if normalized.count(";") > 1 or (normalized.endswith(";") and ";" in normalized[:-1]):
        raise UnsafeQueryError("Only one SQL statement is allowed")

    normalized = normalized.rstrip(";").strip()
    if not READ_ONLY_START.search(normalized):
        raise UnsafeQueryError("Only SELECT, WITH, SHOW, DESCRIBE, and EXPLAIN are allowed")

    if FORBIDDEN_SQL.search(normalized):
        raise UnsafeQueryError("Query contains a forbidden SQL operation")

    starts_with_table_inspection = re.match(
        r"^\s*(show|describe|desc)\b",
        normalized,
        re.IGNORECASE,
    )
    if not starts_with_table_inspection and not NYTW_TABLE_PATTERN.search(normalized):
        raise UnsafeQueryError("Query must reference a nytw_* table")

    return normalized


def add_default_limit(sql: str, limit: int = 100) -> str:
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return sql
    if LIMIT_PATTERN.search(sql):
        return sql
    return f"{sql}\nLIMIT {limit}"


def _json_default(value: Any) -> str:
    return str(value)


def clean_model_answer(content: str) -> str:
    if "</think>" in content:
        return content.rsplit("</think>", 1)[-1].strip()
    return content.strip()


def looks_like_planning_leak(content: str) -> bool:
    cleaned = clean_model_answer(content)
    if not cleaned:
        return False
    return bool(PLANNING_LEAK_PATTERN.search(cleaned)) and len(cleaned.split()) > 20


def requested_event_limit(question: str, default: int = 5) -> int:
    match = re.search(r"\b(?:top|best|first|show|list|recommend)\s+(\d{1,2})\b", question, re.I)
    if not match:
        return default
    return max(1, min(int(match.group(1)), 10))


def likely_event_list_question(question: str) -> bool:
    return bool(EVENT_LIST_COMMAND_PATTERN.search(question) and EVENT_WORD_PATTERN.search(question))


def likely_nytw_data_question(question: str) -> bool:
    if NYTW_EXPLICIT_PATTERN.search(question):
        return True
    if not NYTW_EVENT_DATA_PATTERN.search(question):
        return False
    return bool(COUNT_PATTERN.search(question) or NYTW_LOCATION_PATTERN.search(question))


def is_more_results_request(question: str) -> bool:
    return bool(MORE_RESULTS_PATTERN.search(question))


def keyword_terms(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", question.lower())
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "best",
        "events",
        "event",
        "find",
        "for",
        "involving",
        "involve",
        "involved",
        "in",
        "list",
        "me",
        "of",
        "or",
        "show",
        "the",
        "to",
        "top",
    }
    terms = [word for word in words if len(word) > 2 and word not in stop_words]
    if "ai" in words and "ai" not in terms:
        terms.insert(0, "ai")
    return terms[:8]


def expanded_keyword_terms(question: str) -> list[str]:
    terms = []
    seen = set()

    expansions = {
        "running": ["running", "run", "runs", "runner", "runners", "5k", "jog"],
        "run": ["run", "running", "runs", "runner", "runners", "5k", "jog"],
        "agents": ["agents", "agent", "agentic", "autonomous", "orchestration"],
        "agent": ["agent", "agents", "agentic", "autonomous", "orchestration"],
        "orcheastration": ["orchestration", "orchestrate", "orchestrating"],
        "orchestration": ["orchestration", "orchestrate", "orchestrating"],
    }

    for term in keyword_terms(question):
        for expanded in expansions.get(term, [term]):
            if expanded not in seen:
                terms.append(expanded)
                seen.add(expanded)

    return terms[:12]


def build_keyword_event_query(
    question: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> str:
    terms = expanded_keyword_terms(question)
    limit = requested_event_limit(question) if limit is None else limit
    if not terms:
        terms = ["ai", "agent"]

    conditions = []
    score_parts = []
    for term in terms:
        escaped = term.replace("'", "\\'")
        pattern = f"%{escaped}%"
        conditions.append(
            f"(title ILIKE '{pattern}' OR description ILIKE '{pattern}' OR host ILIKE '{pattern}')"
        )
        score_parts.append(
            "multiIf("
            f"title ILIKE '{pattern}', 5, "
            f"description ILIKE '{pattern}', 2, "
            f"host ILIKE '{pattern}', 1, "
            "0)"
        )

    score = " + ".join(score_parts)
    where = " OR ".join(conditions)
    return f"""
SELECT
  title,
  event_date,
  start_time,
  end_time,
  neighborhood,
  venue_name,
  rsvp_url,
  going_guest_count,
  left(description, 500) AS description_excerpt,
  ({score}) AS relevance_score
FROM nytw_events
WHERE fetch_status = 'ok'
  AND NOT canceled
  AND ({where})
ORDER BY relevance_score DESC, coalesce(going_guest_count, 0) DESC
LIMIT {limit}
{f"OFFSET {offset}" if offset else ""}
""".strip()


def compact_text(value: Any, *, max_chars: int = 130) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`#>\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "matches the requested topic"
    sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    if len(sentence) <= max_chars:
        return sentence
    return sentence[: max_chars - 1].rstrip() + "..."


def format_event_rows(result: dict[str, Any], *, offset: int = 0) -> str:
    if not result.get("ok"):
        return f"Query failed: {result.get('error', 'unknown error')}"

    rows = result.get("rows") or []
    if not rows:
        return "No more matching events found." if offset else "No matching events found."

    lines = []
    for row in rows:
        title = row.get("title") or "Untitled event"
        date = row.get("event_date") or "date TBD"
        start = row.get("start_time") or "time TBD"
        end = row.get("end_time") or ""
        time = f"{start}-{end}" if end else str(start)
        location = row.get("venue_name") or row.get("neighborhood") or "location TBD"
        if row.get("venue_name") and row.get("neighborhood"):
            location = f"{row['venue_name']}, {row['neighborhood']}"
        reason = compact_text(row.get("description_excerpt") or row.get("description"))
        rsvp_url = row.get("rsvp_url") or row.get("public_short_url") or ""
        lines.append(f"**{title}** — {date}, {time} — {location} — {reason} — {rsvp_url}")

    return "\n\n".join(lines)


class NytwSubconsciousAgent:
    def __init__(
        self,
        *,
        clickhouse: ClickHouseService | None = None,
        subconscious: SubconsciousConfig,
        senso: SensoService | None = None,
    ) -> None:
        self.clickhouse = clickhouse
        self.subconscious = subconscious
        self.senso = senso

    @classmethod
    def from_env(cls) -> "NytwSubconsciousAgent":
        senso_config = SensoConfig.from_env()
        return cls(
            subconscious=SubconsciousConfig.from_env(),
            senso=SensoService(senso_config) if senso_config else None,
        )

    def ask(self, question: str, *, event_offset: int = 0, max_turns: int = 8) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": NYTW_AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]

        if likely_event_list_question(question):
            result = self._query_sql(build_keyword_event_query(question, offset=event_offset))
            return format_event_rows(result, offset=event_offset)

        if self.senso and not likely_nytw_data_question(question):
            answer = self._answer_from_senso(question)
            if answer:
                return answer

        for _ in range(max_turns):
            response = self._chat(messages, tools=[QUERY_TOOL])
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []

            if not tool_calls and looks_like_planning_leak(message.get("content") or ""):
                messages.append(
                    {
                        "role": "assistant",
                        "content": "I need to query the database before answering.",
                    }
                )
                messages.append({"role": "user", "content": RETRY_AFTER_PLANNING_PROMPT})
                continue

            messages.append(message)

            if not tool_calls:
                return clean_model_answer(message.get("content") or "")

            for tool_call in tool_calls:
                result = self._handle_tool_call(tool_call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "name": tool_call.get("function", {}).get("name"),
                        "content": json.dumps(result, default=_json_default),
                    }
                )
            messages.append({"role": "user", "content": FINAL_FORMAT_PROMPT})

        raise RuntimeError("Agent did not finish within max_turns")

    def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.subconscious.model,
            "messages": messages,
            "max_tokens": self.subconscious.max_tokens,
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        request = urllib.request.Request(
            f"{self.subconscious.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.subconscious.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Subconscious API error {exc.code}: {detail}") from exc

    def _handle_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        function = tool_call.get("function", {})
        if function.get("name") != "query_nytw_clickhouse":
            return {"ok": False, "error": f"Unknown tool: {function.get('name')}"}

        try:
            args = json.loads(function.get("arguments") or "{}")
            return self._query_sql(args.get("sql", ""))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _query_sql(self, sql: str) -> dict[str, Any]:
        try:
            safe_sql = add_default_limit(validate_nytw_query(sql))
            if self.clickhouse is None:
                self.clickhouse = ClickHouseService(ClickHouseConfig.from_env())
            rows = self.clickhouse.query(safe_sql)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "sql": safe_sql,
            "row_count": len(rows),
            "rows": rows,
        }

    def _answer_from_senso(self, question: str) -> str:
        try:
            result = self.senso.search(question) if self.senso else {}
        except Exception as exc:
            return f"Senso search failed: {exc}"
        return format_senso_answer(result)
