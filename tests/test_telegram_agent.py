from twag_clickhouse.telegram_agent import (
    SUBJECTIVE_QUESTION_REPLY,
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
        "Hi, I'm a bot that will answer questions about TechWeek NY events!"
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
