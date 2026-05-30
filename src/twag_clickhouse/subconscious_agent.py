from __future__ import annotations

import json
import logging
import os
import re
import threading
from html import unescape
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .city import CityConfig, active_city
from .client import ClickHouseService
from .config import ClickHouseConfig


DEFAULT_SUBCONSCIOUS_BASE_URL = "https://api.subconscious.dev/v1"
DEFAULT_SUBCONSCIOUS_MODEL = "subconscious/tim-qwen3.6-27b"
LOGGER = logging.getLogger(__name__)
TokenUsageCallback = Callable[[dict[str, Any]], None]
DEFAULT_EVENT_PAGE_SIZE = 25
MAX_EVENT_PAGE_SIZE = 100
MAX_TOOL_RESULT_ROWS = 25
MAX_TOOL_RESULT_STRING_CHARS = 700
OMITTED_TOOL_RESULT_FIELDS = {
    "raw_markdown",
    "raw_json",
    "frontmatter_json",
    "markdown_body",
    "content_json",
    "text",
}
TECH_WEEK_TABLE_PREFIXES = ("nytw", "bostw")

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
    allowed_prefixes = tuple(dict.fromkeys((prefix, *TECH_WEEK_TABLE_PREFIXES)))
    prefix_group = "|".join(re.escape(value) for value in allowed_prefixes)
    return re.compile(
        rf"\b("
        rf"(?:{prefix_group})_(current_events|current_manifest|calendar_events|calendar_manifest|events|hosts|event_hosts|manifest|sync_changes|sync_runs)|"
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
EVENT_CHANGE_PATTERN = re.compile(
    r"\b("
    r"what(?:'s|\s+has)?\s+changed|changes?|changed|"
    r"new\s+events?|events?\s+added|added\s+events?|"
    r"events?\s+updated|updated\s+events?|"
    r"events?\s+(?:removed|canceled|cancelled)|(?:removed|canceled|cancelled)\s+events?"
    r")\b",
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
ISO_DATE_PATTERN = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
MONTH_DAY_PATTERN = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z.]*\s+(\d{1,2})\b",
    re.IGNORECASE,
)
MONTH_NUM = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
RELATIVE_DATE_PATTERN = re.compile(
    r"\b(today|tonight|yesterday|tomorrow|this\s+(?:morning|afternoon|evening))\b",
    re.IGNORECASE,
)
LAST_N_DAYS_PATTERN = re.compile(r"\blast\s+(\d{1,2})\s+days?\b", re.IGNORECASE)
WEEKDAY_PATTERN = re.compile(
    r"\b("
    r"monday|mon|"
    r"tuesday|tues|tue|"
    r"wednesday|weds|wed|"
    r"thursday|thurs|thu|"
    r"friday|fri|"
    r"saturday|sat|"
    r"sunday|sun"
    r")\.?(?=\s|$|[?,!])",
    re.IGNORECASE,
)
WEEKDAY_ALIASES = {
    "mon": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "wed": "wednesday",
    "weds": "wednesday",
    "thu": "thursday",
    "thurs": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}
WEEKDAY_NUM = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
TIME_OF_DAY_PATTERN = re.compile(
    r"\b(morning|afternoon|evening|tonight|night)\b",
    re.IGNORECASE,
)
PLACEHOLDER_OUTPUT_PATTERN = re.compile(
    r"https?://(?:www\.)?example\.com\b|"
    r"\b(?:placeholder|sample)\s+(?:event|rsvp|url|link)\b|"
    r"\brsvp\d+\b",
    re.IGNORECASE,
)
LOCATION_PREPOSITION_PATTERN = re.compile(r"\b(?:near|around|by|at)\b", re.IGNORECASE)


@dataclass(frozen=True)
class GeoEntity:
    label: str
    neighborhood: str
    patterns: tuple[str, ...]


GEO_ENTITIES_BY_CITY: dict[str, tuple[GeoEntity, ...]] = {
    "nyc": (
        GeoEntity(
            label="Columbia University",
            neighborhood="upper manhattan",
            patterns=(
                r"\bcolumbia\s+university\b",
                r"\bcolumbia\s+business\s+school\b",
                r"\bcolumbia\b",
            ),
        ),
        GeoEntity(
            label="NYU",
            neighborhood="greenwich village",
            patterns=(
                r"\bnyu\b",
                r"\bnew\s+york\s+university\b",
            ),
        ),
    ),
    "boston": (
        GeoEntity(
            label="Harvard",
            neighborhood="cambridge",
            patterns=(
                r"\bharvard\s+square\b",
                r"\bharvard\s+university\b",
                r"\bharvard\b",
            ),
        ),
        GeoEntity(
            label="MIT",
            neighborhood="cambridge",
            patterns=(
                r"\bmit\b",
                r"\bmassachusetts\s+institute\s+of\s+technology\b",
            ),
        ),
    ),
}

_SYSTEM_PROMPT_TEMPLATE = """
You are {agent_name}, a data analyst for the {display_name}
dataset loaded into ClickHouse.

Current local context:
- Current local datetime: {current_datetime}
- Current local date: {current_date}
- Local timezone: {time_zone}
- Dataset event date range: {event_date_range}

Interpret relative dates like "today", "tomorrow", "tonight", "this morning",
"tomorrow morning", and weekdays relative to the current local date above. This
includes weekday abbreviations such as "mon", "tue", "tues", "wed", "thu",
"thurs", "fri", "sat", and "sun"; treat them as date constraints, never as
keyword terms. If a relative date falls inside the dataset range, query that
concrete date instead of asking the user to clarify. Use event_date for dates
and start_at/start_time for time-of-day filters like morning, afternoon,
evening, or tonight.
For weekday requests, compute the next matching weekday from the current local
date. For neighborhood requests such as "East Village", "SoHo", "Cambridge",
or "Upper West Side", use a hard neighborhood/venue/address filter; do not
treat the neighborhood words as only loose text terms. For time windows use
start_at: morning is 05:00-11:59, afternoon is 12:00-16:59, and evening/tonight
is 17:00 or later.
For landmark or campus requests such as "near Columbia University", localize
the named place to its surrounding event neighborhood first, then search inside
that neighborhood instead of matching the place name as loose title text. Known
examples: Columbia University -> Upper Manhattan; NYU -> Greenwich Village;
Harvard, Harvard Square, and MIT -> Cambridge.

SQL construction rules for event-search questions:
- Build the ClickHouse SQL yourself.
- First identify hard filters. Hard filters go in WHERE and are not keyword
  terms. Hard filters include city, date, weekday, morning/afternoon/evening,
  neighborhood, campus, landmark, venue, RSVP availability, capacity, canceled
  status, and host when the user asks for a specific host.
- Then identify topic words. Topic words are what the event is about: AI,
  security, hacker, climate, robotics, investing, art, etc. Only topic words
  should become text-match terms.
- For topic searches, do not require every word to match. Search broadly with
  one compact multiSearchAnyCaseInsensitive(concatWithSeparator(...), [...])
  predicate, then return the matching rows in calendar order unless the user
  explicitly asks for a different sort.
- For list/recommend/find/top event questions, call the tool with a SELECT from
  {prefix}_current_events and include: event_id, title, event_date, start_time,
  end_time, neighborhood, venue_name, rsvp_url, going_guest_count, a compact
  description excerpt, and count() OVER () AS total_matches.
- Apply structured constraints in WHERE before topic matching:
  event_date for dates/weekdays/weekday abbreviations; start_at/toHour(start_at)
  for morning/afternoon/evening; neighborhood/venue_name/venue_address for
  locations. Example: "gaming events in flatiron on tue" means the topic terms
  should include gaming, WHERE should include the next Tuesday event_date and
  Flatiron location filter, and "tue" must not be searched as text.
- If the user asks for N results, use LIMIT N+1 so the app can know whether
  another page exists. If no N is stated, use LIMIT 26. Include OFFSET only when
  the conversation asks for the next page.
- For "changed", "new", "removed", "updated", or "history" questions, build
  the SQL against {prefix}_sync_changes / {prefix}_sync_runs directly. Use
  record_id as the event id, table_name = '{prefix}_events' when the user wants
  event-level changes, and convert relative windows such as "yesterday" or
  "last 7 days" into concrete synced_at filters.

Text matching pattern for event retrieval:
- Keep SQL succinct. Avoid CTEs, nested SELECTs, repeated OR ladders, scoring
  expressions, and retrieval_context unless the user explicitly asks for them.
- Use only the real columns listed below. Do not invent search_text, tags,
  concepts, aliases, embedding, or vector columns.
- Match topic words against title, description, markdown_body, host,
  neighborhood, venue_name, venue_address, and arrayStringConcat(badges, ' ').
  Do not search only title or only description.
- Expand obvious synonyms before writing SQL. Examples:
  * "AI infrastructure" -> ai, artificial intelligence, machine learning,
    agents, agent tooling, semantic data, knowledge graph, vector search, RAG,
    MCP, data infrastructure.
  * "hacker" -> hacker, hacking, hackathon, security, cybersecurity, CTF,
    developer, builder, maker.
- Simple recipe:
  1. Put hard filters in WHERE.
  2. Build topic match terms only from topic words and synonyms.
  3. Use one multiSearchAnyCaseInsensitive(concatWithSeparator(...), [...])
     predicate to keep rows with at least one topic/synonym match.
  4. ORDER BY event_date ASC, start_at ASC, title ASC.
- Do not match filler words. Ignore words like near, events, event, tech, week,
  show, list, best, good, find, on, in, at, the, today, tomorrow, monday, tue,
  morning, evening, and city/neighborhood names already used as hard filters.
- The current schema does not provide embedding vectors. Do not invent vector
  columns or cosineDistance queries unless an embedding column is explicitly
  present in the table schema or requested by the user.

Use the {tool_name} tool whenever the user asks for facts, counts,
rankings, filtering, recommendations, or analysis that depends on the data.
Do not invent event data. Query the database first, then answer from the rows.
Placeholder content is invalid: never output example.com links, fake RSVP URLs
such as rsvp1, sample event titles, invented venues, or fabricated dates. If
the rows do not contain a real event or URL, say that the data does not verify
the requested item.

Available ClickHouse tables:

Database structure:
- The default ClickHouse database stores two Tech Week calendars with matching
  schemas: New York uses nytw_* tables/views and Boston uses bostw_* tables/views.
- The active/default city for this conversation is {display_name}, so use
  {prefix}_* unless the user explicitly asks for Boston, New York, both cities,
  or all Tech Week calendars.
- Primary event query targets are current views: nytw_current_events and
  bostw_current_events. These views already select the latest completed sync
  run, so do not add run_id filters for normal current-calendar questions.
- For cross-city event questions, UNION ALL matching SELECT lists from
  nytw_current_events and bostw_current_events, include a literal city column,
  and then ORDER/LIMIT the combined result.
- Raw versioned tables are for reference/debugging: *_calendar_events and
  *_calendar_manifest are keyed by run_id plus record id. Prefer current views
  for user-facing event discovery, counts, schedules, venue, RSVP, capacity, and
  neighborhood questions.

{prefix}_current_events:
- event_id: unique event identifier
- title: event title; strong keyword signal
- description: rich full description; best field for topic/semantic matching
- event_date, day, start_time, end_time, start_at, end_at
- host, neighborhood, venue_name, venue_address
- rsvp_url, public_short_url, google_maps; use rsvp_url for event links and
  public_short_url as a fallback link when needed
- visibility, guest_action, fetch_status, at_capacity, is_capped, canceled
- owner_count, going_guest_count, total_guest_count, approved_guest_count
- max_capacity, remaining_capacity, badges, owner_ids
- canceled_at, canceled_by, cancellation_message
- description, markdown_body, frontmatter_json, raw_markdown

{prefix}_events:
- static seed event table retained for fallback/back-compat. Prefer
  {prefix}_current_events for current calendar data.

{prefix}_calendar_events:
- raw versioned event rows from sync runs; use only when the user asks about
  a specific historical run or when debugging sync state.

{prefix}_hosts:
- user_id, name, bio, bio_visibility, photo, is_managed, on_partiful
- socials_json, tags, raw_json

{prefix}_event_hosts:
- event_id, user_id, host_position, is_platform_admin

{prefix}_current_manifest:
- event_id, url, title, host, date_time, neighborhood, badges, source, raw_json

{prefix}_sync_runs:
- run_id, city_slug, calendar_url, started_at, finished_at, status, events,
  manifest_events, changes, error

{prefix}_sync_changes:
- run_id, city_slug, table_name, change_type, record_id, title
- changed_fields, previous_hash, content_hash, previous_json, content_json, synced_at

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
- Always exclude canceled current events with NOT canceled or canceled = false.
- For topic searches, match title, description, markdown_body, host, badges,
  and optionally venue/neighborhood fields. Use description and markdown_body as
  the richest text fields, but avoid SELECT * because they can be large.
- Build broad topic queries with OR synonym blocks, and build multi-topic
  intersections as separate AND blocks.
- For dates use event_date or day. For chronological ordering, prefer start_at
  when available; start_time is a display string and can sort poorly across
  AM/PM boundaries.
- For open RSVP questions, require a nonempty RSVP link and available capacity:
  rsvp_url != '', NOT at_capacity, and remaining_capacity IS NULL OR
  remaining_capacity > 0.
- For change/history queries, use *_sync_changes. Event-level changes use
  table_name = '{prefix}_events' and record_id as the event id. Join or
  follow up against *_current_events only when the final answer needs current
  event date, venue, or RSVP fields.
- Use reasoning only to decide what information to retrieve and how to query it.
  Do not spend reasoning tokens on prose style, formatting, or how to phrase the
  final response.
- Senso is mirrored into ClickHouse as senso_* tables. Never call Senso
  directly. Use senso_* only for general-purpose Tech Week context, policy,
  background, or explanatory questions that the {prefix}_* event rows cannot
  answer.
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
- For "top N", "best N", "recommend N", or "list N" questions, answer with
  exactly N event bullets when N is stated. For open-ended event-search
  questions with no N, preserve the provided result page instead of
  hand-picking a smaller subset.
- Keep result order from the data query. It is already ordered by the requested
  structured filters.
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
If no explicit smaller N was requested, preserve the page of event rows that
was provided. Do not reduce it to a hand-picked top 5.
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
                "Run one read-only ClickHouse SQL query against Tech Week event "
                "tables/views and synced Senso knowledge-base tables, then return JSON rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "A single read-only SQL statement using nytw_* or bostw_* "
                            f"event tables/views, or synced senso_* tables. Use {city.table_prefix}_* "
                            "first for default event questions, current_events views for "
                            "current calendar data, and UNION ALL across nytw_current_events "
                            "and bostw_current_events only when the user asks across cities. "
                            "For event search, build the SQL yourself with explicit WHERE "
                            "filters for dates, weekday abbreviations, locations, and time windows. "
                            "For topic search: put hard filters in WHERE, build topic terms "
                            "only from topic words and synonyms, keep SQL succinct, use one "
                            "multiSearchAnyCaseInsensitive(concatWithSeparator(...), [...]) predicate across "
                            "real text columns, and ORDER BY event_date ASC, start_at ASC, title ASC. "
                            "Do not invent search_text, tags, concepts, aliases, embedding, or "
                            "relevance_score columns."
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


def compact_tool_result_value(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = re.sub(r"\s+", " ", value).strip()
        if len(cleaned) > MAX_TOOL_RESULT_STRING_CHARS:
            return cleaned[: MAX_TOOL_RESULT_STRING_CHARS - 1].rstrip() + "..."
        return cleaned
    if isinstance(value, list):
        return [compact_tool_result_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return {
            key: compact_tool_result_value(nested)
            for key, nested in value.items()
            if key not in OMITTED_TOOL_RESULT_FIELDS
        }
    return value


def compact_tool_result_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact_rows = []
    for row in rows[:MAX_TOOL_RESULT_ROWS]:
        compact_rows.append(
            {
                key: compact_tool_result_value(value)
                for key, value in row.items()
                if key not in OMITTED_TOOL_RESULT_FIELDS
            }
        )
    return compact_rows


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


def placeholder_output_detected(content: str) -> bool:
    return bool(PLACEHOLDER_OUTPUT_PATTERN.search(content or ""))


def verified_answer_or_placeholder_warning(content: str) -> str:
    answer = clean_model_answer(content)
    if placeholder_output_detected(answer):
        return (
            "I could not verify that answer from the event data. "
            "Please try a narrower event query with a date, neighborhood, topic, or host."
        )
    return answer


def format_query_failure_response(error: Any) -> str:
    detail = str(error or "unknown query error").strip()
    if len(detail) > 220:
        detail = detail[:217].rstrip() + "..."
    return (
        "I couldn't run the data query for that request.\n\n"
        f"Database error: {detail}\n\n"
        "Try a simpler event query with a date, neighborhood, topic, or host, "
        "for example `events in East Village on Tuesday morning`."
    )


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


def tool_call_sql(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function", {})
    try:
        args = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        return ""
    return str(args.get("sql") or "").strip()


def tool_call_from_sql(sql: str, *, call_id: str = "deterministic-event-query") -> dict[str, Any]:
    return {
        "id": call_id,
        "function": {
            "name": active_city().tool_name,
            "arguments": json.dumps({"sql": sql}),
        },
    }


def should_use_event_query_fallback(question: str, content: str = "") -> bool:
    if likely_event_list_question(question):
        return True
    if EVENT_WORD_PATTERN.search(question) and keyword_terms(question):
        return True
    if not keyword_terms(question):
        return False
    return f"{active_city().table_prefix}_current_events" in content


@dataclass(frozen=True)
class EventSearchPlan:
    intent: str
    city: str
    terms: tuple[str, ...]
    date: str
    neighborhood: str
    time_filter: str
    open_rsvp: bool
    limit: int
    offset: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "city": self.city,
            "terms": list(self.terms),
            "date": self.date or None,
            "neighborhood": self.neighborhood or None,
            "time_filter": self.time_filter or None,
            "open_rsvp": self.open_rsvp,
            "limit": self.limit,
            "offset": self.offset,
        }


QUESTION_WORD_PATTERN = re.compile(
    r"\b(what|why|how|who|where|when|is|are|does|do|can|could|should|would)\b",
    re.IGNORECASE,
)


def likely_event_search_plan_question(question: str) -> bool:
    if likely_event_change_question(question):
        return False
    if likely_event_list_question(question):
        return True
    if QUESTION_WORD_PATTERN.search(question):
        return False
    if EVENT_WORD_PATTERN.search(question) and keyword_terms(question):
        return True
    words = re.findall(r"[a-z0-9]+", question.lower())
    return bool(
        keyword_terms(question)
        and 0 < len(words) <= 4
        and not QUESTION_WORD_PATTERN.search(question)
    )


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


def likely_event_change_question(question: str) -> bool:
    return bool(EVENT_CHANGE_PATTERN.search(question))


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
    city = active_city()
    entity = infer_event_geo_entity(text, city)
    if entity:
        text = _remove_geo_entity_text(text, entity)
    text = _location_pattern(city.neighborhoods_regex).sub(" ", text)
    text = ISO_DATE_PATTERN.sub(" ", text)
    text = MONTH_DAY_PATTERN.sub(" ", text)
    text = RELATIVE_DATE_PATTERN.sub(" ", text)
    text = WEEKDAY_PATTERN.sub(" ", text)
    text = TIME_OF_DAY_PATTERN.sub(" ", text)
    text = LOCATION_PREPOSITION_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def keyword_terms(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", event_search_text(question).lower())
    stop_words = {
        "a",
        "an",
        "and",
        "about",
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
        "tell",
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
        "hacker": ["hacker", "hackers", "hackathon", "hacking", "security", "cybersecurity"],
        "hackers": ["hackers", "hacker", "hackathon", "hacking", "security", "cybersecurity"],
        "hacking": ["hacking", "hacker", "hackers", "hackathon", "security", "cybersecurity"],
        "hackathon": ["hackathon", "hacker", "hackers", "hacking", "buildathon"],
        "cyber": ["cyber", "cybersecurity", "security", "infosec", "hacker", "hackers"],
        "cybersecurity": ["cybersecurity", "security", "infosec", "hacker", "hackers"],
        "security": ["security", "cybersecurity", "infosec", "hacker", "hackers"],
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


def infer_event_geo_entity(question: str, city: CityConfig | None = None) -> GeoEntity | None:
    city = city or active_city()
    for entity in GEO_ENTITIES_BY_CITY.get(city.slug, ()):
        if any(re.search(pattern, question, re.IGNORECASE) for pattern in entity.patterns):
            return entity
    return None


def _remove_geo_entity_text(question: str, entity: GeoEntity) -> str:
    text = question
    for pattern in entity.patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text


def _event_query_base_date(city: CityConfig) -> date:
    override = os.getenv("TWAG_CURRENT_DATE", "").strip()
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError:
            pass
    return current_city_datetime(city).date()


def infer_event_query_date(question: str, city: CityConfig | None = None) -> str:
    city = city or active_city()
    iso = ISO_DATE_PATTERN.search(question)
    if iso:
        return iso.group(1)

    base_date = _event_query_base_date(city)
    relative = RELATIVE_DATE_PATTERN.search(question)
    if relative:
        phrase = relative.group(1).lower()
        if phrase == "tomorrow":
            return (base_date + timedelta(days=1)).isoformat()
        if phrase == "yesterday":
            return (base_date - timedelta(days=1)).isoformat()
        return base_date.isoformat()

    weekday = WEEKDAY_PATTERN.search(question)
    if weekday:
        weekday_name = WEEKDAY_ALIASES.get(weekday.group(1).lower(), weekday.group(1).lower())
        target = WEEKDAY_NUM[weekday_name]
        delta = (target - base_date.weekday()) % 7
        return (base_date + timedelta(days=delta)).isoformat()

    md = MONTH_DAY_PATTERN.search(question)
    if md:
        month = MONTH_NUM.get(md.group(1).lower()[:3])
        day = int(md.group(2))
        if month:
            year = int(city.default_map_date.split("-", 1)[0])
            return f"{year:04d}-{month:02d}-{day:02d}"

    return ""


def infer_event_query_neighborhood(question: str, city: CityConfig | None = None) -> str:
    city = city or active_city()
    entity = infer_event_geo_entity(question, city)
    if entity:
        return entity.neighborhood
    match = _location_pattern(city.neighborhoods_regex).search(question)
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1).strip().lower())
    aliases = {
        "uws": "upper west side",
        "ues": "upper east side",
    }
    return aliases.get(value, value)


def infer_event_query_time_filter(question: str) -> str:
    match = TIME_OF_DAY_PATTERN.search(question)
    if not match:
        return ""
    value = match.group(1).lower()
    if value == "morning":
        return "(toHour(start_at) >= 5 AND toHour(start_at) < 12)"
    if value == "afternoon":
        return "(toHour(start_at) >= 12 AND toHour(start_at) < 17)"
    return "toHour(start_at) >= 17"


def build_event_search_plan(
    question: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> EventSearchPlan:
    city = active_city()
    terms = expanded_keyword_terms(question)
    limit = requested_event_limit(question) if limit is None else limit
    date_filter = infer_event_query_date(question, city)
    neighborhood_filter = infer_event_query_neighborhood(question, city)
    time_filter = infer_event_query_time_filter(question)
    has_structured_filters = bool(date_filter or neighborhood_filter or time_filter)
    if not terms and not has_structured_filters:
        terms = ["ai", "agent"]
    return EventSearchPlan(
        intent="event_search",
        city=city.slug,
        terms=tuple(terms),
        date=date_filter,
        neighborhood=neighborhood_filter,
        time_filter=time_filter,
        open_rsvp=wants_open_rsvps(question),
        limit=limit,
        offset=offset,
    )


def render_event_search_sql(plan: EventSearchPlan) -> str:
    city = active_city()
    where_clauses = ["fetch_status = 'ok'", "NOT canceled"]
    if plan.open_rsvp:
        where_clauses.extend(
            [
                "rsvp_url != ''",
                "NOT at_capacity",
                "(remaining_capacity IS NULL OR remaining_capacity > 0)",
            ]
        )
    if plan.date:
        where_clauses.append(f"event_date = {_clickhouse_string(plan.date)}")
    if plan.neighborhood:
        pattern = _clickhouse_string(f"%{plan.neighborhood}%")
        where_clauses.append(
            "("
            f"neighborhood ILIKE {pattern} OR "
            f"venue_name ILIKE {pattern} OR "
            f"venue_address ILIKE {pattern}"
            ")"
        )
    if plan.time_filter:
        where_clauses.append(plan.time_filter)
    search_text = (
        "concatWithSeparator(' ', title, description, markdown_body, host, "
        "neighborhood, venue_name, venue_address, arrayStringConcat(badges, ' '))"
    )
    if plan.terms:
        where_clauses.append(
            f"multiSearchAnyCaseInsensitive({search_text}, {_clickhouse_array(list(plan.terms))})"
        )
    sql = (
        "SELECT event_id, title, event_date, start_time, end_time, neighborhood, "
        "venue_name, rsvp_url, going_guest_count, left(description, 500) AS "
        "description_excerpt, count() OVER () AS total_matches "
        f"FROM {city.table_prefix}_current_events "
        f"WHERE {' AND '.join(where_clauses)} "
        "ORDER BY event_date ASC, start_at ASC, title ASC "
        f"LIMIT {plan.limit}"
    )
    if plan.offset:
        sql += f" OFFSET {plan.offset}"
    return sql


def build_keyword_event_query(
    question: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> str:
    return render_event_search_sql(
        build_event_search_plan(question, limit=limit, offset=offset)
    )


def infer_change_query_since_date(question: str, city: CityConfig | None = None) -> str:
    city = city or active_city()
    base_date = _event_query_base_date(city)
    last_n = LAST_N_DAYS_PATTERN.search(question)
    if last_n:
        return (base_date - timedelta(days=max(1, int(last_n.group(1))))).isoformat()
    inferred = infer_event_query_date(question, city)
    if inferred:
        return inferred
    return (base_date - timedelta(days=1)).isoformat()


def change_type_filter(question: str) -> str:
    lowered = question.lower()
    if re.search(r"\b(new|added|inserted)\b", lowered):
        return "  AND change_type = 'inserted'"
    if re.search(r"\b(updated|modified)\b", lowered):
        return "  AND change_type = 'updated'"
    if re.search(r"\b(removed|canceled|cancelled)\b", lowered):
        return (
            "  AND (change_type = 'removed' OR has(changed_fields, 'canceled') "
            "OR has(changed_fields, 'canceled_at'))"
        )
    return "  AND change_type IN ('inserted', 'updated', 'removed')"


def build_event_change_query(
    question: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> str:
    city = active_city()
    limit = requested_event_limit(question) if limit is None else limit
    since_date = infer_change_query_since_date(question, city)
    since_sql = _clickhouse_string(f"{since_date} 00:00:00")
    table_name = _clickhouse_string(f"{city.table_prefix}_events")
    return f"""
SELECT
  record_id AS event_id,
  title,
  change_type,
  changed_fields,
  synced_at,
  count() OVER () AS total_matches
FROM {city.table_prefix}_sync_changes
WHERE table_name = {table_name}
  AND synced_at >= toDateTime64({since_sql}, 3, 'UTC')
{change_type_filter(question)}
ORDER BY synced_at DESC, title ASC
LIMIT {limit}
{f"OFFSET {offset}" if offset else ""}
""".strip()


def format_event_change_rows(
    result: dict[str, Any],
    *,
    since_date: str,
    offset: int = 0,
) -> str:
    if not result.get("ok"):
        return f"Query failed: {result.get('error', 'unknown error')}"

    rows = result.get("rows") or []
    if not rows:
        return f"No event changes found since {since_date}."

    total_matches = _total_matches_from_rows(rows)
    lines = []
    if total_matches is not None:
        start = offset + 1
        end = offset + len(rows)
        noun = "change" if total_matches == 1 else "changes"
        lines.append(f"Showing {start}-{end} of {total_matches} event {noun} since {since_date}.")

    labels = {
        "inserted": "added",
        "updated": "updated",
        "removed": "removed",
    }
    for row in rows:
        title = row.get("title") or row.get("event_id") or "Untitled event"
        change_type = labels.get(str(row.get("change_type") or ""), str(row.get("change_type") or "changed"))
        changed_fields = row.get("changed_fields") or []
        if isinstance(changed_fields, list) and changed_fields:
            detail = ", ".join(str(field) for field in changed_fields[:6])
        else:
            detail = "event row"
        synced_at = str(row.get("synced_at") or "sync time unknown")
        lines.append(f"**{title}** — {change_type} — {detail} — {synced_at}")

    return "\n\n".join(lines)


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
        enable_thinking: bool | None = None,
        stream_callback: Callable[[str], None] | None = None,
        raw_stream_callback: Callable[[str], None] | None = None,
        planning_stream_callback: Callable[[str], None] | None = None,
        detail_callback: Callable[[str], None] | None = None,
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
        if event_offset:
            page_size = requested_event_limit(question)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Pagination instruction: this is a follow-up page for the same "
                        f"event-search request. Preserve the original constraints and ordering. "
                        f"Use LIMIT {page_size + 1} OFFSET {event_offset} in the ClickHouse SQL."
                    ),
                }
            )
        response_parts: list[str] = []

        if likely_event_search_plan_question(question):
            if progress_callback:
                progress_callback("Planning a structured event search.")
            plan = build_event_search_plan(question, offset=event_offset)
            tool_call = tool_call_from_sql(render_event_search_sql(plan))
            if progress_callback:
                progress_callback("Running the ClickHouse query.")
            result = self._handle_tool_call(tool_call)
            if detail_callback:
                detail = (
                    "Event search plan.\n\n"
                    f"{json.dumps(plan.as_dict(), indent=2)}\n\n"
                    f"ClickHouse SQL executed.\n\n{tool_call_sql(tool_call)}\n"
                )
                if not result.get("ok"):
                    detail += f"\nError: {result.get('error', 'unknown error')}\n"
                detail_callback(detail)
            if progress_callback:
                if result.get("ok"):
                    row_count = int(result.get("row_count") or len(result.get("rows") or []))
                    progress_callback(
                        f"ClickHouse returned {row_count} "
                        f"row{'s' if row_count != 1 else ''}; composing the answer."
                    )
                else:
                    progress_callback(
                        "The query failed validation or execution; "
                        "preparing the error response."
                    )
            if not result.get("ok"):
                return format_query_failure_response(result.get("error"))
            return self._answer_from_tool_results(
                messages,
                [(tool_call, result)],
                max_turns=max_turns,
                enable_thinking=enable_thinking,
                stream_callback=stream_callback,
                raw_stream_callback=raw_stream_callback,
                token_usage_callback=token_usage_callback,
                progress_callback=progress_callback,
            )

        for _ in range(max_turns):
            if progress_callback:
                progress_callback(
                    f"Choosing the smallest useful data query across {city.short_name} events "
                    "and synced Senso context."
                )
            response = self._chat(
                messages,
                tools=[build_query_tool(city)],
                stream_callback=(lambda _partial: None) if planning_stream_callback else None,
                raw_stream_callback=planning_stream_callback,
                enable_thinking=True,
            )
            self._emit_token_usage(response, token_usage_callback)
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                tool_calls = extract_embedded_tool_calls(message.get("content") or "")
            truncated = response_was_truncated(response)
            content = message.get("content") or ""

            if tool_calls:
                valid_tool_calls = [call for call in tool_calls if tool_call_sql(call)]
                if len(valid_tool_calls) != len(tool_calls):
                    if should_use_event_query_fallback(question, content):
                        if progress_callback:
                            progress_callback(
                                "Query generation did not produce usable SQL; "
                                "using the compact event-search fallback."
                            )
                        tool_calls = [
                            tool_call_from_sql(
                                build_keyword_event_query(question, offset=event_offset)
                            )
                        ]
                    else:
                        return format_query_failure_response(
                            "The model did not produce a SQL query. Try a more specific "
                            "event query with a date, neighborhood, topic, or host."
                        )
                else:
                    tool_calls = valid_tool_calls

            if not tool_calls and looks_like_planning_leak(content):
                if should_use_event_query_fallback(question, content):
                    if progress_callback:
                        progress_callback(
                            "Query generation produced planning text instead of SQL; "
                            "using the compact event-search fallback."
                        )
                    tool_calls = [
                        tool_call_from_sql(
                            build_keyword_event_query(question, offset=event_offset)
                        )
                    ]
                else:
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                CONTINUE_TRUNCATED_PROMPT if truncated else RETRY_AFTER_PLANNING_PROMPT
                            ),
                        }
                    )
                    continue

            if not tool_calls and not looks_like_planning_leak(content):
                messages.append(message)
                response_parts = [
                    merge_continued_text(
                        "".join(response_parts),
                        content,
                    )
                ]
                if truncated:
                    messages.append({"role": "user", "content": CONTINUE_TRUNCATED_PROMPT})
                    continue
                return verified_answer_or_placeholder_warning("".join(response_parts))

            if not tool_calls:
                messages.append({"role": "assistant", "content": content})
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

            tool_results: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for tool_call in tool_calls:
                if progress_callback:
                    progress_callback("Validating the selected ClickHouse query before execution.")
                result = self._handle_tool_call(tool_call)
                if detail_callback and result.get("sql"):
                    detail = f"ClickHouse SQL executed.\n\n{result['sql']}\n"
                    if not result.get("ok"):
                        detail += f"\nError: {result.get('error', 'unknown error')}\n"
                    detail_callback(detail)
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
                if not result.get("ok"):
                    return format_query_failure_response(result.get("error"))
                tool_results.append((tool_call, result))
            return self._answer_from_tool_results(
                messages,
                tool_results,
                max_turns=max_turns,
                enable_thinking=enable_thinking,
                stream_callback=stream_callback,
                raw_stream_callback=raw_stream_callback,
                token_usage_callback=token_usage_callback,
                progress_callback=progress_callback,
            )

        raise RuntimeError("Agent did not finish within max_turns")

    def _answer_from_tool_results(
        self,
        messages: list[dict[str, Any]],
        tool_results: list[tuple[dict[str, Any], dict[str, Any]]],
        *,
        max_turns: int,
        enable_thinking: bool | None,
        stream_callback: Callable[[str], None] | None,
        raw_stream_callback: Callable[[str], None] | None,
        token_usage_callback: TokenUsageCallback | None,
        progress_callback: Callable[[str], None] | None,
    ) -> str:
        if tool_results and not messages[-1].get("tool_calls"):
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [tool_call for tool_call, _result in tool_results],
                }
            )
        for tool_call, result in tool_results:
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
            progress_callback("Formatting the database rows into the final answer.")
        final_response_parts: list[str] = []
        for _ in range(max_turns):
            if stream_callback:
                response = self._chat(
                    messages,
                    stream_callback=stream_callback,
                    raw_stream_callback=raw_stream_callback,
                    enable_thinking=enable_thinking,
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
            return verified_answer_or_placeholder_warning("".join(final_response_parts))
        raise RuntimeError("Agent final answer did not finish within max_turns")

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
        tool_call_deltas: dict[int, dict[str, Any]] = {}
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
                        for tool_delta in delta.get("tool_calls") or []:
                            index = int(tool_delta.get("index") or 0)
                            existing = tool_call_deltas.setdefault(
                                index,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if tool_delta.get("id"):
                                existing["id"] = tool_delta["id"]
                            function_delta = tool_delta.get("function") or {}
                            if function_delta.get("name"):
                                existing["function"]["name"] += str(function_delta["name"])
                            if function_delta.get("arguments"):
                                existing["function"]["arguments"] += str(
                                    function_delta["arguments"]
                                )
                        chunk = str(delta.get("content") or "")
                    if not chunk:
                        continue
                    full_content += chunk
                    if raw_stream_callback:
                        raw_stream_callback(chunk)
                    visible = visible_stream_content(full_content)
                    if len(visible) > len(visible_sent):
                        chunk = visible[len(visible_sent) :]
                        visible_sent = visible
                        if chunk:
                            stream_callback(chunk)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Subconscious API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Subconscious API network error: {exc}") from exc

        tool_calls = [
            call
            for _index, call in sorted(tool_call_deltas.items())
            if call.get("function", {}).get("name")
            or call.get("function", {}).get("arguments")
        ]
        message: dict[str, Any] = {
            "role": "assistant",
            "content": full_content,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": message,
                }
            ],
            "usage": usage,
        }

    def _handle_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        function = tool_call.get("function", {})
        if function.get("name") != active_city().tool_name:
            return {"ok": False, "error": f"Unknown tool: {function.get('name')}"}

        try:
            sql = tool_call_sql(tool_call)
            if not sql:
                return {"ok": False, "error": "The model did not produce a SQL query."}
            self.last_sql_queries.append(str(sql))
            result = self._query_sql(sql, compact=True)
            if result.get("ok"):
                rows = result.get("rows") or []
                if isinstance(rows, list) and any(isinstance(row, dict) and row.get("event_id") for row in rows):
                    self.last_event_map_rows = [
                        row for row in rows if isinstance(row, dict) and row.get("event_id")
                    ][:MAX_TOOL_RESULT_ROWS]
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _query_sql(self, sql: str, *, compact: bool = True) -> dict[str, Any]:
        try:
            safe_sql = add_default_limit(validate_nytw_query(sql))
            rows = self._ensure_clickhouse().query(safe_sql)
        except Exception as exc:
            LOGGER.warning("ClickHouse query failed: %s\nSQL:\n%s", exc, sql)
            return {"ok": False, "error": str(exc), "sql": sql}

        result = {
            "ok": True,
            "sql": safe_sql,
            "row_count": len(rows),
            "rows": compact_tool_result_rows(rows) if compact else rows,
        }
        if compact:
            result["truncated_rows"] = max(0, len(rows) - MAX_TOOL_RESULT_ROWS)
        return result

    def _ensure_clickhouse(self) -> ClickHouseService:
        with self._clickhouse_lock:
            if self.clickhouse is None:
                self.clickhouse = ClickHouseService(ClickHouseConfig.from_env())
            return self.clickhouse
