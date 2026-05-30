from __future__ import annotations

import os

from unittest.mock import patch

from twag_clickhouse.chat_session import (
    ChatState,
    active_city_override,
    answer_session_message,
    help_reply,
    infer_date,
    map_command_query,
    map_command_reply,
    gallery_page_url_for,
    map_page_url_for,
    map_url_for,
)
from twag_clickhouse.city import active_city


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
    assert states["local"].thinking_enabled is True
    assert answer_session_message(Agent(), states, "local", "/quiet").startswith(
        "Quiet mode is on"
    )
    assert states["local"].verbose is False
    assert states["local"].thinking_enabled is False


def test_answer_session_message_supports_city_switching() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            return f"{active_city().slug}: {question}"

    states = {"local": ChatState()}

    with patch.dict("os.environ", {"TWAG_CITY": "nyc"}, clear=True):
        assert answer_session_message(Agent(), states, "local", "/city") == (
            "Current city is NY Tech Week. Use `/city nyc` or `/city boston` to switch."
        )
        assert answer_session_message(Agent(), states, "local", "/city boston") == (
            "Switched to Boston Tech Week."
        )
        assert states["local"].city == "boston"
        assert "TWAG Boston Tech Week Bot" in answer_session_message(
            Agent(), states, "local", "/help"
        )
        assert (
            answer_session_message(Agent(), states, "local", "AI events")
            == "boston: AI events"
        )
        assert os.environ["TWAG_CITY"] == "nyc"

        assert answer_session_message(Agent(), states, "local", "/city nyc") == (
            "Switched to NY Tech Week."
        )
        assert (
            answer_session_message(Agent(), states, "local", "AI events")
            == "nyc: AI events"
        )


def test_infer_date_uses_current_local_date_for_relative_terms(monkeypatch) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-28")

    assert infer_date("tomorrow", "2026-06-02") == "2026-05-29"
    assert infer_date("today", "2026-06-02") == "2026-05-28"
    assert infer_date("tonight", "2026-06-02") == "2026-05-28"
    assert infer_date("friday", "2026-06-02") == "2026-05-29"
    assert infer_date("thursday", "2026-06-02") == "2026-05-28"


def test_map_command_query_distinguishes_dates_from_searches(monkeypatch) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-28")

    assert map_command_query("/map") == ""
    assert map_command_query("/map 2026-06-03") == ""
    assert map_command_query("/map June 3") == ""
    assert map_command_query("/map tomorrow") == ""
    assert map_command_query("/map events at Columbia University") == (
        "events at Columbia University"
    )
    assert map_command_query("/map events at Columbia University tomorrow") == (
        "events at Columbia University"
    )
    assert map_command_query("/map Columbia University on June 3") == (
        "Columbia University"
    )
    assert map_command_query("events at Columbia University") == ""


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


def test_answer_session_message_tracks_more_for_topic_event_queries() -> None:
    class Agent:
        def __init__(self) -> None:
            self.calls = []

        def ask(self, question, **kwargs):
            self.calls.append((question, kwargs.get("event_offset", 0)))
            return (
                f"**The Future of AI Personalization** — 2026-06-04, 4:00pm ET "
                f"— Murray Hill — {question} @ {kwargs.get('event_offset', 0)} "
                "— https://partiful.com/e/example"
            )

    states = {"local": ChatState()}
    agent = Agent()

    assert (
        "AI personalization @ 0"
        in answer_session_message(agent, states, "local", "AI personalization")
    )
    assert (
        answer_session_message(agent, states, "local", "more")
        == (
            "**The Future of AI Personalization** — 2026-06-04, 4:00pm ET "
            "— Murray Hill — list events matching AI personalization @ 25 "
            "— https://partiful.com/e/example"
        )
    )
    assert agent.calls == [
        ("AI personalization", 0),
        ("list events matching AI personalization", 25),
    ]


def test_answer_session_message_tracks_more_when_answer_has_more_hint() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            return (
                f"{question} @ {kwargs.get('event_offset', 0)}\n\n"
                "More results are available. Send `more` for the next page."
            )

    states = {"local": ChatState()}

    answer_session_message(Agent(), states, "local", "AI personalization")

    assert (
        states["local"].conversation.last_event_question
        == "list events matching AI personalization"
    )


def test_answer_session_message_leaves_map_linking_to_terminal_handoff() -> None:
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

    assert answer == "top 3 AI events on May 27 @ 0"
    assert more == "top 3 AI events on May 27 @ 3"


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


def test_public_page_urls_use_current_city() -> None:
    with patch.dict(
        "os.environ",
        {"TWAG_CITY": "nyc", "TWAG_PUBLIC_MAP_BASE_URL": "https://natea.github.io/twag/"},
        clear=True,
    ):
        assert map_page_url_for() == "https://natea.github.io/twag/events_map_nyc.html"
        assert (
            gallery_page_url_for()
            == "https://natea.github.io/twag/events_gallery_nyc.html"
        )

    with patch.dict(
        "os.environ",
        {
            "TWAG_CITY": "boston",
            "TWAG_PUBLIC_MAP_BASE_URL": "https://natea.github.io/twag/events_map_nyc.html",
        },
        clear=True,
    ):
        assert map_page_url_for() == "https://natea.github.io/twag/events_map_boston.html"
        assert (
            gallery_page_url_for()
            == "https://natea.github.io/twag/events_gallery_boston.html"
        )
