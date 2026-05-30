from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from .chat_session import (
    GREETING_REPLY,
    HELP_REPLY,
    SUBJECTIVE_QUESTION_REPLY,
    TELEGRAM_PRESENTATION,
    ChatState,
    TokenUsageAccumulator,
    active_city_override,
    answer_route,
    answer_session_message,
    chat_command as telegram_command,
    clear_chat_status,
    is_subjective_question,
    question_log_route,
    status_text,
    update_chat_status,
)
from .city import CITIES, active_city
from .rendering import markdown_to_telegram_html
from .subconscious_agent import NytwSubconsciousAgent
from .client import CLICKHOUSE_HTTP_LOGGER


TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_STATUS_HEARTBEAT_SECONDS = 8.0
DEFAULT_STREAM_DRAFT_INTERVAL_SECONDS = 1.0
TELEGRAM_STREAM_RESET_CHARS = 3600
DEFAULT_QUESTION_LOG_PATH = "logs/twag-telegram-questions.jsonl"
STATUS_HEARTBEAT_TEMPLATES = (
    "Still on this step: {step}",
    "Keeping Telegram warm while this runs: {step}",
    "No final answer yet; current step is still: {step}",
)


@dataclass(frozen=True)
class TelegramBotEndpoint:
    token: str
    default_city: str
    source_env: str


def _city_token_env(slug: str) -> str:
    return f"{slug.upper()}_TELEGRAM_BOT_TOKEN"


def _resolve_bot_endpoints() -> list[TelegramBotEndpoint]:
    """Pick Telegram bot tokens for the unified or city-specific bot accounts."""

    generic_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if generic_token:
        return [
            TelegramBotEndpoint(
                token=generic_token,
                default_city=active_city().slug,
                source_env="TELEGRAM_BOT_TOKEN",
            )
        ]

    endpoints: list[TelegramBotEndpoint] = []
    for slug in sorted(CITIES):
        env_name = _city_token_env(slug)
        token = os.getenv(env_name, "").strip()
        if token:
            endpoints.append(
                TelegramBotEndpoint(
                    token=token,
                    default_city=slug,
                    source_env=env_name,
                )
            )
    if endpoints:
        return endpoints

    city = active_city()
    env_name = _city_token_env(city.slug)
    raise ValueError(
        "No Telegram bot token found. Set TELEGRAM_BOT_TOKEN or one or more "
        f"city-specific tokens such as {env_name}."
    )


def _resolve_bot_token() -> str:
    """Backward-compatible helper for callers that only need the first token."""
    return _resolve_bot_endpoints()[0].token


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
    bot_endpoints: list[TelegramBotEndpoint]
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

    @property
    def bot_token(self) -> str:
        return self.bot_endpoints[0].token

    @classmethod
    def from_env(cls) -> "TelegramAgentConfig":
        bot_endpoints = _resolve_bot_endpoints()

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
            bot_endpoints=bot_endpoints,
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


@dataclass
class TelegramBotRuntime:
    endpoint: TelegramBotEndpoint
    telegram: TelegramApi
    states: dict[int, ChatState] = field(default_factory=dict)
    offset: int | None = None
    retry_delay: float = 2.0
    next_poll_at: float = 0.0


def _chat_state_for_runtime(runtime: TelegramBotRuntime, chat_id: int) -> ChatState:
    state = runtime.states.setdefault(
        chat_id,
        ChatState(city=runtime.endpoint.default_city),
    )
    if state.city is None:
        state.city = runtime.endpoint.default_city
    return state


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


def status_heartbeat_text(state: ChatState, *, beat: int, elapsed: int) -> str:
    step = state.active_steps[-1] if state.active_steps else "working through the search pipeline"
    template = STATUS_HEARTBEAT_TEMPLATES[beat % len(STATUS_HEARTBEAT_TEMPLATES)]
    return f"{template.format(step=step)} ({elapsed}s elapsed.)"


def answer_message(
    agent: NytwSubconsciousAgent,
    states: dict[int, ChatState],
    chat_id: int,
    text: str,
    progress: Any | None = None,
    stream_callback: Any | None = None,
    raw_stream_callback: Any | None = None,
    enable_thinking: bool | None = None,
    token_usage_callback: Any | None = None,
) -> str:
    state = states.setdefault(chat_id, ChatState())
    thinking = state.thinking_enabled if enable_thinking is None else enable_thinking
    return answer_session_message(
        agent,
        states,
        chat_id,
        text,
        progress=progress,
        stream_callback=stream_callback,
        raw_stream_callback=raw_stream_callback if thinking else None,
        enable_thinking=thinking,
        token_usage_callback=token_usage_callback,
        presentation=TELEGRAM_PRESENTATION,
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

    city_context = active_city_override(state.city) if state.city else nullcontext()
    with city_context:
        show_status = state.verbose
        if show_status:
            route, first_step = answer_route(text, state)
            update_chat_status(
                state, question=text, route=route, step="Received your message."
            )
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
            raw_stream_callback=(
                raw_stream_update if state.verbose and state.thinking_enabled else None
            ),
            enable_thinking=state.thinking_enabled,
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
    runtimes = [
        TelegramBotRuntime(
            endpoint=endpoint,
            telegram=TelegramApi(endpoint.token, request_timeout=config.request_timeout),
            retry_delay=config.retry_initial,
        )
        for endpoint in config.bot_endpoints
    ]
    poll_timeout = config.poll_timeout if len(runtimes) == 1 else min(config.poll_timeout, 5)
    agent = NytwSubconsciousAgent.from_env()
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
        f"bots={[(runtime.endpoint.source_env, runtime.endpoint.default_city) for runtime in runtimes]} "
        f"module={__file__} "
        f"clickhouse_filters={[type(filter_).__name__ for filter_ in clickhouse_logger.filters]} "
        "Press Ctrl+C to stop.",
        flush=True,
    )

    if config.clear_webhook_on_start:
        for runtime in runtimes:
            try:
                runtime.telegram.delete_webhook()
            except TelegramRateLimitError as exc:
                print(
                    "telegram deleteWebhook rate limited "
                    f"for {runtime.endpoint.source_env}; "
                    f"continuing to poll after Telegram cooldown ({exc.retry_after}s)",
                    flush=True,
                )
                runtime.next_poll_at = time.monotonic() + exc.retry_after + 1
            except TelegramTransientError as exc:
                print(
                    "telegram deleteWebhook transient error "
                    f"for {runtime.endpoint.source_env}: {exc}; continuing to poll",
                    flush=True,
                )

    while True:
        polled_any = False
        for runtime in runtimes:
            if time.monotonic() < runtime.next_poll_at:
                continue
            polled_any = True
            try:
                updates = runtime.telegram.get_updates(
                    offset=runtime.offset,
                    timeout=poll_timeout,
                )
                runtime.retry_delay = config.retry_initial
                for update in updates:
                    runtime.offset = int(update["update_id"]) + 1
                    context = message_context(update)
                    if context is None:
                        continue

                    chat_id = context.chat_id
                    text = context.text
                    if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
                        continue

                    _chat_state_for_runtime(runtime, chat_id)

                    started_at = time.monotonic()
                    usage = TokenUsageAccumulator()
                    error: str | None = None
                    reply = ""
                    try:
                        reply = answer_message_with_status(
                            telegram=runtime.telegram,
                            agent=agent,
                            states=runtime.states,
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

                    state = runtime.states.get(chat_id)
                    if reply and not (state and state.final_reply_sent):
                        send_final_reply_with_rate_limit_retry(
                            runtime.telegram,
                            chat_id,
                            reply,
                        )
                    if state:
                        state.final_reply_sent = False
            except KeyboardInterrupt:
                print("\nStopped.")
                return 0
            except TelegramRateLimitError as exc:
                sleep_for = exc.retry_after + random.uniform(0, 1.0)
                print(
                    "telegram polling rate limited "
                    f"for {runtime.endpoint.source_env}: "
                    f"retrying after Telegram cooldown in {sleep_for:.1f}s",
                    flush=True,
                )
                runtime.next_poll_at = time.monotonic() + sleep_for
                runtime.retry_delay = config.retry_initial
            except TelegramTransientError as exc:
                sleep_for = runtime.retry_delay + random.uniform(
                    0,
                    min(1.0, runtime.retry_delay / 4),
                )
                print(
                    "telegram polling transient error "
                    f"for {runtime.endpoint.source_env}: {exc}; "
                    f"retrying in {sleep_for:.1f}s",
                    flush=True,
                )
                runtime.next_poll_at = time.monotonic() + sleep_for
                runtime.retry_delay = min(config.retry_max, runtime.retry_delay * 2)
            except Exception as exc:
                print(
                    f"telegram polling error for {runtime.endpoint.source_env}: {exc}",
                    flush=True,
                )
                runtime.next_poll_at = time.monotonic() + 5
        if not polled_any:
            time.sleep(0.2)


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
