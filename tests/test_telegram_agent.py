import json
import os
import time
import urllib.error
from unittest.mock import patch
from urllib.parse import parse_qs

from twag_clickhouse.telegram_agent import (
    ChatState,
    GREETING_REPLY,
    HELP_REPLY,
    QuestionLogWriter,
    SUBJECTIVE_QUESTION_REPLY,
    TelegramAgentConfig,
    TelegramApi,
    TelegramRateLimitError,
    TelegramMessageContext,
    TelegramTransientError,
    TELEGRAM_STREAM_RESET_CHARS,
    TokenUsageAccumulator,
    answer_message,
    answer_message_with_status,
    is_subjective_question,
    message_context,
    message_text,
    send_final_reply_with_rate_limit_retry,
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


def test_message_context_extracts_user_and_chat_metadata():
    update = {
        "update_id": 7,
        "message": {
            "message_id": 42,
            "from": {
                "id": 456,
                "is_bot": False,
                "username": "ada",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "language_code": "en",
            },
            "chat": {
                "id": 123,
                "type": "private",
                "username": "ada",
            },
            "text": "  list AI events  ",
        },
    }

    context = message_context(update)

    assert context is not None
    assert context.update_id == 7
    assert context.chat_id == 123
    assert context.user_id == 456
    assert context.username == "ada"
    assert context.first_name == "Ada"
    assert context.text == "list AI events"


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

    assert answer_message(Agent(), {}, 123, "/start") == GREETING_REPLY
    assert GREETING_REPLY == HELP_REPLY
    assert "**Sponsored by data.flowers**" in GREETING_REPLY
    assert "List AI events in SoHo" in GREETING_REPLY
    assert "Use concrete criteria" in GREETING_REPLY


def test_answer_message_supports_help_verbose_and_quiet_commands():
    class Agent:
        def ask(self, question):
            raise AssertionError("agent should not be called for commands")

    states = {123: ChatState()}

    assert answer_message(Agent(), states, 123, "/help") == HELP_REPLY
    assert answer_message(Agent(), states, 123, "/verbose@Twagbot").startswith(
        "Verbose mode is on"
    )
    assert states[123].verbose is True
    assert answer_message(Agent(), states, 123, "/quiet").startswith("Quiet mode is on")
    assert states[123].verbose is False
    assert "/help" in HELP_REPLY
    assert "/verbose" in HELP_REPLY
    assert "/quiet" in HELP_REPLY


def test_subjective_question_detection():
    assert is_subjective_question("what is the best event?")
    assert is_subjective_question("what should I do tonight?")
    assert not is_subjective_question("which neighborhoods have the most events?")


def test_answer_message_ridicules_subjective_questions_without_agent_call():
    class Agent:
        def ask(self, question):
            raise AssertionError("agent should not be called for subjective questions")

    assert answer_message(Agent(), {}, 123, "best event?") == SUBJECTIVE_QUESTION_REPLY
    assert "open RSVPs" in SUBJECTIVE_QUESTION_REPLY


def test_answer_message_with_status_sends_progress_updates():
    class Agent:
        def __init__(self):
            self.questions = []

        def ask(self, question, **kwargs):
            self.questions.append((question, kwargs))
            return "Here are the events."

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": 99}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

        def send_message_draft(self, chat_id, draft_id, text):
            pass

    telegram = Telegram()
    agent = Agent()
    states = {123: ChatState(verbose=True)}

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
        states=states,
        chat_id=123,
        text="events in upper west side?",
    )

    assert answer == "Here are the events."
    assert agent.questions[0][0] == "events in upper west side?"
    assert callable(agent.questions[0][1]["stream_callback"])
    assert callable(agent.questions[0][1]["progress_callback"])
    assert "Route: ClickHouse event search" in telegram.sent[0][1]
    assert any("NY Tech Week search pipeline" in edit[2] for edit in telegram.edits)
    assert telegram.edits[-1][2].endswith("Done.")
    assert states[123].active_question is None
    assert states[123].status_message_id is None
    assert states[123].final_reply_sent is True


def test_answer_message_with_status_shows_agent_search_stages():
    class Agent:
        def ask(self, question, **kwargs):
            progress = kwargs["progress_callback"]
            progress("Expanded search terms: mathematics.")
            progress(
                "Building a ranked event query across titles, descriptions, "
                "hosts, venues, and neighborhoods."
            )
            progress("ClickHouse returned 3 candidate rows; formatting 3 results.")
            return "Here are the math events."

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": 99}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

    telegram = Telegram()

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states={123: ChatState(verbose=True)},
        chat_id=123,
        text="Need events about mathematics",
    )

    status_updates = "\n".join(edit[2] for edit in telegram.edits)
    assert answer == "Here are the math events."
    assert "Expanded search terms: mathematics." in status_updates
    assert "Building a ranked event query" in status_updates
    assert "ClickHouse returned 3 candidate rows" in status_updates


def test_answer_message_with_status_skips_progress_for_subjective_questions():
    class Agent:
        def ask(self, question, **kwargs):
            raise AssertionError("agent should not be called")

    class Telegram:
        def send_message(self, chat_id, text):
            raise AssertionError("status should not be sent")

    answer = answer_message_with_status(
        telegram=Telegram(),  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states={},
        chat_id=123,
        text="best event?",
    )

    assert answer == SUBJECTIVE_QUESTION_REPLY


def test_answer_message_with_status_heartbeats_during_slow_work():
    class Agent:
        def ask(self, question, **kwargs):
            time.sleep(0.035)
            return "Done."

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": 100}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

    telegram = Telegram()

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states={123: ChatState(verbose=True)},
        chat_id=123,
        text="events in upper west side?",
        status_heartbeat_seconds=0.01,
    )

    assert answer == "Done."
    assert any(
        "current step" in edit[2] or "Still on this step" in edit[2]
        for edit in telegram.edits
    )
    assert not any("waiting on the backend" in edit[2] for edit in telegram.edits)
    assert len(telegram.actions) >= 2


def test_answer_message_with_status_streams_telegram_drafts():
    class Agent:
        def ask(self, question, **kwargs):
            kwargs["stream_callback"]("Partial")
            kwargs["stream_callback"]("Partial answer")
            return "Partial answer"

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []
            self.drafts = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": 101}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

        def send_message_draft(self, chat_id, draft_id, text):
            self.drafts.append((chat_id, draft_id, text))

    telegram = Telegram()
    states = {}

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states=states,
        chat_id=123,
        text="how many events in soho?",
        stream_drafts=True,
        stream_draft_interval_seconds=0,
    )

    assert answer == "Partial answer"
    assert [sent[1] for sent in telegram.sent] == ["Partial"]
    assert telegram.edits[-1][2] == "Partial answer"
    assert states[123].final_reply_sent is True


def test_long_streamed_final_answer_is_not_marked_sent():
    long_answer = "A" * 5000

    class Agent:
        def ask(self, question, **kwargs):
            kwargs["stream_callback"]("Partial")
            return long_answer

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": 101}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

    telegram = Telegram()
    states = {}

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states=states,
        chat_id=123,
        text="how many events in soho?",
        stream_drafts=True,
        stream_draft_interval_seconds=0,
    )

    assert answer == long_answer
    assert len(telegram.sent[-1][1]) == TELEGRAM_STREAM_RESET_CHARS
    assert states[123].final_reply_sent is False


def test_long_streamed_final_answer_would_be_sent_by_splitter():
    long_answer = "A" * 5000

    class Agent:
        def ask(self, question, **kwargs):
            kwargs["stream_callback"]("Partial")
            return long_answer

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": len(self.sent)}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

    telegram = Telegram()
    states = {}
    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states=states,
        chat_id=123,
        text="how many events in soho?",
        stream_drafts=True,
        stream_draft_interval_seconds=0,
    )
    state = states[123]
    if answer and not state.final_reply_sent:
        for part in split_telegram_message(answer):
            telegram.send_message(123, part)

    assert [len(sent[1]) for sent in telegram.sent] == [
        7,
        TELEGRAM_STREAM_RESET_CHARS,
        4096,
        904,
    ]


def test_verbose_stream_resets_old_reasoning_to_ellipsis_before_limit():
    first = "A" * 3500
    second = "B" * 200

    class Agent:
        def ask(self, question, **kwargs):
            kwargs["raw_stream_callback"](first)
            kwargs["raw_stream_callback"](second)
            return "Final answer"

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": len(self.sent)}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

    telegram = Telegram()
    states = {123: ChatState(verbose=True)}

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states=states,
        chat_id=123,
        text="fashion model events?",
        stream_draft_interval_seconds=0,
    )

    assert answer == "Final answer"
    stream_edits = [edit for edit in telegram.edits if edit[1] in {2, 3}]
    assert (123, 2, "...") in stream_edits
    assert telegram.sent[2][1] == second


def test_verbose_mode_streams_raw_thinking_text():
    class Agent:
        def ask(self, question, **kwargs):
            kwargs["raw_stream_callback"]("<think>plan")
            kwargs["raw_stream_callback"]("</think>Answer")
            return "Answer"

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": len(self.sent)}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

    telegram = Telegram()
    states = {123: ChatState(verbose=True)}

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states=states,
        chat_id=123,
        text="how many events in soho?",
        stream_draft_interval_seconds=0,
    )

    assert answer == "Answer"
    stream_edits = [edit for edit in telegram.edits if edit[1] == 2]
    assert stream_edits[-1][2] == "<think>plan</think>Answer"


def test_verbose_mode_final_reply_uses_full_continued_answer():
    long_reasoning = "<think>" + ("plan " * 900) + "</think>"

    class Agent:
        def ask(self, question, **kwargs):
            kwargs["raw_stream_callback"](long_reasoning + "First half")
            kwargs["raw_stream_callback"](" second half")
            return "First half second half"

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = []
            self.actions = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": len(self.sent)}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((chat_id, message_id, text))

        def send_chat_action(self, chat_id, action="typing"):
            self.actions.append((chat_id, action))

    telegram = Telegram()
    states = {123: ChatState(verbose=True)}

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states=states,
        chat_id=123,
        text="fashion model events?",
        stream_draft_interval_seconds=0,
    )
    state = states[123]
    if answer and not state.final_reply_sent:
        for part in split_telegram_message(answer):
            telegram.send_message(123, part)

    assert answer == "First half second half"
    assert state.final_reply_sent is False
    assert telegram.sent[-1][1] == "First half second half"


def test_telegram_config_reads_retry_settings():
    env = {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_POLL_TIMEOUT": "20",
        "TELEGRAM_REQUEST_TIMEOUT": "35",
        "TELEGRAM_RETRY_INITIAL_SECONDS": "3",
        "TELEGRAM_RETRY_MAX_SECONDS": "30",
        "TELEGRAM_STATUS_HEARTBEAT_SECONDS": "4",
        "TELEGRAM_STREAM_DRAFTS": "false",
        "TELEGRAM_STREAM_DRAFT_INTERVAL_SECONDS": "0.5",
        "TELEGRAM_WARM_CLICKHOUSE_ON_START": "false",
        "TELEGRAM_QUESTION_LOG_PATH": "/tmp/twag-questions.jsonl",
    }

    with patch.dict(os.environ, env, clear=True):
        config = TelegramAgentConfig.from_env()

    assert config.poll_timeout == 20
    assert config.request_timeout == 35
    assert config.retry_initial == 3
    assert config.retry_max == 30
    assert config.status_heartbeat_seconds == 4
    assert config.stream_drafts is False
    assert config.stream_draft_interval_seconds == 0.5
    assert config.warm_clickhouse_on_start is False
    assert config.question_log_path == "/tmp/twag-questions.jsonl"


def test_token_usage_accumulator_sums_openai_and_alt_token_fields():
    usage = TokenUsageAccumulator()

    usage.add({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    usage.add({"input_tokens": 3, "output_tokens": 2})

    assert usage.as_dict()["calls"] == 2
    assert usage.as_dict()["prompt_tokens"] == 13
    assert usage.as_dict()["completion_tokens"] == 7
    assert usage.as_dict()["total_tokens"] == 20


def test_question_log_writer_appends_jsonl(tmp_path):
    path = tmp_path / "questions.jsonl"
    writer = QuestionLogWriter(str(path))
    context = TelegramMessageContext(
        update_id=7,
        message_id=42,
        chat_id=123,
        chat_type="private",
        chat_title="",
        chat_username="ada",
        user_id=456,
        is_bot=False,
        username="ada",
        first_name="Ada",
        last_name="Lovelace",
        language_code="en",
        text="List AI events",
    )

    writer.write(
        context,
        route="clickhouse-event-search",
        answer="Answer",
        ok=True,
        error=None,
        duration_ms=12,
        token_usage={
            "calls": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "raw": [],
        },
    )

    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["question"] == "List AI events"
    assert record["user"]["id"] == 456
    assert record["chat"]["id"] == 123
    assert record["token_usage"]["total_tokens"] == 15


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


def test_telegram_api_raises_rate_limit_with_retry_after():
    api = TelegramApi("token", request_timeout=1)

    class Response:
        pass

    error = urllib.error.HTTPError(
        url="https://api.telegram.org/bottoken/getUpdates",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=None,
    )
    error.read = lambda: (
        b'{"ok":false,"error_code":429,"description":"Too Many Requests",'
        b'"parameters":{"retry_after":164}}'
    )

    with patch("urllib.request.urlopen", side_effect=error):
        try:
            api.request("getUpdates")
        except TelegramRateLimitError as exc:
            assert exc.retry_after == 164
        else:
            raise AssertionError("expected TelegramRateLimitError")


def test_status_rate_limit_suppresses_optional_updates():
    class Agent:
        def ask(self, question, **kwargs):
            progress = kwargs["progress_callback"]
            progress("Expanded search terms: mathematics.")
            progress("ClickHouse returned 2 candidate rows; formatting 2 results.")
            return "Math answer"

    class Telegram:
        def __init__(self):
            self.sent = []
            self.edits = 0
            self.actions = 0

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return [{"result": {"message_id": 99}}]

        def edit_message_text(self, chat_id, message_id, text):
            self.edits += 1
            raise TelegramRateLimitError(164, "too many edits")

        def send_chat_action(self, chat_id, action="typing"):
            self.actions += 1

    telegram = Telegram()

    answer = answer_message_with_status(
        telegram=telegram,  # type: ignore[arg-type]
        agent=Agent(),  # type: ignore[arg-type]
        states={123: ChatState(verbose=True)},
        chat_id=123,
        text="Need events about mathematics",
    )

    assert answer == "Math answer"
    assert telegram.edits == 1
    assert len(telegram.sent) == 1


def test_send_final_reply_retries_once_after_rate_limit():
    class Telegram:
        def __init__(self):
            self.calls = 0

        def send_message(self, chat_id, text):
            self.calls += 1
            if self.calls == 1:
                raise TelegramRateLimitError(2, "too many sends")
            return [{"result": {"message_id": self.calls}}]

    telegram = Telegram()

    with patch("time.sleep") as sleep:
        send_final_reply_with_rate_limit_retry(
            telegram,  # type: ignore[arg-type]
            123,
            "Final answer",
        )

    sleep.assert_called_once_with(3)
    assert telegram.calls == 2


def test_telegram_api_sends_html_parse_mode_for_markdown_answers():
    api = TelegramApi("token", request_timeout=1)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true, "result": {"message_id": 1}}'

    def fake_urlopen(request, timeout):
        captured["data"] = request.data.decode("utf-8")
        return Response()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        api.send_message(123, "**AI & Agents** — use `more`")

    payload = {key: values[0] for key, values in parse_qs(captured["data"]).items()}
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>AI &amp; Agents</b> — use <code>more</code>"
