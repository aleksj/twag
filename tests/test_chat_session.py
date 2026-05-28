from __future__ import annotations

from unittest.mock import patch

from twag_clickhouse.chat_session import (
    ChatState,
    active_city_override,
    answer_session_message,
    help_reply,
    map_command_reply,
    map_url_for,
)


def test_answer_session_message_supports_commands_without_agent_call() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            raise AssertionError("agent should not be called for commands")

    states = {"local": ChatState()}

    assert answer_session_message(Agent(), states, "local", "/help") == help_reply()
    assert answer_session_message(Agent(), states, "local", "/verbose").startswith(
        "Verbose mode is on"
    )
    assert states["local"].verbose is True
    assert answer_session_message(Agent(), states, "local", "/quiet").startswith(
        "Quiet mode is on"
    )
    assert states["local"].verbose is False


def test_answer_session_message_tracks_more_per_session() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            return f"{question} @ {kwargs.get('event_offset', 0)}"

    states = {"local": ChatState()}

    assert (
        answer_session_message(Agent(), states, "local", "top 3 AI events")
        == "top 3 AI events @ 0"
    )
    assert answer_session_message(Agent(), states, "local", "more") == "top 3 AI events @ 3"


def test_answer_session_message_appends_event_map_links_from_plan_url_pattern() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            return f"{question} @ {kwargs.get('event_offset', 0)}"

    env = {
        "TWAG_CITY": "boston",
        "TWAG_PUBLIC_MAP_BASE_URL": "https://natea.github.io/twag/",
    }
    states = {"local": ChatState()}

    with patch.dict("os.environ", env, clear=True):
        answer = answer_session_message(
            Agent(),
            states,
            "local",
            "top 3 AI events on May 27",
        )
        more = answer_session_message(Agent(), states, "local", "more")

    expected_map = (
        "[View on the map]"
        "(https://natea.github.io/twag/events_map_boston.html#date=2026-05-27)"
    )
    assert answer == f"top 3 AI events on May 27 @ 0\n\n{expected_map}"
    assert more == f"top 3 AI events on May 27 @ 3\n\n{expected_map}"


def test_answer_session_message_hides_agent_configuration_errors() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            raise ValueError("SUBCONSCIOUS_API_KEY is required")

    states = {"local": ChatState()}

    answer = answer_session_message(
        Agent(),
        states,
        "local",
        "how many AI events are in Cambridge?",
    )

    assert "SUBCONSCIOUS_API_KEY" not in answer
    assert "backend" not in answer.lower()
    assert "credentials" not in answer.lower()
    assert "TWAG search is unavailable" in answer


def test_answer_session_message_hides_configuration_errors_for_more() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            raise ValueError("CLICKHOUSE_HOST is required")

    state = ChatState()
    state.conversation.last_event_question = "top 3 AI events"
    states = {"local": state}

    answer = answer_session_message(Agent(), states, "local", "more")

    assert "CLICKHOUSE_HOST" not in answer
    assert "TWAG search is unavailable" in answer


def test_active_city_override_restores_previous_city() -> None:
    with patch.dict("os.environ", {"TWAG_CITY": "nyc"}):
        with active_city_override("boston"):
            assert help_reply().startswith("**TWAG Boston Tech Week Bot**")
        assert help_reply().startswith("**TWAG NY Tech Week Bot**")


# (test removed)

def test_map_command_reply_uses_configured_public_base_url() -> None:
    env = {
        "TWAG_CITY": "boston",
        "TWAG_PUBLIC_MAP_BASE_URL": "https://example.test/maps",
    }
    with patch.dict("os.environ", env, clear=True):
        reply = map_command_reply("/map May 27")

    assert "Boston Tech Week map for 2026-05-27" in reply
    assert "https://example.test/maps/events_map_boston.html#date=2026-05-27" in reply


def test_map_url_uses_readme_github_pages_base_by_city() -> None:
    with patch.dict(
        "os.environ",
        {"TWAG_CITY": "nyc", "TWAG_PUBLIC_MAP_BASE_URL": "https://natea.github.io/twag/"},
        clear=True,
    ):
        assert (
            map_url_for("2026-06-03")
            == "https://natea.github.io/twag/events_map_nyc.html#date=2026-06-03"
        )

    with patch.dict(
        "os.environ",
        {
            "TWAG_CITY": "boston",
            "TWAG_PUBLIC_MAP_BASE_URL": "https://natea.github.io/twag/",
        },
        clear=True,
    ):
        assert (
            map_url_for("2026-05-27")
            == "https://natea.github.io/twag/events_map_boston.html#date=2026-05-27"
        )


def test_map_url_tolerates_full_map_html_env_and_stays_city_contextual() -> None:
    env = {
        "TWAG_CITY": "boston",
        "TWAG_PUBLIC_MAP_BASE_URL": "https://natea.github.io/twag/events_map_nyc.html",
    }

    with patch.dict("os.environ", env, clear=True):
        assert (
            map_url_for("2026-05-27")
            == "https://natea.github.io/twag/events_map_boston.html#date=2026-05-27"
        )
