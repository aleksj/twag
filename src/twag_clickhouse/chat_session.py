from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Iterator

from .city import active_city, load_city
from .conversation import AgentConversation, AgentLike
from .subconscious_agent import (
    NytwSubconsciousAgent,
    is_more_results_request,
    likely_event_list_question,
)


DEFAULT_STATUS_STEP_LIMIT = 8
LOGGER = logging.getLogger(__name__)
AGENT_NOT_CONFIGURED_REPLY = (
    "TWAG search is unavailable right now. Please try again later."
)
AGENT_INTERNAL_ERROR_REPLY = (
    "TWAG search hit an internal error while answering. Please try again later."
)
CONFIGURATION_ERROR_MARKERS = (
    "SUBCONSCIOUS_API_KEY is required",
    "CLICKHOUSE_HOST is required",
    "CLICKHOUSE_PASSWORD or CLICKHOUSE_API_KEY is required",
)
SPONSOR_LINE = os.getenv(
    "TWAG_SPONSOR_LINE",
    "**Sponsored by Data.Flowers** - https://data.flowers/",
)
SUBJECTIVE_QUESTION_PATTERN = re.compile(
    r"\b("
    r"best|coolest|fun|funniest|good|recommend|recommendation|suggest|"
    r"should i|what should i do|where should i go|which event should i attend|"
    r"pick for me|vibe|vibes|worth it"
    r")\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z.]*\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_MONTH_NUM = {
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


@dataclass
class ChatState:
    conversation: AgentConversation = field(default_factory=AgentConversation)
    active_question: str | None = None
    active_route: str | None = None
    active_steps: list[str] = field(default_factory=list)
    active_heartbeat: str | None = None
    status_message_id: int | None = None
    verbose: bool = False
    final_reply_sent: bool = False


@dataclass(frozen=True)
class ChatPresentation:
    assistant_label: str = "Bot"
    map_icon: str = ""
    map_environment_name: str = "environment"

    @property
    def map_prefix(self) -> str:
        return f"{self.map_icon} " if self.map_icon else ""


DEFAULT_PRESENTATION = ChatPresentation()
TELEGRAM_PRESENTATION = ChatPresentation(
    map_icon="🗺",
    map_environment_name="bot's environment",
)


@dataclass
class TokenUsageAccumulator:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw: list[dict[str, Any]] = field(default_factory=list)

    def add(self, usage: dict[str, Any]) -> None:
        self.calls += 1
        self.raw.append(usage)
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        total = int(usage.get("total_tokens") or prompt + completion)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "raw": self.raw,
        }


@contextmanager
def active_city_override(slug: str) -> Iterator[None]:
    load_city(slug)
    previous = os.environ.get("TWAG_CITY")
    os.environ["TWAG_CITY"] = slug.strip().lower()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TWAG_CITY", None)
        else:
            os.environ["TWAG_CITY"] = previous


def subjective_question_reply() -> str:
    return active_city().vibe_line


def help_reply(*, presentation: ChatPresentation = DEFAULT_PRESENTATION) -> str:
    city = active_city()
    return (
        f"**TWAG {city.short_name} {presentation.assistant_label}**\n"
        f"Ask me data-backed questions about {city.short_name} events.\n\n"
        f"{SPONSOR_LINE}\n\n"
        "**Try**\n"
        f"- List AI events in {city.example_neighborhood}\n"
        "- Show cybersecurity events with open RSVPs\n"
        "- Which neighborhoods have the most events?\n"
        "- more\n\n"
        "**Commands**\n"
        "`/help` - show this guide\n"
        "`/map [YYYY-MM-DD]` - open the event map for a given day\n"
        "`/verbose` - show the agent thinking stream\n"
        "`/quiet` - show only result updates and final answers\n\n"
        "Use concrete criteria like topic, date, neighborhood, host, capacity, "
        "RSVP status, or time.\n\n"
        "Built by [Aleks](https://github.com/aleksj) and "
        "[Nate Aune](https://github.com/natea), with contributions from "
        "[Stage11](https://github.com/Stage-11-Agentics/)."
    )


HELP_REPLY = help_reply()
GREETING_REPLY = HELP_REPLY
SUBJECTIVE_QUESTION_REPLY = subjective_question_reply()


def public_map_base_url() -> str:
    return os.getenv("TWAG_PUBLIC_MAP_BASE_URL", "").strip()


def infer_date(text: str, fallback: str) -> str:
    iso = _ISO_DATE_RE.search(text)
    if iso:
        return iso.group(1)
    md = _MONTH_DAY_RE.search(text)
    if md:
        month = _MONTH_NUM.get(md.group(1).lower())
        day = int(md.group(2))
        if month:
            year = int(fallback.split("-")[0])
            return f"{year:04d}-{month:02d}-{day:02d}"
    return fallback


def map_url_for(date_iso: str) -> str:
    city = active_city()
    base = public_map_base_url()
    if not base:
        return ""
    if base.endswith(".html"):
        root = base.rsplit("/", 1)[0]
        return f"{root}/{city.map_html_filename}#date={date_iso}"
    base = base if base.endswith("/") else base + "/"
    return f"{base}{city.map_html_filename}#date={date_iso}"


def map_link_line(
    text: str,
    *,
    presentation: ChatPresentation = DEFAULT_PRESENTATION,
) -> str:
    city = active_city()
    date_iso = infer_date(text, city.default_map_date)
    url = map_url_for(date_iso)
    if not url:
        return ""
    return f"\n\n{presentation.map_prefix}[View on the map]({url})"


def event_answer_map_link(
    text: str,
    state: ChatState,
    *,
    presentation: ChatPresentation = DEFAULT_PRESENTATION,
) -> str:
    if is_more_results_request(text):
        if not state.conversation.last_event_question:
            return ""
        return map_link_line(
            state.conversation.last_event_question,
            presentation=presentation,
        )
    if likely_event_list_question(text):
        return map_link_line(text, presentation=presentation)
    return ""


def map_command_reply(
    text: str,
    *,
    presentation: ChatPresentation = DEFAULT_PRESENTATION,
) -> str:
    city = active_city()
    parts = text.strip().split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    date_iso = infer_date(arg, city.default_map_date) if arg else city.default_map_date
    url = map_url_for(date_iso)
    if not url:
        return (
            "Map URL is not configured. Set `TWAG_PUBLIC_MAP_BASE_URL` in the "
            f"{presentation.map_environment_name} to enable the map link."
        )
    return f"{presentation.map_prefix}[{city.short_name} map for {date_iso}]({url})"


def is_subjective_question(text: str) -> bool:
    return bool(SUBJECTIVE_QUESTION_PATTERN.search(text))


def chat_command(text: str) -> str | None:
    first = text.strip().split(maxsplit=1)[0]
    if not first.startswith("/"):
        return None
    command = first[1:].split("@", 1)[0].lower()
    return command or None


def status_text(state: ChatState) -> str:
    route = state.active_route or "working"
    lines = [f"Working on: {state.active_question}", f"Route: {route}", ""]
    lines.extend(f"- {step}" for step in state.active_steps)
    if state.active_heartbeat:
        lines.append(f"- {state.active_heartbeat}")
    return "\n".join(lines).strip()


def update_chat_status(
    state: ChatState,
    *,
    question: str | None = None,
    route: str | None = None,
    step: str | None = None,
    heartbeat: bool = False,
) -> str:
    if question is not None:
        state.active_question = question
        state.active_route = None
        state.active_steps = []
        state.active_heartbeat = None
        state.status_message_id = None
    if route is not None:
        state.active_route = route
    if step is not None:
        if heartbeat:
            state.active_heartbeat = step
        else:
            state.active_heartbeat = None
            state.active_steps.append(step)
            if len(state.active_steps) > DEFAULT_STATUS_STEP_LIMIT:
                state.active_steps = state.active_steps[-DEFAULT_STATUS_STEP_LIMIT:]
    return status_text(state)


def clear_chat_status(state: ChatState) -> None:
    state.active_question = None
    state.active_route = None
    state.active_steps = []
    state.active_heartbeat = None
    state.status_message_id = None


def answer_route(text: str, state: ChatState) -> tuple[str, str]:
    if is_more_results_request(text):
        return (
            "ClickHouse event search",
            "Advancing the saved result window for the previous event search.",
        )
    if likely_event_list_question(text):
        return (
            "ClickHouse event search",
            "Preparing a ranked event search across topic, location, venue, and host fields.",
        )
    return (
        "ClickHouse agent query",
        (
            f"Letting the agent choose between {active_city().short_name} event rows "
            "and synced Senso knowledge-base context."
        ),
    )


def question_log_route(text: str) -> str:
    command = chat_command(text)
    if command:
        return f"command:{command}"
    if is_subjective_question(text):
        return "subjective-refusal"
    if is_more_results_request(text):
        return "clickhouse-event-search:more"
    if likely_event_list_question(text):
        return "clickhouse-event-search"
    return "clickhouse-agent-query"


def agent_error_reply(exc: Exception) -> str:
    message = str(exc)
    if any(marker in message for marker in CONFIGURATION_ERROR_MARKERS):
        LOGGER.exception("TWAG search configuration error")
        return AGENT_NOT_CONFIGURED_REPLY
    LOGGER.exception("TWAG search agent error")
    return AGENT_INTERNAL_ERROR_REPLY


def answer_session_message(
    agent: NytwSubconsciousAgent | AgentLike,
    states: dict[Hashable, ChatState],
    session_id: Hashable,
    text: str,
    progress: Callable[[str], None] | None = None,
    stream_callback: Callable[[str], None] | None = None,
    raw_stream_callback: Callable[[str], None] | None = None,
    token_usage_callback: Callable[[dict[str, Any]], None] | None = None,
    presentation: ChatPresentation = DEFAULT_PRESENTATION,
) -> str:
    state = states.setdefault(session_id, ChatState())
    command = chat_command(text)

    if command == "start":
        return help_reply(presentation=presentation)
    if command == "help":
        return help_reply(presentation=presentation)
    if command == "map":
        return map_command_reply(text, presentation=presentation)
    if command == "verbose":
        state.verbose = True
        return "Verbose mode is on. I'll show the agent thinking stream while I work."
    if command == "quiet":
        state.verbose = False
        return "Quiet mode is on. I'll show only streamed results and final answers."

    if is_subjective_question(text):
        return subjective_question_reply()

    if is_more_results_request(text):
        if progress:
            progress("Reusing the previous event query and moving to the next page.")
        try:
            answer = state.conversation.answer(
                agent,
                text,
                token_usage_callback=token_usage_callback,
                progress_callback=progress,
                no_previous_more_message="Ask an event-list question first, then send 'more'.",
            )
        except Exception as exc:
            return agent_error_reply(exc)
        return answer + event_answer_map_link(text, state, presentation=presentation)

    if progress:
        progress(f"Handing the request to the {active_city().short_name} search pipeline.")
    try:
        answer = state.conversation.answer(
            agent,
            text,
            stream_callback=stream_callback,
            raw_stream_callback=raw_stream_callback,
            token_usage_callback=token_usage_callback,
            progress_callback=progress,
        )
    except Exception as exc:
        return agent_error_reply(exc)
    return answer + event_answer_map_link(text, state, presentation=presentation)
