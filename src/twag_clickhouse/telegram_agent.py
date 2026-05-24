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
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

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
STATUS_HEARTBEAT_MESSAGES = (
    "Still working; waiting on the backend.",
    "Still working; keeping the request alive.",
    "Still working; no final answer yet.",
)
SUBJECTIVE_QUESTION_PATTERN = re.compile(
    r"\b("
    r"best|coolest|fun|funniest|good|recommend|recommendation|suggest|"
    r"should i|what should i do|where should i go|which event should i attend|"
    r"pick for me|vibe|vibes|worth it"
    r")\b",
    re.IGNORECASE,
)

SUBJECTIVE_QUESTION_REPLY = (
    "C'mon, this is NYC, not a vibes committee. I'm not here to crown the "
    "'best' event or tell you what to do with your afternoon. Give me criteria: "
    "topic keywords, date, neighborhood, host, capacity, RSVP status, or time. "
    "Try: 'List AI events in SoHo on June 3' or 'Show cybersecurity events with open RSVPs.'"
)
SPONSOR_LINE = (
    "**Sponsored by data.flowers** - the data excellence company.\n"
    "Want to sponsor TechWeek AI search? Contact info@data.flowers"
)
HELP_REPLY = (
    "**TWAG NY Tech Week Bot**\n"
    "Ask me data-backed questions about TechWeek NY events.\n\n"
    f"{SPONSOR_LINE}\n\n"
    "**Try**\n"
    "- List AI events in SoHo\n"
    "- Show cybersecurity events with open RSVPs\n"
    "- Which neighborhoods have the most events?\n"
    "- more\n\n"
    "**Commands**\n"
    "`/help` - show this guide\n"
    "`/verbose` - show the agent thinking stream\n"
    "`/quiet` - show only result updates and final answers\n\n"
    "Use concrete criteria like topic, date, neighborhood, host, capacity, RSVP status, or time."
)
GREETING_REPLY = HELP_REPLY


@dataclass
class ChatState:
    conversation: AgentConversation = field(default_factory=AgentConversation)
    active_question: str | None = None
    active_route: str | None = None
    active_steps: list[str] = field(default_factory=list)
    status_message_id: int | None = None
    verbose: bool = False
    final_reply_sent: bool = False


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


def message_text(update: dict[str, Any]) -> tuple[int, int, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return None

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    return int(chat["id"]), int(message.get("message_id", 0)), text.strip()


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
    return "\n".join(lines).strip()


def update_chat_status(
    state: ChatState,
    *,
    question: str | None = None,
    route: str | None = None,
    step: str | None = None,
) -> str:
    if question is not None:
        state.active_question = question
        state.active_route = None
        state.active_steps = []
        state.status_message_id = None
    if route is not None:
        state.active_route = route
    if step is not None:
        state.active_steps.append(step)
        if len(state.active_steps) > TELEGRAM_STATUS_STEP_LIMIT:
            state.active_steps = state.active_steps[-TELEGRAM_STATUS_STEP_LIMIT:]
    return status_text(state)


def clear_chat_status(state: ChatState) -> None:
    state.active_question = None
    state.active_route = None
    state.active_steps = []
    state.status_message_id = None


def answer_route(text: str, state: ChatState) -> tuple[str, str]:
    if is_more_results_request(text):
        return "ClickHouse event search", "Fetching the next page of matching events."
    if likely_event_list_question(text):
        return "ClickHouse event search", "Searching NY Tech Week events by topic, location, venue, and host."
    return (
        "ClickHouse agent query",
        "Querying NYTW event data first, with synced Senso KB context available if needed.",
    )


def answer_message(
    agent: NytwSubconsciousAgent,
    states: dict[int, ChatState],
    chat_id: int,
    text: str,
    progress: Any | None = None,
    stream_callback: Any | None = None,
    raw_stream_callback: Any | None = None,
) -> str:
    state = states.setdefault(chat_id, ChatState())
    command = telegram_command(text)

    if command == "start":
        return GREETING_REPLY
    if command == "help":
        return HELP_REPLY
    if command == "verbose":
        state.verbose = True
        return "Verbose mode is on. I'll show the agent thinking stream while I work."
    if command == "quiet":
        state.verbose = False
        return "Quiet mode is on. I'll show only streamed results and final answers."

    if is_subjective_question(text):
        return SUBJECTIVE_QUESTION_REPLY

    if is_more_results_request(text):
        if progress:
            progress("Requesting the next page from ClickHouse.")
        return state.conversation.answer(
            agent,
            text,
            no_previous_more_message="Ask an event-list question first, then send 'more'.",
        )

    if progress:
        progress("Running the agent.")
    return state.conversation.answer(
        agent,
        text,
        stream_callback=stream_callback,
        raw_stream_callback=raw_stream_callback,
    )


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
) -> str:
    state = states.setdefault(chat_id, ChatState())
    state.final_reply_sent = False
    command = telegram_command(text)

    if command or is_subjective_question(text):
        return answer_message(agent, states, chat_id, text)

    show_status = state.verbose
    if show_status:
        route, first_step = answer_route(text, state)
        update_chat_status(state, question=text, route=route, step="Received your message.")
        update_chat_status(state, step=first_step)

        sent = telegram.send_message(chat_id, status_text(state))
        message = sent[0].get("result", {}) if sent else {}
        state.status_message_id = int(message.get("message_id", 0) or 0) or None
    status_lock = threading.Lock()
    stream_lock = threading.Lock()
    last_draft_at = 0.0
    draft_failed = False
    last_stream_text = ""
    stream_message_id: int | None = None
    raw_stream_text = ""

    def progress(step: str) -> None:
        if not show_status:
            return
        with status_lock:
            update_chat_status(state, step=step)
            text_to_send = status_text(state)
        try:
            telegram.send_chat_action(chat_id)
            if state.status_message_id:
                telegram.edit_message_text(chat_id, state.status_message_id, text_to_send)
        except Exception as exc:
            print(f"telegram status update failed: {exc}", flush=True)

    def stream_update(partial: str, *, force: bool = False) -> None:
        nonlocal draft_failed, last_draft_at, last_stream_text, stream_message_id
        if not partial or not stream_drafts or draft_failed:
            return
        now = time.monotonic()
        with stream_lock:
            if partial == last_stream_text:
                return
            if not force and now - last_draft_at < stream_draft_interval_seconds:
                return
            last_stream_text = partial
            last_draft_at = now
            text_to_send = partial[-TELEGRAM_MESSAGE_LIMIT:].strip()
        try:
            if stream_message_id:
                telegram.edit_message_text(chat_id, stream_message_id, text_to_send)
            else:
                sent = telegram.send_message(chat_id, text_to_send)
                message = sent[0].get("result", {}) if sent else {}
                stream_message_id = int(message.get("message_id", 0) or 0) or None
        except Exception as exc:
            draft_failed = True
            print(f"telegram streaming disabled for this reply: {exc}", flush=True)

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
            message = STATUS_HEARTBEAT_MESSAGES[beat % len(STATUS_HEARTBEAT_MESSAGES)]
            beat += 1
            progress(f"{message} ({elapsed}s elapsed.)")

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name=f"telegram-status-{chat_id}",
        daemon=True,
    )

    failed = False
    try:
        if show_status:
            heartbeat_thread.start()
        progress("Waiting for the agent response.")
        answer = answer_message(
            agent,
            states,
            chat_id,
            text,
            progress=progress,
            stream_callback=(lambda _partial: None) if state.verbose else stream_update,
            raw_stream_callback=raw_stream_update if state.verbose else None,
        )
        stream_update(answer, force=True)
        if stream_message_id:
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
        if state.status_message_id:
            try:
                with status_lock:
                    final_status = status_text(state)
                telegram.edit_message_text(
                    chat_id,
                    state.status_message_id,
                    final_status + ("\n\nStopped with an error." if failed else "\n\nDone."),
                )
            except Exception as exc:
                print(f"telegram final status update failed: {exc}", flush=True)
        clear_chat_status(state)


def run_telegram_agent() -> int:
    config = TelegramAgentConfig.from_env()
    telegram = TelegramApi(config.bot_token, request_timeout=config.request_timeout)
    agent = NytwSubconsciousAgent.from_env()
    states: dict[int, ChatState] = {}

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
                parsed = message_text(update)
                if not parsed:
                    continue

                chat_id, _message_id, text = parsed
                if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
                    continue

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
                    )
                except Exception as exc:
                    reply = f"Sorry, I hit an error while answering: {exc}"

                state = states.get(chat_id)
                if reply and not (state and state.final_reply_sent):
                    telegram.send_message(chat_id, reply)
                if state:
                    state.final_reply_sent = False
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
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
