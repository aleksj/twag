import os
import urllib.error
from unittest.mock import patch

from twag_clickhouse.telegram_agent import (
    SUBJECTIVE_QUESTION_REPLY,
    TelegramAgentConfig,
    TelegramApi,
    TelegramTransientError,
    answer_message,
    is_subjective_question,
    message_text,
    split_telegram_message,
)


def test_message_text_extracts_chat_id_and_text():
    update = {
        "message": {
            "message_id": 42,
            "chat": {"id": 123},
            "text": "  list AI events  ",
        }
    }

    assert message_text(update) == (123, 42, "list AI events")


def test_message_text_ignores_non_text_updates():
    assert message_text({"message": {"chat": {"id": 123}, "photo": []}}) is None


def test_split_telegram_message_keeps_short_text_intact():
    assert split_telegram_message("hello") == ["hello"]


def test_split_telegram_message_splits_long_text():
    parts = split_telegram_message("a" * 5000)

    assert len(parts) == 2
    assert "".join(parts) == "a" * 5000


def test_answer_message_returns_greeting_for_start():
    class Agent:
        def ask(self, question):
            raise AssertionError("agent should not be called for /start")

    assert answer_message(Agent(), {}, 123, "/start") == (
        "Hi, I'm a bot that answers from Senso by default and uses ClickHouse for TechWeek NY event data."
    )


def test_subjective_question_detection():
    assert is_subjective_question("what is the best event?")
    assert is_subjective_question("what should I do tonight?")
    assert not is_subjective_question("which neighborhoods have the most events?")


def test_answer_message_ridicules_subjective_questions_without_agent_call():
    class Agent:
        def ask(self, question):
            raise AssertionError("agent should not be called for subjective questions")

    assert answer_message(Agent(), {}, 123, "best event?") == SUBJECTIVE_QUESTION_REPLY


def test_telegram_config_reads_retry_settings():
    env = {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_POLL_TIMEOUT": "20",
        "TELEGRAM_REQUEST_TIMEOUT": "35",
        "TELEGRAM_RETRY_INITIAL_SECONDS": "3",
        "TELEGRAM_RETRY_MAX_SECONDS": "30",
    }

    with patch.dict(os.environ, env, clear=True):
        config = TelegramAgentConfig.from_env()

    assert config.poll_timeout == 20
    assert config.request_timeout == 35
    assert config.retry_initial == 3
    assert config.retry_max == 30


def test_telegram_api_timeout_is_transient():
    api = TelegramApi("token", request_timeout=1)

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(TimeoutError("Operation timed out")),
    ):
        try:
            api.request("getMe")
        except TelegramTransientError as exc:
            assert "timed out" in str(exc)
        else:
            raise AssertionError("expected TelegramTransientError")
