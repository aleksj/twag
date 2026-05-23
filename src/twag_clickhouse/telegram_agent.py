from __future__ import annotations

import json
import os
import pathlib
import random
import re
import socket
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

from .subconscious_agent import (
    NytwSubconsciousAgent,
    is_more_results_request,
    likely_event_list_question,
    requested_event_limit,
)


TELEGRAM_MESSAGE_LIMIT = 4096
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
    "Try: 'List AI events in SoHo on June 3' or 'Show cybersecurity events with RSVP links.'"
)


@dataclass
class ChatState:
    last_event_question: str | None = None
    last_event_offset: int = 0


@dataclass
class TelegramAgentConfig:
    bot_token: str
    poll_timeout: int = 30
    request_timeout: int = 45
    retry_initial: float = 2.0
    retry_max: float = 60.0
    clear_webhook_on_start: bool = True
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
            clear_webhook_on_start=os.getenv(
                "TELEGRAM_CLEAR_WEBHOOK_ON_POLL",
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

    def send_message(self, chat_id: int, text: str) -> None:
        for part in split_telegram_message(text):
            self.request(
                "sendMessage",
                {
                    "chat_id": str(chat_id),
                    "text": part,
                    "disable_web_page_preview": "true",
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


def answer_message(
    agent: NytwSubconsciousAgent,
    states: dict[int, ChatState],
    chat_id: int,
    text: str,
) -> str:
    state = states.setdefault(chat_id, ChatState())

    if text.startswith("/start") or text.startswith("/help"):
        return "Hi, I'm a bot that answers from Senso by default and uses ClickHouse for TechWeek NY event data."

    if is_subjective_question(text):
        return SUBJECTIVE_QUESTION_REPLY

    if is_more_results_request(text):
        if not state.last_event_question:
            return "Ask an event-list question first, then send 'more'."
        state.last_event_offset += requested_event_limit(state.last_event_question)
        return agent.ask(state.last_event_question, event_offset=state.last_event_offset)

    answer = agent.ask(text)
    if likely_event_list_question(text):
        state.last_event_question = text
        state.last_event_offset = 0
    return answer


def run_telegram_agent() -> int:
    config = TelegramAgentConfig.from_env()
    telegram = TelegramApi(config.bot_token, request_timeout=config.request_timeout)
    agent = NytwSubconsciousAgent.from_env()
    states: dict[int, ChatState] = {}

    if config.clear_webhook_on_start:
        telegram.delete_webhook()

    print("TWAG Telegram agent is polling. Press Ctrl+C to stop.", flush=True)
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
                    reply = answer_message(agent, states, chat_id, text)
                except Exception as exc:
                    reply = f"Sorry, I hit an error while answering: {exc}"

                telegram.send_message(chat_id, reply)
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
