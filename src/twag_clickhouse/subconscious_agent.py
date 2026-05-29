from __future__ import annotations

import json
import os
import re
import threading
from html import unescape
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .city import CityConfig, active_city
from .client import ClickHouseService
from .config import ClickHouseConfig


DEFAULT_SUBCONSCIOUS_BASE_URL = "https://api.subconscious.dev/v1"
DEFAULT_SUBCONSCIOUS_MODEL = "subconscious/tim-qwen3.6-27b"
TokenUsageCallback = Callable[[dict[str, Any]], None]
DEFAULT_EVENT_PAGE_SIZE = 25
MAX_EVENT_PAGE_SIZE = 100

FORBIDDEN_SQL = re.compile(
    r"\b("
    r"alter|attach|create|delete|detach|drop|grant|insert|kill|optimize|"
    r"rename|replace|revoke|set|system|truncate|update|use"
    r")\b",
    re.IGNORECASE,
)

READ_ONLY_START = re.compile(r"^\s*(select|with|show|describe|desc|explain)\b", re.IGNORECASE)
LIMIT_PATTERN = re.compile(r"\blimit\b", re.IGNORECASE)
def _agent_table_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"\b("
        rf"{prefix}_(current_events|current_manifest|calendar_events|calendar_manifest|events|hosts|event_hosts|manifest)|"
        r"senso_(kb_nodes|kb_documents|kb_chunks|sync_runs)"
        r")\b",
        re.IGNORECASE,
    )


def _planning_leak_pattern(tool_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"\b("
        r"the user is asking|i need to|i should|i will|let'?s|query:|"
        rf"clickhouse|sql query|tool call|{re.escape(tool_name)}|execute"
        r")\b",
        re.IGNORECASE,
    )


# Back-compat: keep module-level patterns wired to the active city. These are
# reread per-call inside the agent methods, so they stay aligned with TWAG_CITY.
AGENT_TABLE_PATTERN = _agent_table_pattern(active_city().table_prefix)
PLANNING_LEAK_PATTERN = _planning_leak_pattern(active_city().tool_name)
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
def _location_pattern(neighborhoods_regex: str) -> re.Pattern[str]:
    return re.compile(rf"\b({neighborhoods_regex})\b", re.IGNORECASE)


# Back-compat constant; rebuilt from the active city's neighborhood regex.
NYTW_LOCATION_PATTERN = _location_pattern(active_city().neighborhoods_regex)
EVENT_LOCATION_SEARCH_PATTERN = re.compile(
    r"\bevents?\b.*\b(in|near|around|at)\b|\b(in|near|around|at)\b.*\bevents?\b",
    re.IGNORECASE,
)
COUNT_PATTERN = re.compile(r"\b(how many|count|total|number of)\b", re.IGNORECASE)
MORE_RESULTS_PATTERN = re.compile(
    r"^\s*(more|next|show more|more results|next results|continue)\s*$",
    re.IGNORECASE,
)
OPEN_RSVP_PATTERN = re.compile(
    r"\b("
    r"open\s+rsvps?|available\s+rsvps?|still\s+open|"
    r"still\s+have\s+open\s+rsvps?|not\s+full|"
    r"spots?\s+(?:left|available)|capacity\s+(?:left|available)"
    r")\b",
    re.IGNORECASE,
)
RSVP_LINK_PHRASE_PATTERN = re.compile(
    r"\b(?:with\s+)?rsvp\s+links?\b|\brsvp\s+urls?\b",
    re.IGNORECASE,
)

_SYSTEM_PROMPT_TEMPLATE = """
You are {agent_name}, a data analyst for the {display_name}
dataset loaded into ClickHouse.

Current local context:
- Current local datetime: {current_datetime}
- Current local date: {current_date}
- Local timezone: {time_zone}
- Dataset event date range: {event_date_range}

Interpret relative dates like "today", "tomorrow", "tonight", "this morning",
"tomorrow morning", and weekdays relative to the current local date above. If a
relative date falls inside the dataset range, query that concrete date instead
of asking the user to clarify. Use event_date for dates and start_at/start_time
for time-of-day filters like morning, afternoon, evening, or tonight.

Use the {tool_name} tool whenever the user asks for facts, counts,
rankings, filtering, recommendations, or analysis that depends on the data.
Do not invent event data. Query the database first, then answer from the rows.

Available ClickHouse tables:

{prefix}_current_events:
- event_id, title, event_date, day, start_time, end_time, start_at, end_at
- host, neighborhood, venue_name, venue_address
- rsvp_url, public_short_url, google_maps
- visibility, guest_action, fetch_status, at_capacity, is_capped, canceled
- owner_count, going_guest_count, total_guest_count, approved_guest_count
- max_capacity, remaining_capacity, badges, owner_ids
- canceled_at, canceled_by, cancellation_message
- description, markdown_body, frontmatter_json, raw_markdown

{prefix}_events:
- static seed event table retained for fallback/back-compat. Prefer
  {prefix}_current_events for current calendar data.

{prefix}_hosts:
- user_id, name, bio, bio_visibility, photo, is_managed, on_partiful
- socials_json, tags, raw_json

{prefix}_event_hosts:
- event_id, user_id, host_position, is_platform_admin

{prefix}_current_manifest:
- event_id, url, title, host, date_time, neighborhood, badges, source, raw_json

senso_kb_nodes:
- kb_node_id, parent_id, path, name, node_type, content_id, version
- processing_status, raw_json, synced_at

senso_kb_documents:
- kb_node_id, content_id, title, summary, text, content_json, download_url
- filename, content_hash, synced_at

senso_kb_chunks:
- kb_node_id, chunk_index, path, title, chunk_text, token_estimate, synced_at

senso_sync_runs:
- run_id, started_at, finished_at, status, nodes, documents, chunks, error

Important query rules:
- Event queries are primary. For event discovery, ranking, counts, schedules,
  capacity, venue, host, RSVP, or neighborhood questions, query
  {prefix}_current_events first and prefer {prefix}_* over Senso.
- Use reasoning only to decide what information to retrieve and how to query it.
  Do not spend reasoning tokens on prose style, formatting, or how to phrase the
  final response.
- Senso is mirrored into ClickHouse as senso_* tables. Never call Senso
  directly. Use senso_* only for general-purpose Tech Week context, policy,
  background, or explanatory questions that the {prefix}_* event rows cannot
  answer.
- Prefer live events: fetch_status = 'ok' AND NOT canceled.
- For "open RSVP", "spots left", or "not full" questions, filter to events
  with rsvp_url != '', NOT at_capacity, and remaining_capacity IS NULL or > 0.
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
"""


_RETRY_AFTER_PLANNING_TEMPLATE = """
Your previous response exposed planning instead of using the tool.
Do not explain your process. Call {tool_name} now, then return only
the final concise answer following the answer contract.
"""


def build_system_prompt(
    city: CityConfig | None = None,
    *,
    now: datetime | None = None,
) -> str:
    city = city or active_city()
    local_now = current_city_datetime(city, now=now)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=city.agent_name,
        display_name=city.display_name,
        tool_name=city.tool_name,
        prefix=city.table_prefix,
        current_datetime=local_now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        current_date=local_now.strftime("%A, %Y-%m-%d"),
        time_zone=city.time_zone,
        event_date_range=city.event_date_range,
    ).strip()


def current_city_datetime(
    city: CityConfig | None = None,
    *,
    now: datetime | None = None,
) -> datetime:
    city = city or active_city()
    try:
        tz = ZoneInfo(city.time_zone)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    value = now or datetime.now(tz)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def build_retry_after_planning_prompt(city: CityConfig | None = None) -> str:
    city = city or active_city()
    return _RETRY_AFTER_PLANNING_TEMPLATE.format(tool_name=city.tool_name).strip()


# Back-compat constants pinned to the active city at import time.
NYTW_AGENT_SYSTEM_PROMPT = build_system_prompt()
RETRY_AFTER_PLANNING_PROMPT = build_retry_after_planning_prompt()

FINAL_FORMAT_PROMPT = """
Rewrite the answer for the user.
Use only the tool results already provided in this conversation.
Do not mention SQL, tools, or reasoning.
Do not reason about formatting. Apply the answer contract directly.
Follow the answer contract exactly.
If fewer rows are provided than requested, show all provided rows without
apologizing. Do not invent missing events.
""".strip()

CONTINUE_TRUNCATED_PROMPT = """
Your previous response was cut off before completion.
Continue from where it stopped and finish the answer. Do not restart.
If you were reasoning, complete the reasoning and include the final answer.
""".strip()

def build_query_tool(city: CityConfig | None = None) -> dict[str, Any]:
    city = city or active_city()
    return {
        "type": "function",
        "function": {
            "name": city.tool_name,
            "description": (
                f"Run one read-only ClickHouse SQL query against {city.display_name} "
                "event tables and synced Senso knowledge-base tables, then return JSON rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            f"A single read-only SQL statement using {city.table_prefix}_* "
                            f"tables or synced senso_* tables. Use {city.table_prefix}_* "
                            "first for event questions."
                        ),
                    }
                },
                "required": ["sql"],
                "additionalProperties": False,
            },
        },
    }


# Back-compat constant for code that imports QUERY_TOOL directly.
QUERY_TOOL = build_query_tool()


@dataclass(frozen=True)
class SubconsciousConfig:
    api_key: str
    model: str = DEFAULT_SUBCONSCIOUS_MODEL
    base_url: str = DEFAULT_SUBCONSCIOUS_BASE_URL
    max_tokens: int = 1200
    enable_thinking: bool = True

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
            enable_thinking=os.getenv(
                "SUBCONSCIOUS_ENABLE_THINKING",
                "true",
            ).lower()
            == "true",
        )


class UnsafeQueryError(ValueError):
    pass


def validate_nytw_query(sql: str, *, prefix: str | None = None) -> str:
    prefix = prefix or active_city().table_prefix
    table_pattern = _agent_table_pattern(prefix)
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
    if not starts_with_table_inspection and not table_pattern.search(normalized):
        raise UnsafeQueryError(f"Query must reference a {prefix}_* or senso_* table")

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


def visible_stream_content(content: str) -> str:
    if "</think>" in content:
        return content.rsplit("</think>", 1)[-1]
    if "<think" in content:
        return ""
    return content


def looks_like_planning_leak(content: str) -> bool:
    cleaned = clean_model_answer(content)
    candidate = cleaned or content
    if not candidate:
        return False
    return bool(PLANNING_LEAK_PATTERN.search(candidate)) and len(candidate.split()) > 5


def response_was_truncated(response: dict[str, Any]) -> bool:
    choices = response.get("choices") or []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        reason = choice.get("finish_reason") or choice.get("stop_reason")
        if isinstance(reason, str) and reason.lower() in {
            "length",
            "max_tokens",
            "max_completion_tokens",
        }:
            return True
    return False


def merge_continued_text(previous: str, continuation: str) -> str:
    if not previous:
        return continuation
    if not continuation:
        return previous

    max_overlap = min(len(previous), len(continuation))
    for size in range(max_overlap, 0, -1):
        if previous[-size:] == continuation[:size]:
            return previous + continuation[size:]
    return previous + continuation


STATUS_TERM_STOPWORDS = {
    "need",
    "needs",
    "want",
    "wants",
    "about",
    "event",
    "events",
    "show",
    "find",
    "list",
    "give",
    "me",
    "please",
}


def status_query_terms(question: str, *, limit: int = 6) -> list[str]:
    terms = [
        term
        for term in expanded_keyword_terms(question)
        if term.lower() not in STATUS_TERM_STOPWORDS
    ]
    return terms[:limit]


def _tool_call_from_object(value: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    function = value.get("function") if isinstance(value.get("function"), dict) else value
    name = function.get("name")
    tool_name = active_city().tool_name
    if name != tool_name:
        return None

    arguments = function.get("arguments") or value.get("arguments") or value.get("parameters")
    sql = ""
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
            if isinstance(decoded, dict):
                sql = str(decoded.get("sql") or "")
        except json.JSONDecodeError:
            sql = arguments
    elif isinstance(arguments, dict):
        sql = str(arguments.get("sql") or "")
    elif isinstance(value.get("sql"), str):
        sql = str(value["sql"])

    if not sql:
        return None

    return {
        "id": value.get("id") or f"embedded-tool-call-{index}",
        "function": {
            "name": tool_name,
            "arguments": json.dumps({"sql": sql}),
        },
    }


def extract_embedded_tool_calls(content: str) -> list[dict[str, Any]]:
    """Recover tool calls that some thinking models emit as content JSON."""
    if active_city().tool_name not in content:
        return []

    decoder = json.JSONDecoder()
    calls: list[dict[str, Any]] = []
    seen_sql: set[str] = set()

    for match in re.finditer(r"\{", content):
        try:
            value, _end = decoder.raw_decode(content[match.start() :])
        except json.JSONDecodeError:
            continue
        call = _tool_call_from_object(value, len(calls) + 1)
        if not call:
            continue
        sql = json.loads(call["function"]["arguments"])["sql"]
        if sql in seen_sql:
            continue
        seen_sql.add(sql)
        calls.append(call)

    return calls


def requested_event_limit(
    question: str,
    default: int = DEFAULT_EVENT_PAGE_SIZE,
    maximum: int = MAX_EVENT_PAGE_SIZE,
) -> int:
    match = re.search(r"\b(?:top|best|first|show|list|recommend)\s+(\d+)\b", question, re.I)
    if not match:
        return default
    return max(1, min(int(match.group(1)), maximum))


def likely_event_list_question(question: str) -> bool:
    if not EVENT_WORD_PATTERN.search(question):
        return False
    return bool(
        EVENT_LIST_COMMAND_PATTERN.search(question)
        or NYTW_LOCATION_PATTERN.search(question)
        or EVENT_LOCATION_SEARCH_PATTERN.search(question)
    )


def likely_nytw_data_question(question: str) -> bool:
    if NYTW_EXPLICIT_PATTERN.search(question):
        return True
    if not NYTW_EVENT_DATA_PATTERN.search(question):
        return False
    return bool(COUNT_PATTERN.search(question) or NYTW_LOCATION_PATTERN.search(question))


def is_more_results_request(question: str) -> bool:
    return bool(MORE_RESULTS_PATTERN.search(question))


def wants_open_rsvps(question: str) -> bool:
    return bool(OPEN_RSVP_PATTERN.search(question))


def event_search_text(question: str) -> str:
    text = OPEN_RSVP_PATTERN.sub(" ", question)
    text = RSVP_LINK_PHRASE_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def keyword_terms(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", event_search_text(question).lower())
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
        "have",
        "involving",
        "involve",
        "involved",
        "in",
        "list",
        "me",
        "of",
        "or",
        "show",
        "still",
        "the",
        "to",
        "top",
        "with",
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


def _clickhouse_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _clickhouse_array(values: list[str]) -> str:
    return "[" + ", ".join(_clickhouse_string(value) for value in values) + "]"


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

    phrase = " ".join(keyword_terms(question))
    terms_array = _clickhouse_array(terms)
    phrase_sql = _clickhouse_string(phrase)
    availability_filter = ""
    if wants_open_rsvps(question):
        availability_filter = """
  AND rsvp_url != ''
  AND NOT at_capacity
  AND (remaining_capacity IS NULL OR remaining_capacity > 0)
""".rstrip()
    conditions = []
    score_parts = []
    for term in terms:
        escaped = term.replace("'", "\\'")
        pattern = f"%{escaped}%"
        conditions.append(
            f"(title ILIKE '{pattern}' OR description ILIKE '{pattern}' OR "
            f"host ILIKE '{pattern}' OR neighborhood ILIKE '{pattern}' OR "
            f"venue_name ILIKE '{pattern}' OR venue_address ILIKE '{pattern}')"
        )
        score_parts.append(
            "multiIf("
            f"title ILIKE '{pattern}', 5, "
            f"description ILIKE '{pattern}', 2, "
            f"host ILIKE '{pattern}', 1, "
            f"neighborhood ILIKE '{pattern}', 6, "
            f"venue_name ILIKE '{pattern}', 3, "
            f"venue_address ILIKE '{pattern}', 2, "
            "0)"
        )

    score = " + ".join(score_parts)
    where = " OR ".join(conditions)
    return f"""
WITH
  {terms_array} AS query_terms,
  {phrase_sql} AS query_phrase
SELECT
  event_id,
  title,
  event_date,
  start_time,
  end_time,
  neighborhood,
  venue_name,
  rsvp_url,
  going_guest_count,
  left(description, 500) AS description_excerpt,
  left(retrieval_text, 900) AS retrieval_context,
  term_overlap,
  (
    ({score}) +
    term_overlap * 4 +
    multiIf(query_phrase != '' AND positionCaseInsensitive(retrieval_text, query_phrase) > 0, 12, 0)
  ) AS relevance_score,
  count() OVER () AS total_matches
FROM
(
SELECT
  *,
  arrayCount(term -> positionCaseInsensitive(retrieval_text, term) > 0, query_terms) AS term_overlap
FROM
(
SELECT
  *,
  concat(
    coalesce(title, ''), ' ',
    coalesce(description, ''), ' ',
    coalesce(markdown_body, ''), ' ',
    coalesce(host, ''), ' ',
    coalesce(neighborhood, ''), ' ',
    coalesce(venue_name, ''), ' ',
    coalesce(venue_address, ''), ' ',
    arrayStringConcat(badges, ' ')
  ) AS retrieval_text
FROM {active_city().table_prefix}_current_events
WHERE fetch_status = 'ok'
  AND NOT canceled
{availability_filter}
)
)
WHERE term_overlap > 0
  OR ({where})
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


def _total_matches_from_rows(rows: list[dict[str, Any]]) -> int | None:
    for row in rows:
        value = row.get("total_matches")
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def format_event_rows(
    result: dict[str, Any],
    *,
    offset: int = 0,
    page_size: int | None = None,
    more_hint: bool = False,
) -> str:
    if not result.get("ok"):
        return f"Query failed: {result.get('error', 'unknown error')}"

    rows = result.get("rows") or []
    if not rows:
        return "No more matching events found." if offset else "No matching events found."

    has_more = page_size is not None and len(rows) > page_size
    display_rows = rows[:page_size] if page_size is not None else rows

    lines = []
    total_matches = _total_matches_from_rows(rows)
    if total_matches is not None:
        start = offset + 1
        end = offset + len(display_rows)
        noun = "event" if total_matches == 1 else "events"
        lines.append(f"Showing {start}-{end} of {total_matches} matching {noun}.")

    for row in display_rows:
        title = row.get("title") or "Untitled event"
        date = row.get("event_date") or "date TBD"
        start = row.get("start_time") or "time TBD"
        end = row.get("end_time") or ""
        time = f"{start}-{end}" if end else str(start)
        location = row.get("venue_name") or row.get("neighborhood") or "location TBD"
        if row.get("venue_name") and row.get("neighborhood"):
            location = f"{row['venue_name']}, {row['neighborhood']}"
        reason = compact_text(
            row.get("description_excerpt")
            or row.get("retrieval_context")
            or row.get("description")
        )
        rsvp_url = row.get("rsvp_url") or row.get("public_short_url") or ""
        lines.append(f"**{title}** — {date}, {time} — {location} — {reason} — {rsvp_url}")

    if has_more and more_hint:
        lines.append("More results are available. Send `more` for the next page.")

    return "\n\n".join(lines)


class NytwSubconsciousAgent:
    def __init__(
        self,
        *,
        clickhouse: ClickHouseService | None = None,
        subconscious: SubconsciousConfig,
    ) -> None:
        self.clickhouse = clickhouse
        self.subconscious = subconscious
        self._clickhouse_lock = threading.Lock()
        self.last_event_map_rows: list[dict[str, Any]] = []
        self.last_sql_queries: list[str] = []

    @classmethod
    def from_env(cls) -> "NytwSubconsciousAgent":
        return cls(
            subconscious=SubconsciousConfig.from_env(),
        )

    def ask(
        self,
        question: str,
        *,
        event_offset: int = 0,
        max_turns: int = 8,
        stream_callback: Callable[[str], None] | None = None,
        raw_stream_callback: Callable[[str], None] | None = None,
        token_usage_callback: TokenUsageCallback | None = None,
        progress_callback: Callable[[str], None] | None = None,
        conversation_context: str | None = None,
    ) -> str:
        self.last_event_map_rows = []
        self.last_sql_queries = []
        city = active_city()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(city)},
        ]
        if conversation_context:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"{conversation_context}\n\n"
                        "Use this brief context only to interpret follow-up references "
                        "and reuse proven ClickHouse query patterns. Current query follows."
                    ),
                }
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": "Understood. I will answer the current query using that context only when relevant.",
                }
            )
        messages.append({"role": "user", "content": question})
        response_parts: list[str] = []

        if likely_event_list_question(question):
            page_size = requested_event_limit(question)
            if progress_callback:
                terms = status_query_terms(question)
                if terms:
                    progress_callback(f"Expanded search terms: {', '.join(terms)}.")
                progress_callback(
                    "Building a ranked event query across titles, descriptions, "
                    "hosts, venues, and neighborhoods."
                )
            sql = build_keyword_event_query(
                question,
                limit=page_size + 1,
                offset=event_offset,
            )
            self.last_sql_queries.append(sql)
            result = self._query_sql(sql)
            if result.get("ok"):
                self.last_event_map_rows = list((result.get("rows") or [])[:page_size])
            if progress_callback:
                if result.get("ok"):
                    row_count = int(result.get("row_count") or len(result.get("rows") or []))
                    visible = min(row_count, page_size)
                    progress_callback(
                        f"ClickHouse returned {row_count} candidate rows; "
                        f"formatting {visible} result{'s' if visible != 1 else ''}."
                    )
                else:
                    progress_callback(
                        "ClickHouse returned an error; preparing the failure details."
                    )
            return format_event_rows(
                result,
                offset=event_offset,
                page_size=page_size,
                more_hint=True,
            )

        for _ in range(max_turns):
            if progress_callback:
                progress_callback(
                    f"Choosing the smallest useful data query across {city.short_name} events "
                    "and synced Senso context."
                )
            response = self._chat(messages, tools=[build_query_tool(city)])
            self._emit_token_usage(response, token_usage_callback)
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                tool_calls = extract_embedded_tool_calls(message.get("content") or "")
            truncated = response_was_truncated(response)

            if not tool_calls and looks_like_planning_leak(message.get("content") or ""):
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            CONTINUE_TRUNCATED_PROMPT if truncated else RETRY_AFTER_PLANNING_PROMPT
                        ),
                    }
                )
                continue

            messages.append(message)

            if not tool_calls:
                response_parts = [
                    merge_continued_text(
                        "".join(response_parts),
                        message.get("content") or "",
                    )
                ]
                if truncated:
                    messages.append({"role": "user", "content": CONTINUE_TRUNCATED_PROMPT})
                    continue
                return clean_model_answer("".join(response_parts))

            for tool_call in tool_calls:
                if progress_callback:
                    progress_callback("Validating the selected ClickHouse query before execution.")
                result = self._handle_tool_call(tool_call)
                if progress_callback:
                    if result.get("ok"):
                        row_count = int(result.get("row_count") or len(result.get("rows") or []))
                        progress_callback(
                            f"ClickHouse returned {row_count} "
                            f"row{'s' if row_count != 1 else ''}; "
                            "sending rows back for synthesis."
                        )
                    else:
                        progress_callback(
                            "The query failed validation or execution; "
                            "preparing the error response."
                        )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "name": tool_call.get("function", {}).get("name"),
                        "content": json.dumps(result, default=_json_default),
                    }
                )
            messages.append({"role": "user", "content": FINAL_FORMAT_PROMPT})
            if progress_callback:
                progress_callback("Formatting the database rows into the final Telegram answer.")
            final_response_parts: list[str] = []
            for _ in range(max_turns):
                if stream_callback and raw_stream_callback:
                    response = self._chat(
                        messages,
                        stream_callback=stream_callback,
                        raw_stream_callback=raw_stream_callback,
                        enable_thinking=False,
                    )
                else:
                    response = self._chat(messages, enable_thinking=False)
                self._emit_token_usage(response, token_usage_callback)
                message = response["choices"][0]["message"]
                content = message.get("content") or ""
                truncated = response_was_truncated(response)
                if looks_like_planning_leak(content):
                    messages.append({"role": "assistant", "content": content})
                    if truncated:
                        final_response_parts = [
                            merge_continued_text("".join(final_response_parts), content)
                        ]
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                CONTINUE_TRUNCATED_PROMPT if truncated else FINAL_FORMAT_PROMPT
                            ),
                        }
                    )
                    continue
                if truncated:
                    final_response_parts = [
                        merge_continued_text("".join(final_response_parts), content)
                    ]
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": CONTINUE_TRUNCATED_PROMPT})
                    continue
                final_response_parts = [
                    merge_continued_text("".join(final_response_parts), content)
                ]
                return clean_model_answer("".join(final_response_parts))
            raise RuntimeError("Agent final answer did not finish within max_turns")

        raise RuntimeError("Agent did not finish within max_turns")

    def _emit_token_usage(
        self,
        response: dict[str, Any],
        callback: TokenUsageCallback | None,
    ) -> None:
        usage = response.get("usage")
        if callback and isinstance(usage, dict):
            callback(usage)

    def start_clickhouse_warmup(
        self,
        *,
        error_callback: Callable[[Exception], None] | None = None,
    ) -> threading.Thread:
        def warm() -> None:
            try:
                self._ensure_clickhouse().ping()
            except Exception as exc:
                if error_callback:
                    error_callback(exc)

        thread = threading.Thread(target=warm, name="clickhouse-warmup", daemon=True)
        thread.start()
        return thread

    def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream_callback: Callable[[str], None] | None = None,
        raw_stream_callback: Callable[[str], None] | None = None,
        enable_thinking: bool | None = None,
    ) -> dict[str, Any]:
        thinking_enabled = (
            self.subconscious.enable_thinking
            if enable_thinking is None
            else enable_thinking
        )
        body: dict[str, Any] = {
            "model": self.subconscious.model,
            "messages": messages,
            "max_tokens": self.subconscious.max_tokens,
            "temperature": 0.2,
            "chat_template_kwargs": {
                "enable_thinking": thinking_enabled,
            },
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if stream_callback:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            return self._chat_stream(body, stream_callback, raw_stream_callback)

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
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Subconscious API network error: {exc}") from exc

    def _chat_stream(
        self,
        body: dict[str, Any],
        stream_callback: Callable[[str], None],
        raw_stream_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.subconscious.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.subconscious.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )

        full_content = ""
        visible_sent = ""
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "error":
                        raise RuntimeError(f"Subconscious stream error: {event.get('error')}")
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    if event.get("type") == "delta":
                        chunk = str(event.get("content") or "")
                    else:
                        choices = event.get("choices") or []
                        choice = choices[0] if choices else {}
                        reason = choice.get("finish_reason") or choice.get("stop_reason")
                        if isinstance(reason, str):
                            finish_reason = reason
                        delta = choice.get("delta", {}) if choice else {}
                        chunk = str(delta.get("content") or "")
                    if not chunk:
                        continue
                    full_content += chunk
                    if raw_stream_callback:
                        raw_stream_callback(chunk)
                    visible = visible_stream_content(full_content)
                    if len(visible) > len(visible_sent):
                        visible_sent = visible
                        stream_callback(visible.strip())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Subconscious API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Subconscious API network error: {exc}") from exc

        return {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": full_content,
                    }
                }
            ],
            "usage": usage,
        }

    def _handle_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        function = tool_call.get("function", {})
        if function.get("name") != active_city().tool_name:
            return {"ok": False, "error": f"Unknown tool: {function.get('name')}"}

        try:
            args = json.loads(function.get("arguments") or "{}")
            sql = args.get("sql", "")
            if sql:
                self.last_sql_queries.append(str(sql))
            return self._query_sql(sql)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _query_sql(self, sql: str) -> dict[str, Any]:
        try:
            safe_sql = add_default_limit(validate_nytw_query(sql))
            rows = self._ensure_clickhouse().query(safe_sql)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "sql": safe_sql,
            "row_count": len(rows),
            "rows": rows,
        }

    def _ensure_clickhouse(self) -> ClickHouseService:
        with self._clickhouse_lock:
            if self.clickhouse is None:
                self.clickhouse = ClickHouseService(ClickHouseConfig.from_env())
            return self.clickhouse
