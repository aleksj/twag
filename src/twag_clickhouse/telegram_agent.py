from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from .city import active_city
from .conversation import AgentConversation
from .rendering import markdown_to_telegram_html
from .subconscious_agent import (
    NytwSubconsciousAgent,
    is_more_results_request,
    likely_event_list_question,
)
from .client import CLICKHOUSE_HTTP_LOGGER


TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_STATUS_STEP_LIMIT = 8
DEFAULT_STATUS_HEARTBEAT_SECONDS = 8.0
DEFAULT_STREAM_DRAFT_INTERVAL_SECONDS = 1.0
TELEGRAM_STREAM_RESET_CHARS = 3600
DEFAULT_QUESTION_LOG_PATH = "logs/twag-telegram-questions.jsonl"
STATUS_HEARTBEAT_TEMPLATES = (
    "Still on this step: {step}",
    "Keeping Telegram warm while this runs: {step}",
    "No final answer yet; current step is still: {step}",
)
SUBJECTIVE_QUESTION_PATTERN = re.compile(
    r"\b("
    r"best|coolest|fun|funniest|good|recommend|recommendation|suggest|"
    r"should i|what should i do|where should i go|which event should i attend|"
    r"pick for me|vibe|vibes|worth it"
    r")\b",
    re.IGNORECASE,
)

SPONSOR_LINE = (
    "**Sponsored by data.flowers** - the data excellence company.\n"
    "Want to sponsor TechWeek AI search? Contact info@data.flowers"
)


def _subjective_question_reply() -> str:
    return active_city().vibe_line


def _help_reply() -> str:
    city = active_city()
    return (
        f"**TWAG {city.short_name} Bot**\n"
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
        "Use concrete criteria like topic, date, neighborhood, host, capacity, RSVP status, or time.\n\n"
        "Made by Nate Aune ([@natea](https://twitter.com/natea))."
    )


_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z.]*\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _public_map_base_url() -> str:
    base = os.getenv("TWAG_PUBLIC_MAP_BASE_URL", "").strip()
    if not base:
        return ""
    return base if base.endswith("/") else base + "/"


def _infer_date(text: str, fallback: str) -> str:
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


def _map_url_for(date_iso: str) -> str:
    base = _public_map_base_url()
    if not base:
        return ""
    city = active_city()
    return f"{base}{city.map_html_filename}#date={date_iso}"


def _map_link_line(text: str) -> str:
    city = active_city()
    date_iso = _infer_date(text, city.default_map_date)
    url = _map_url_for(date_iso)
    if not url:
        return ""
    return f"\n\n🗺 [View on map]({url})"


def _map_command_reply(text: str) -> str:
    city = active_city()
    parts = text.strip().split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    date_iso = _infer_date(arg, city.default_map_date) if arg else city.default_map_date
    url = _map_url_for(date_iso)
    if not url:
        return (
            "Map URL is not configured. Set `TWAG_PUBLIC_MAP_BASE_URL` in the "
            "bot's environment to enable the map link."
        )
    return f"🗺 [{city.short_name} map for {date_iso}]({url})"


# Back-compat module constants pinned to the active city at import.
SUBJECTIVE_QUESTION_REPLY = _subjective_question_reply()
HELP_REPLY = _help_reply()
GREETING_REPLY = HELP_REPLY


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
class TelegramMessageContext:
    update_id: int | None
    message_id: int
    chat_id: int
    chat_type: str
    chat_title: str
    chat_username: str
    user_id: int | None
    is_bot: bool | None
    username: str
    first_name: str
    last_name: str
    language_code: str
    text: str


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


class QuestionLogWriter:
    def __init__(self, path: str | None) -> None:
        self.path = self._normalize_path(path)
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_path(path: str | None) -> pathlib.Path | None:
        if path is None:
            return pathlib.Path(DEFAULT_QUESTION_LOG_PATH)
        value = path.strip()
        if value.lower() in {"", "0", "false", "no", "off", "none"}:
            return None
        return pathlib.Path(value)

    def write(
        self,
        context: TelegramMessageContext,
        *,
        route: str,
        answer: str,
        ok: bool,
        error: str | None,
        duration_ms: int,
        token_usage: dict[str, Any],
    ) -> None:
        if self.path is None:
            return

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "platform": "telegram",
            "route": route,
            "ok": ok,
            "error": error,
            "duration_ms": duration_ms,
            "update_id": context.update_id,
            "message_id": context.message_id,
            "chat": {
                "id": context.chat_id,
                "type": context.chat_type,
                "title": context.chat_title,
                "username": context.chat_username,
            },
            "user": {
                "id": context.user_id,
                "is_bot": context.is_bot,
                "username": context.username,
                "first_name": context.first_name,
                "last_name": context.last_name,
                "language_code": context.language_code,
            },
            "question": context.text,
            "answer": answer,
            "token_usage": token_usage,
        }

        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except Exception as exc:
            print(f"question log write failed: {exc}", flush=True)


@dataclass
class TelegramAgentConfig:
    bot_token: str
    poll_timeout: int = 30
    request_timeout: int = 45
    retry_initial: float = 2.0
    retry_max: float = 60.0
    status_heartbeat_seconds: float = DEFAULT_STATUS_HEARTBEAT_SECONDS
    stream_drafts: bool = True
    stream_draft_interval_seconds: float = DEFAULT_STREAM_DRAFT_INTERVAL_SECONDS
    question_log_path: str | None = DEFAULT_QUESTION_LOG_PATH
    clear_webhook_on_start: bool = True
    warm_clickhouse_on_start: bool = True
    allowed_chat_ids: set[int] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "TelegramAgentConfig":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        allowed = {
            int(value.strip())
            for value in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
            if value.strip()
        }

        poll_timeout = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
        request_timeout = int(
            os.getenv("TELEGRAM_REQUEST_TIMEOUT", str(max(45, poll_timeout + 15)))
        )

        return cls(
            bot_token=bot_token,
            poll_timeout=poll_timeout,
            request_timeout=request_timeout,
            retry_initial=float(os.getenv("TELEGRAM_RETRY_INITIAL_SECONDS", "2")),
            retry_max=float(os.getenv("TELEGRAM_RETRY_MAX_SECONDS", "60")),
            status_heartbeat_seconds=float(
                os.getenv(
                    "TELEGRAM_STATUS_HEARTBEAT_SECONDS",
                    str(DEFAULT_STATUS_HEARTBEAT_SECONDS),
                )
            ),
            stream_drafts=os.getenv("TELEGRAM_STREAM_DRAFTS", "true").lower()
            == "true",
            stream_draft_interval_seconds=float(
                os.getenv(
                    "TELEGRAM_STREAM_DRAFT_INTERVAL_SECONDS",
                    str(DEFAULT_STREAM_DRAFT_INTERVAL_SECONDS),
                )
            ),
            question_log_path=os.getenv(
                "TELEGRAM_QUESTION_LOG_PATH",
                DEFAULT_QUESTION_LOG_PATH,
            ),
            clear_webhook_on_start=os.getenv(
                "TELEGRAM_CLEAR_WEBHOOK_ON_POLL",
                "true",
            ).lower()
            == "true",
            warm_clickhouse_on_start=os.getenv(
                "TELEGRAM_WARM_CLICKHOUSE_ON_START",
                "true",
            ).lower()
            == "true",
            allowed_chat_ids=allowed,
        )


class TelegramTransientError(RuntimeError):
    pass


class TelegramRateLimitError(TelegramTransientError):
    def __init__(self, retry_after: int, detail: str) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(f"Telegram rate limit; retry after {self.retry_after}s: {detail}")


def telegram_rate_limit_from_result(result: dict[str, Any]) -> TelegramRateLimitError | None:
    if int(result.get("error_code") or 0) != 429:
        return None
    parameters = result.get("parameters")
    retry_after = 1
    if isinstance(parameters, dict):
        try:
            retry_after = int(parameters.get("retry_after") or 1)
        except (TypeError, ValueError):
            retry_after = 1
    return TelegramRateLimitError(retry_after, json.dumps(result, sort_keys=True))


class TelegramApi:
    def __init__(self, bot_token: str, *, request_timeout: int = 45) -> None:
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.request_timeout = request_timeout

    def request(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {}
        if payload is not None:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(detail)
            except json.JSONDecodeError:
                result = {}
            if exc.code == 429 and isinstance(result, dict):
                rate_limit = telegram_rate_limit_from_result(result)
                if rate_limit:
                    raise rate_limit from exc
            raise RuntimeError(f"Telegram API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason):
                raise TelegramTransientError(f"Telegram request timed out: {reason}") from exc
            raise TelegramTransientError(f"Telegram network error: {reason}") from exc
        except (TimeoutError, socket.timeout, OSError) as exc:
            if isinstance(exc, OSError) and "timed out" not in str(exc).lower():
                raise
            raise TelegramTransientError(f"Telegram request timed out: {exc}") from exc

        if not result.get("ok"):
            rate_limit = telegram_rate_limit_from_result(result)
            if rate_limit:
                raise rate_limit
            raise RuntimeError(f"Telegram API error: {result}")
        return result

    def delete_webhook(self) -> None:
        self.request("deleteWebhook", {"drop_pending_updates": "false"})

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": str(timeout),
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            payload["offset"] = str(offset)

        return self.request("getUpdates", payload)["result"]

    def send_message(self, chat_id: int, text: str) -> list[dict[str, Any]]:
        responses = []
        for part in split_telegram_message(text):
            responses.append(
                self.request(
                    "sendMessage",
                    {
                        "chat_id": str(chat_id),
                        "text": markdown_to_telegram_html(part),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    },
                )
            )
        return responses

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.request(
            "editMessageText",
            {
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "text": markdown_to_telegram_html(text[:TELEGRAM_MESSAGE_LIMIT]),
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.request("sendChatAction", {"chat_id": str(chat_id), "action": action})

    def send_message_draft(self, chat_id: int, draft_id: int, text: str) -> None:
        self.request(
            "sendMessageDraft",
            {
                "chat_id": str(chat_id),
                "draft_id": str(draft_id),
                "text": text[:TELEGRAM_MESSAGE_LIMIT],
            },
        )


def split_telegram_message(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        chunk = remaining[:TELEGRAM_MESSAGE_LIMIT]
        split_at = chunk.rfind("\n\n")
        if split_at < 1000:
            split_at = chunk.rfind("\n")
        if split_at < 1000:
            split_at = len(chunk)
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return parts


def message_context(update: dict[str, Any]) -> TelegramMessageContext | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return None

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    user = message.get("from")
    if not isinstance(user, dict):
        user = {}

    return TelegramMessageContext(
        update_id=int(update["update_id"]) if "update_id" in update else None,
        message_id=int(message.get("message_id", 0)),
        chat_id=int(chat["id"]),
        chat_type=str(chat.get("type") or ""),
        chat_title=str(chat.get("title") or ""),
        chat_username=str(chat.get("username") or ""),
        user_id=int(user["id"]) if "id" in user else None,
        is_bot=user.get("is_bot") if isinstance(user.get("is_bot"), bool) else None,
        username=str(user.get("username") or ""),
        first_name=str(user.get("first_name") or ""),
        last_name=str(user.get("last_name") or ""),
        language_code=str(user.get("language_code") or ""),
        text=text.strip(),
    )


def message_text(update: dict[str, Any]) -> tuple[int, int, str] | None:
    context = message_context(update)
    if context is None:
        return None
    return context.chat_id, context.message_id, context.text


def is_subjective_question(text: str) -> bool:
    return bool(SUBJECTIVE_QUESTION_PATTERN.search(text))


def telegram_command(text: str) -> str | None:
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
            if len(state.active_steps) > TELEGRAM_STATUS_STEP_LIMIT:
                state.active_steps = state.active_steps[-TELEGRAM_STATUS_STEP_LIMIT:]
    return status_text(state)


def status_heartbeat_text(state: ChatState, *, beat: int, elapsed: int) -> str:
    step = state.active_steps[-1] if state.active_steps else "working through the search pipeline"
    template = STATUS_HEARTBEAT_TEMPLATES[beat % len(STATUS_HEARTBEAT_TEMPLATES)]
    return f"{template.format(step=step)} ({elapsed}s elapsed.)"


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
    command = telegram_command(text)
    if command:
        return f"command:{command}"
    if is_subjective_question(text):
        return "subjective-refusal"
    if is_more_results_request(text):
        return "clickhouse-event-search:more"
    if likely_event_list_question(text):
        return "clickhouse-event-search"
    return "clickhouse-agent-query"


def answer_message(
    agent: NytwSubconsciousAgent,
    states: dict[int, ChatState],
    chat_id: int,
    text: str,
    progress: Any | None = None,
    stream_callback: Any | None = None,
    raw_stream_callback: Any | None = None,
    token_usage_callback: Any | None = None,
) -> str:
    state = states.setdefault(chat_id, ChatState())
    command = telegram_command(text)

    if command == "start":
        return _help_reply()
    if command == "help":
        return _help_reply()
    if command == "map":
        return _map_command_reply(text)
    if command == "verbose":
        state.verbose = True
        return "Verbose mode is on. I'll show the agent thinking stream while I work."
    if command == "quiet":
        state.verbose = False
        return "Quiet mode is on. I'll show only streamed results and final answers."

    if is_subjective_question(text):
        return _subjective_question_reply()

    if is_more_results_request(text):
        if progress:
            progress("Reusing the previous event query and moving to the next page.")
        answer = state.conversation.answer(
            agent,
            text,
            token_usage_callback=token_usage_callback,
            progress_callback=progress,
            no_previous_more_message="Ask an event-list question first, then send 'more'.",
        )
        return answer + _map_link_line(state.active_question or text)

    if progress:
        progress(f"Handing the request to the {active_city().short_name} search pipeline.")
    answer = state.conversation.answer(
        agent,
        text,
        stream_callback=stream_callback,
        raw_stream_callback=raw_stream_callback,
        token_usage_callback=token_usage_callback,
        progress_callback=progress,
    )
    if likely_event_list_question(text):
        answer = answer + _map_link_line(text)
    return answer


def answer_message_with_status(
    *,
    telegram: TelegramApi,
    agent: NytwSubconsciousAgent,
    states: dict[int, ChatState],
    chat_id: int,
    text: str,
    status_heartbeat_seconds: float = DEFAULT_STATUS_HEARTBEAT_SECONDS,
    stream_drafts: bool = True,
    stream_draft_interval_seconds: float = DEFAULT_STREAM_DRAFT_INTERVAL_SECONDS,
    token_usage_callback: Any | None = None,
) -> str:
    state = states.setdefault(chat_id, ChatState())
    state.final_reply_sent = False
    command = telegram_command(text)

    if command or is_subjective_question(text):
        return answer_message(
            agent,
            states,
            chat_id,
            text,
            token_usage_callback=token_usage_callback,
        )

    show_status = state.verbose
    if show_status:
        route, first_step = answer_route(text, state)
        update_chat_status(state, question=text, route=route, step="Received your message.")
        update_chat_status(state, step=first_step)

        try:
            sent = telegram.send_message(chat_id, status_text(state))
            message = sent[0].get("result", {}) if sent else {}
            state.status_message_id = int(message.get("message_id", 0) or 0) or None
        except TelegramRateLimitError as exc:
            show_status = False
            print(
                "telegram optional status disabled for this reply: "
                f"rate limited for {exc.retry_after}s",
                flush=True,
            )
    status_lock = threading.Lock()
    stream_lock = threading.Lock()
    last_draft_at = 0.0
    draft_failed = False
    optional_rate_limited_until = 0.0
    last_stream_text = ""
    stream_page_text = ""
    stream_message_id: int | None = None
    raw_stream_text = ""

    def optional_updates_allowed() -> bool:
        return time.monotonic() >= optional_rate_limited_until

    def note_optional_rate_limit(source: str, exc: TelegramRateLimitError) -> None:
        nonlocal optional_rate_limited_until, draft_failed
        retry_after = max(1, exc.retry_after)
        optional_rate_limited_until = max(
            optional_rate_limited_until,
            time.monotonic() + retry_after,
        )
        if source == "streaming":
            draft_failed = True
        print(
            f"telegram {source} suppressed: rate limited for {retry_after}s",
            flush=True,
        )

    def progress(step: str) -> None:
        if not show_status:
            return
        with status_lock:
            update_chat_status(state, step=step)
            text_to_send = status_text(state)
        if not optional_updates_allowed():
            return
        try:
            telegram.send_chat_action(chat_id)
            if state.status_message_id:
                telegram.edit_message_text(chat_id, state.status_message_id, text_to_send)
        except TelegramRateLimitError as exc:
            note_optional_rate_limit("status updates", exc)
        except Exception as exc:
            print(f"telegram status update failed: {exc}", flush=True)

    def heartbeat_progress(step: str) -> None:
        if not show_status:
            return
        with status_lock:
            update_chat_status(state, step=step, heartbeat=True)
            text_to_send = status_text(state)
        if not optional_updates_allowed():
            return
        try:
            telegram.send_chat_action(chat_id)
            if state.status_message_id:
                telegram.edit_message_text(chat_id, state.status_message_id, text_to_send)
        except TelegramRateLimitError as exc:
            note_optional_rate_limit("status heartbeat", exc)
        except Exception as exc:
            print(f"telegram status heartbeat failed: {exc}", flush=True)

    def stream_update(partial: str, *, force: bool = False) -> bool:
        nonlocal draft_failed, last_draft_at, last_stream_text, stream_message_id
        nonlocal stream_page_text
        if not partial or not stream_drafts or draft_failed or not optional_updates_allowed():
            return False
        now = time.monotonic()
        is_complete_message = len(partial) <= TELEGRAM_MESSAGE_LIMIT
        with stream_lock:
            if partial == last_stream_text:
                return is_complete_message and stream_message_id is not None
            if not force and now - last_draft_at < stream_draft_interval_seconds:
                return False
            if partial.startswith(last_stream_text):
                delta = partial[len(last_stream_text) :]
            else:
                delta = partial
                stream_page_text = ""
                stream_message_id = None
            last_stream_text = partial
            last_draft_at = now
            if (
                stream_message_id
                and len(stream_page_text) + len(delta) > TELEGRAM_STREAM_RESET_CHARS
            ):
                try:
                    telegram.edit_message_text(chat_id, stream_message_id, "...")
                except TelegramRateLimitError as exc:
                    note_optional_rate_limit("streaming", exc)
                    return False
                except Exception as exc:
                    draft_failed = True
                    print(f"telegram streaming disabled for this reply: {exc}", flush=True)
                    return False
                stream_message_id = None
                stream_page_text = ""
            stream_page_text += delta
            if len(stream_page_text) > TELEGRAM_STREAM_RESET_CHARS:
                stream_page_text = "..." + stream_page_text[-(TELEGRAM_STREAM_RESET_CHARS - 3) :]
            text_to_send = stream_page_text.strip()
        try:
            if stream_message_id:
                telegram.edit_message_text(chat_id, stream_message_id, text_to_send)
            else:
                sent = telegram.send_message(chat_id, text_to_send)
                message = sent[0].get("result", {}) if sent else {}
                stream_message_id = int(message.get("message_id", 0) or 0) or None
            return is_complete_message
        except TelegramRateLimitError as exc:
            note_optional_rate_limit("streaming", exc)
            return False
        except Exception as exc:
            draft_failed = True
            print(f"telegram streaming disabled for this reply: {exc}", flush=True)
            return False

    def raw_stream_update(chunk: str) -> None:
        nonlocal raw_stream_text
        raw_stream_text += chunk
        stream_update(raw_stream_text)

    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        if status_heartbeat_seconds <= 0:
            return
        started_at = time.monotonic()
        beat = 0
        while not stop_heartbeat.wait(status_heartbeat_seconds):
            elapsed = int(time.monotonic() - started_at)
            with status_lock:
                message = status_heartbeat_text(state, beat=beat, elapsed=elapsed)
            beat += 1
            heartbeat_progress(message)

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name=f"telegram-status-{chat_id}",
        daemon=True,
    )

    failed = False
    try:
        if show_status:
            heartbeat_thread.start()
        progress("Starting the search and keeping this status message live.")
        answer = answer_message(
            agent,
            states,
            chat_id,
            text,
            progress=progress,
            stream_callback=(lambda _partial: None) if state.verbose else stream_update,
            raw_stream_callback=raw_stream_update if state.verbose else None,
            token_usage_callback=token_usage_callback,
        )
        final_preview = raw_stream_text if state.verbose and raw_stream_text else answer
        final_stream_complete = stream_update(final_preview, force=True)
        if final_stream_complete and stream_message_id:
            state.final_reply_sent = True
        progress("Answer ready; sending it now.")
        return answer
    except Exception:
        failed = True
        raise
    finally:
        stop_heartbeat.set()
        if show_status:
            heartbeat_thread.join(timeout=1)
        if state.status_message_id and optional_updates_allowed():
            try:
                with status_lock:
                    state.active_heartbeat = None
                    final_status = status_text(state)
                telegram.edit_message_text(
                    chat_id,
                    state.status_message_id,
                    final_status + ("\n\nStopped with an error." if failed else "\n\nDone."),
                )
            except TelegramRateLimitError as exc:
                note_optional_rate_limit("final status update", exc)
            except Exception as exc:
                print(f"telegram final status update failed: {exc}", flush=True)
        clear_chat_status(state)


def send_final_reply_with_rate_limit_retry(
    telegram: TelegramApi,
    chat_id: int,
    reply: str,
) -> None:
    try:
        telegram.send_message(chat_id, reply)
        return
    except TelegramRateLimitError as exc:
        sleep_for = exc.retry_after + 1
        print(
            f"telegram final reply rate limited; retrying once in {sleep_for}s",
            flush=True,
        )
        time.sleep(sleep_for)

    try:
        telegram.send_message(chat_id, reply)
    except TelegramRateLimitError as exc:
        print(
            "telegram final reply still rate limited after retry; "
            f"dropping reply after Telegram requested {exc.retry_after}s more",
            flush=True,
        )
    except Exception as exc:
        print(f"telegram final reply send failed after retry: {exc}", flush=True)


def run_telegram_agent() -> int:
    config = TelegramAgentConfig.from_env()
    telegram = TelegramApi(config.bot_token, request_timeout=config.request_timeout)
    agent = NytwSubconsciousAgent.from_env()
    states: dict[int, ChatState] = {}
    question_log = QuestionLogWriter(config.question_log_path)

    if config.warm_clickhouse_on_start:
        agent.start_clickhouse_warmup(
            error_callback=lambda exc: print(
                f"clickhouse warmup failed: {exc}",
                flush=True,
            )
        )

    clickhouse_logger = logging.getLogger(CLICKHOUSE_HTTP_LOGGER)
    print(
        "TWAG Telegram agent is polling. "
        f"module={__file__} "
        f"clickhouse_filters={[type(filter_).__name__ for filter_ in clickhouse_logger.filters]} "
        "Press Ctrl+C to stop.",
        flush=True,
    )

    if config.clear_webhook_on_start:
        try:
            telegram.delete_webhook()
        except TelegramRateLimitError as exc:
            print(
                "telegram deleteWebhook rate limited; "
                f"continuing to poll after Telegram cooldown ({exc.retry_after}s)",
                flush=True,
            )
            time.sleep(exc.retry_after + 1)
        except TelegramTransientError as exc:
            print(f"telegram deleteWebhook transient error: {exc}; continuing to poll", flush=True)
    offset: int | None = None
    retry_delay = config.retry_initial

    while True:
        try:
            updates = telegram.get_updates(offset=offset, timeout=config.poll_timeout)
            retry_delay = config.retry_initial
            for update in updates:
                offset = int(update["update_id"]) + 1
                context = message_context(update)
                if context is None:
                    continue

                chat_id = context.chat_id
                text = context.text
                if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
                    continue

                started_at = time.monotonic()
                usage = TokenUsageAccumulator()
                error: str | None = None
                reply = ""
                try:
                    reply = answer_message_with_status(
                        telegram=telegram,
                        agent=agent,
                        states=states,
                        chat_id=chat_id,
                        text=text,
                        status_heartbeat_seconds=config.status_heartbeat_seconds,
                        stream_drafts=config.stream_drafts,
                        stream_draft_interval_seconds=config.stream_draft_interval_seconds,
                        token_usage_callback=usage.add,
                    )
                except Exception as exc:
                    error = str(exc)
                    reply = f"Sorry, I hit an error while answering: {exc}"
                finally:
                    question_log.write(
                        context,
                        route=question_log_route(text),
                        answer=reply,
                        ok=error is None,
                        error=error,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                        token_usage=usage.as_dict(),
                    )

                state = states.get(chat_id)
                if reply and not (state and state.final_reply_sent):
                    send_final_reply_with_rate_limit_retry(telegram, chat_id, reply)
                if state:
                    state.final_reply_sent = False
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except TelegramRateLimitError as exc:
            sleep_for = exc.retry_after + random.uniform(0, 1.0)
            print(
                "telegram polling rate limited: "
                f"retrying after Telegram cooldown in {sleep_for:.1f}s",
                flush=True,
            )
            time.sleep(sleep_for)
            retry_delay = config.retry_initial
        except TelegramTransientError as exc:
            sleep_for = retry_delay + random.uniform(0, min(1.0, retry_delay / 4))
            print(
                f"telegram polling transient error: {exc}; retrying in {sleep_for:.1f}s",
                flush=True,
            )
            time.sleep(sleep_for)
            retry_delay = min(config.retry_max, retry_delay * 2)
        except Exception as exc:
            print(f"telegram polling error: {exc}", flush=True)
            time.sleep(5)


@contextmanager
def single_instance_lock() -> Any:
    lock_path = pathlib.Path(os.getenv("TELEGRAM_AGENT_LOCK_FILE", ".telegram-agent.lock"))
    lock_fd: int | None = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(lock_fd, str(os.getpid()).encode("utf-8"))
        yield
    except FileExistsError:
        pid = lock_path.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Telegram agent lock exists at {lock_path}. "
            f"Another local instance may be running with PID {pid}. "
            "Stop it or delete the stale lock file."
        )
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    if load_dotenv:
        load_dotenv(".env", override=False)
    with single_instance_lock():
        return run_telegram_agent()


if __name__ == "__main__":
    raise SystemExit(main())
