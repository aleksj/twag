from __future__ import annotations

from twag_clickhouse.terminal_server import (
    SessionCreateRequest,
    TerminalSession,
    _answer_in_thread,
    cities,
    create_session,
    terminal_token_is_valid,
    health,
    ready_event,
    root,
    terminal_asset,
    terminal_index,
)


def test_terminal_server_metadata_endpoints() -> None:
    assert root()["endpoints"]["websocket"] == "/sessions/{session_id}"
    assert root()["endpoints"]["terminal"] == "/terminal"
    assert health()["service"] == "twag-terminal-server"

    city_slugs = {city["slug"] for city in cities()["cities"]}
    assert {"nyc", "boston"} <= city_slugs


def test_terminal_server_serves_browser_terminal_assets() -> None:
    index_response = terminal_index()
    app_response = terminal_asset("app.js")
    css_response = terminal_asset("styles.css")

    assert str(index_response.path).endswith("index.html")
    assert str(app_response.path).endswith("app.js")
    assert str(css_response.path).endswith("styles.css")


def test_create_session_is_local_and_lazy() -> None:
    result = create_session(SessionCreateRequest(city="boston"))

    assert result["city"] == "boston"
    assert result["websocket"] == f"/sessions/{result['session_id']}"


def test_ready_event_includes_city_specific_telegram_greeting() -> None:
    session = TerminalSession(session_id="ready-session", city="boston")

    event = ready_event(session)

    assert event["type"] == "ready"
    assert event["city"] == "boston"
    assert "**TWAG Boston Tech Week Bot**" in event["greeting"]
    assert "**Sponsored by Data.Flowers**" in event["greeting"]
    assert "List AI events in Cambridge" in event["greeting"]
    assert "Use concrete criteria" in event["greeting"]


def test_terminal_operator_token_is_optional_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TWAG_TERMINAL_TOKEN", raising=False)

    assert terminal_token_is_valid(None) is True
    assert terminal_token_is_valid("anything") is True


def test_terminal_operator_token_validates_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("TWAG_TERMINAL_TOKEN", "secret-token")

    assert terminal_token_is_valid(None) is False
    assert terminal_token_is_valid("wrong") is False
    assert terminal_token_is_valid("secret-token") is True


def test_answer_in_thread_emits_status_and_final_events(monkeypatch) -> None:
    monkeypatch.delenv("TWAG_PUBLIC_MAP_BASE_URL", raising=False)

    class Agent:
        def ask(self, question, **kwargs):
            kwargs["progress_callback"]("Fake search step.")
            return f"answered {question}"

    session = TerminalSession(session_id="test-session", city="nyc", agent=Agent())
    events = []

    _answer_in_thread(session, "how many events in soho?", events.append)

    assert events[-1] is None
    typed_events = [event for event in events if event is not None]
    assert typed_events[0]["type"] == "status"
    assert any(event.get("step") == "Fake search step." for event in typed_events)
    assert typed_events[-1]["type"] == "final"
    assert typed_events[-1]["text"] == "answered how many events in soho?"


def test_answer_in_thread_tracks_more_with_contextual_map_link(monkeypatch) -> None:
    class Agent:
        def __init__(self) -> None:
            self.calls = []

        def ask(self, question, **kwargs):
            self.calls.append((question, kwargs.get("event_offset", 0)))
            return f"{question} @ {kwargs.get('event_offset', 0)}"

    monkeypatch.setenv("TWAG_PUBLIC_MAP_BASE_URL", "https://example.test/map/")
    agent = Agent()
    session = TerminalSession(session_id="more-session", city="nyc", agent=agent)  # type: ignore[arg-type]
    first_events = []
    more_events = []

    _answer_in_thread(session, "list events involving running", first_events.append)
    _answer_in_thread(session, "more", more_events.append)

    expected_map = (
        "[View on the map]"
        "(https://example.test/map/events_map_nyc.html#date=2026-06-02)"
    )
    first_typed = [event for event in first_events if event is not None]
    more_typed = [event for event in more_events if event is not None]
    assert first_typed[-1]["text"] == f"list events involving running @ 0\n\n{expected_map}"
    assert more_typed[-1]["text"] == f"list events involving running @ 25\n\n{expected_map}"
    assert agent.calls == [
        ("list events involving running", 0),
        ("list events involving running", 25),
    ]


def test_answer_in_thread_does_not_create_agent_for_local_commands(monkeypatch) -> None:
    monkeypatch.delenv("TWAG_PUBLIC_MAP_BASE_URL", raising=False)

    session = TerminalSession(session_id="command-session", city="nyc")
    events = []

    _answer_in_thread(session, "/map", events.append)

    typed_events = [event for event in events if event is not None]
    assert session.agent is None
    assert typed_events[-1]["type"] == "final"
    assert "Map URL is not configured" in typed_events[-1]["text"]


def test_answer_in_thread_emits_safe_final_for_agent_configuration_errors() -> None:
    class Agent:
        def ask(self, question, **kwargs):
            raise ValueError("SUBCONSCIOUS_API_KEY is required")

    session = TerminalSession(session_id="error-session", city="nyc", agent=Agent())
    events = []

    _answer_in_thread(session, "how many events in soho?", events.append)

    typed_events = [event for event in events if event is not None]
    assert not any(event["type"] == "error" for event in typed_events)
    assert typed_events[-1]["type"] == "final"
    assert "SUBCONSCIOUS_API_KEY" not in typed_events[-1]["text"]
    assert "backend" not in typed_events[-1]["text"].lower()
    assert "credentials" not in typed_events[-1]["text"].lower()
    assert "TWAG search is unavailable" in typed_events[-1]["text"]


def test_answer_in_thread_does_not_emit_raw_unhandled_exception_text(monkeypatch) -> None:
    session = TerminalSession(session_id="broken-session", city="nyc")
    events = []

    def broken_answer_route(text, state):
        raise RuntimeError("raw internal failure with secret details")

    monkeypatch.setattr(
        "twag_clickhouse.terminal_server.answer_route",
        broken_answer_route,
    )

    _answer_in_thread(session, "hello", events.append)

    typed_events = [event for event in events if event is not None]
    assert typed_events[-1]["type"] == "error"
    assert "secret details" not in typed_events[-1]["error"]
    assert "Please try again later" in typed_events[-1]["error"]
