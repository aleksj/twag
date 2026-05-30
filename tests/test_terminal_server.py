from __future__ import annotations

import time
from urllib.parse import quote

import pytest
from fastapi.responses import Response

from twag_clickhouse.terminal_server import (
    SessionCreateRequest,
    TerminalSession,
    _TerminalToolCallFilter,
    _answer_in_thread,
    _get_session,
    _handle_user_message,
    _sessions,
    _states,
    app,
    cities,
    create_session,
    terminal_token_is_valid,
    health,
    ready_event,
    root,
    terminal_asset,
    terminal_index,
    terminal_result_map,
    terminal_result_map_geojson,
    terminal_query_timeout_reply,
    terminal_query_timeout_summary,
)


def test_terminal_server_metadata_endpoints() -> None:
    assert root()["endpoints"]["websocket"] == "/sessions/{session_id}"
    assert root()["endpoints"]["terminal"] == "/terminal"
    assert health()["service"] == "twag-terminal-server"

    city_slugs = {city["slug"] for city in cities()["cities"]}
    assert {"nyc", "boston"} <= city_slugs


def test_terminal_timeout_message_uses_human_elapsed_label() -> None:
    reply = terminal_query_timeout_reply("events added?", 0.45)
    summary = terminal_query_timeout_summary(0.45)

    assert "after 0s" not in reply
    assert "after less than 1s" in reply
    assert summary == "Stopped after less than 1s. Try a narrower query."


def test_terminal_server_serves_browser_terminal_assets() -> None:
    index_response = terminal_index()
    app_response = terminal_asset("app.js")
    css_response = terminal_asset("styles.css")

    assert "/terminal/app.js?v=" in index_response.body.decode("utf-8")
    assert "/terminal/styles.css?v=" in index_response.body.decode("utf-8")
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
    assert set(event["backend_status"]) == {"clickhouse", "subconscious"}
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


def test_answer_in_thread_does_not_emit_thinking_stream_in_quiet_mode(monkeypatch) -> None:
    monkeypatch.delenv("TWAG_PUBLIC_MAP_BASE_URL", raising=False)

    class Agent:
        def ask(self, question, **kwargs):
            assert kwargs.get("raw_stream_callback") is None
            kwargs["stream_callback"]("Answer")
            return "Answer"

    session = TerminalSession(session_id="thinking-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "how many events in soho?", events.append)

    typed_events = [event for event in events if event is not None]
    thinking_events = [event for event in typed_events if event["type"] == "thinking_delta"]
    delta_events = [event for event in typed_events if event["type"] == "delta"]
    assert thinking_events == []
    assert delta_events[-1]["text"] == "Answer"
    assert typed_events[-1]["type"] == "final"
    assert typed_events[-1]["text"] == "Answer"


def test_answer_in_thread_emits_folded_thinking_stream_in_verbose_terminal_mode(monkeypatch) -> None:
    monkeypatch.delenv("TWAG_PUBLIC_MAP_BASE_URL", raising=False)

    class Agent:
        def ask(self, question, **kwargs):
            kwargs["raw_stream_callback"]("<think>plan</think>")
            return "Answer"

    session = TerminalSession(session_id="verbose-thinking-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    session.state.verbose = True
    events = []

    _answer_in_thread(session, "how many events in soho?", events.append)

    thinking_events = [
        event for event in events if event is not None and event["type"] == "thinking_delta"
    ]
    assert thinking_events
    assert thinking_events[-1]["expanded"] is False


def test_terminal_tool_call_filter_hides_streamed_tool_protocol() -> None:
    stream_filter = _TerminalToolCallFilter()
    chunks = [
        "Thinking first.\n<too",
        "l_call>\n=query_bostw_clickhouse\nSELECT * FROM table\n",
        "</function>\n</tool_call>\n",
        "Readable answer.",
    ]

    assert "".join(stream_filter.feed(chunk) for chunk in chunks) == (
        "Thinking first.\n\nReadable answer."
    )


@pytest.mark.anyio
async def test_handle_user_message_times_out_with_recovery_guidance(monkeypatch) -> None:
    class WebSocket:
        def __init__(self) -> None:
            self.events = []

        async def send_json(self, event):
            self.events.append(event)

    def slow_answer(_session, _text, _emit):
        time.sleep(0.2)

    monkeypatch.setattr(
        "twag_clickhouse.terminal_server.terminal_query_timeout_seconds",
        lambda: 0.05,
    )
    monkeypatch.setattr(
        "twag_clickhouse.terminal_server.terminal_query_heartbeat_seconds",
        lambda: 0.01,
    )
    monkeypatch.setattr(
        "twag_clickhouse.terminal_server._answer_in_thread",
        slow_answer,
    )
    websocket = WebSocket()
    session = TerminalSession(session_id="timeout-session", city="boston")

    await _handle_user_message(websocket, session, "what has changed from yesterday?")

    error_events = [event for event in websocket.events if event["type"] == "error"]
    status_events = [event for event in websocket.events if event["type"] == "status"]
    assert status_events
    assert error_events
    assert "stopped instead of waiting silently" in error_events[-1]["error"]
    assert "events added since yesterday" in error_events[-1]["error"]
    assert "what has changed from yesterday?" in error_events[-1]["error"]


@pytest.mark.anyio
async def test_handle_user_message_final_sentinel_wins_over_worker_done_race(monkeypatch) -> None:
    class WebSocket:
        def __init__(self) -> None:
            self.events = []

        async def send_json(self, event):
            self.events.append(event)

    def complete_then_linger(_session, _text, emit):
        emit({"type": "final", "text": "done", "duration_ms": 1})
        emit(None)
        time.sleep(0.05)

    monkeypatch.setattr(
        "twag_clickhouse.terminal_server.terminal_query_timeout_seconds",
        lambda: 1.0,
    )
    monkeypatch.setattr(
        "twag_clickhouse.terminal_server.terminal_query_heartbeat_seconds",
        lambda: 0.01,
    )
    monkeypatch.setattr(
        "twag_clickhouse.terminal_server._answer_in_thread",
        complete_then_linger,
    )
    websocket = WebSocket()
    session = TerminalSession(session_id="complete-race-session", city="nyc")

    await _handle_user_message(websocket, session, "events added since yesterday")

    assert [event["type"] for event in websocket.events] == ["final"]


def test_answer_in_thread_emits_backend_readiness_events(monkeypatch) -> None:
    monkeypatch.delenv("TWAG_PUBLIC_MAP_BASE_URL", raising=False)

    class Agent:
        def ask(self, question, **kwargs):
            kwargs["token_usage_callback"]({"total_tokens": 10})
            return f"answered {question}"

    session = TerminalSession(session_id="backend-status-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "summarize the dataset", events.append)

    backend_events = [
        event for event in events if event is not None and event["type"] == "backend_status"
    ]
    assert backend_events
    assert backend_events[0]["services"]["clickhouse"]["state"] == "working"
    assert backend_events[0]["services"]["subconscious"]["state"] == "working"
    assert backend_events[-1]["services"]["clickhouse"]["state"] == "ready"
    assert backend_events[-1]["services"]["subconscious"]["state"] == "ready"


def test_answer_in_thread_uses_terminal_agent_turn_limit(monkeypatch) -> None:
    monkeypatch.setenv("TWAG_TERMINAL_AGENT_MAX_TURNS", "17")
    monkeypatch.delenv("TWAG_TERMINAL_ENABLE_THINKING", raising=False)

    class Agent:
        def __init__(self) -> None:
            self.max_turns = None
            self.enable_thinking = None

        def ask(self, question, **kwargs):
            self.max_turns = kwargs.get("max_turns")
            self.enable_thinking = kwargs.get("enable_thinking")
            return f"answered {question}"

    agent = Agent()
    session = TerminalSession(session_id="turn-limit-session", city="nyc", agent=agent)  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "how many events in soho?", events.append)

    assert agent.max_turns == 17
    assert agent.enable_thinking is False


def test_terminal_session_can_be_restored_from_disk(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TWAG_TERMINAL_SESSION_DIR", str(tmp_path))

    session = TerminalSession(session_id="persisted-session", city="nyc")
    session.state.verbose = True
    session.state.conversation.last_event_question = "list AI events"
    session.state.conversation.last_event_offset = 25
    events = []

    _answer_in_thread(session, "/help", events.append)
    _sessions.pop(session.session_id, None)
    _states.pop(session.session_id, None)

    restored = _get_session(session.session_id)

    assert restored is not None
    assert restored.city == "nyc"
    assert restored.state.verbose is True
    assert restored.state.conversation.last_event_question == "list AI events"
    assert restored.state.conversation.last_event_offset == 25


def test_answer_in_thread_tracks_more_without_unbacked_map_link(monkeypatch) -> None:
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

    first_typed = [event for event in first_events if event is not None]
    more_typed = [event for event in more_events if event is not None]
    assert first_typed[-1]["text"] == "list events involving running @ 0"
    assert more_typed[-1]["text"] == "list events involving running @ 25"
    assert agent.calls == [
        ("list events involving running", 0),
        ("list events involving running", 25),
    ]


def test_answer_in_thread_adds_map_link_for_mapped_event_results(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TWAG_PUBLIC_MAP_BASE_URL", raising=False)
    monkeypatch.delenv("TWAG_PUBLIC_TERMINAL_BASE_URL", raising=False)
    monkeypatch.setenv("TWAG_TERMINAL_RESULT_MAPS_ENABLED", "true")

    class Agent:
        def ask(self, question, **_kwargs):
            self.last_event_map_rows = [{"event_id": "mapped-1"}]
            return f"answered {question}"

    (tmp_path / "nyc.geojson").write_text(
        """
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {"type": "Point", "coordinates": [-73.99, 40.73]},
      "properties": {
        "event_id": "mapped-1",
        "event_date": "2026-06-01",
        "title": "Mapped event"
      }
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("twag_clickhouse.terminal_server.DOCS_DIR", tmp_path)
    session = TerminalSession(session_id="mapped-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "list AI events in SoHo", events.append)

    typed_events = [event for event in events if event is not None]
    final_text = typed_events[-1]["text"]
    assert "View on map" in final_text
    assert "/terminal/map/mapped-session/" in final_text

    map_id = next(iter(session.map_results))
    response = Response()
    geojson = terminal_result_map_geojson(session.session_id, map_id, response)
    assert geojson["metadata"]["count"] == 1
    assert geojson["features"][0]["properties"]["event_id"] == "mapped-1"
    assert response.headers["Access-Control-Allow-Origin"] == "*"

    map_response = terminal_result_map(session.session_id, map_id)
    html = map_response.body.decode()
    assert '"token": ""' in html
    assert "tile.openstreetmap.org" in html
    assert "maplibre-gl" in html
    assert "window.mapboxgl = window.maplibregl" in html
    assert "demotiles.maplibre.org/font" in html
    assert "mapbox://styles/mapbox" not in html
    assert "api.mapbox.com/mapbox-gl-js" not in html
    assert f'"geojsonUrl": "{map_id}.geojson"' in html


def test_map_command_with_query_generates_filtered_result_map(monkeypatch, tmp_path) -> None:
    class Agent:
        def __init__(self) -> None:
            self.calls = []

        def ask(self, question, **kwargs):
            self.calls.append(question)
            self.last_event_map_rows = [{"event_id": "columbia-1"}]
            return "Showing 1-1 of 1 matching event.\n\n**Columbia event**"

    (tmp_path / "nyc.geojson").write_text(
        """
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {"type": "Point", "coordinates": [-73.96, 40.81]},
      "properties": {
        "event_id": "columbia-1",
        "event_date": "2026-06-02",
        "title": "Columbia event"
      }
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TWAG_PUBLIC_MAP_BASE_URL", "https://natea.github.io/twag/")
    monkeypatch.setenv("TWAG_PUBLIC_TERMINAL_BASE_URL", "https://data.flowers/tw/terminal")
    monkeypatch.setenv("TWAG_TERMINAL_RESULT_MAPS_ENABLED", "true")
    monkeypatch.setattr("twag_clickhouse.terminal_server.DOCS_DIR", tmp_path)
    session = TerminalSession(session_id="map-search-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "/map events at Columbia University", events.append)

    typed_events = [event for event in events if event is not None]
    final_text = typed_events[-1]["text"]
    assert final_text.startswith("Showing 1-1 of 1 matching event.")
    assert "**Columbia event**" in final_text
    map_id = next(iter(session.map_results))
    result_url = quote(
        f"https://data.flowers/tw/terminal/map/map-search-session/{map_id}.geojson",
        safe="",
    )
    assert (
        "[View on map](https://natea.github.io/twag/events_map_nyc.html"
        f"#date=2026-06-02&result_url={result_url}&event_ids=columbia-1)"
    ) in final_text
    assert session.agent.calls == ["list events at Columbia University"]  # type: ignore[union-attr]

    geojson = terminal_result_map_geojson(session.session_id, map_id, Response())
    assert geojson["metadata"]["count"] == 1
    assert geojson["features"][0]["properties"]["event_id"] == "columbia-1"


def test_map_command_with_query_is_disabled_until_public_map_supports_results(monkeypatch) -> None:
    monkeypatch.setenv("TWAG_PUBLIC_MAP_BASE_URL", "https://natea.github.io/twag/")
    monkeypatch.delenv("TWAG_TERMINAL_RESULT_MAPS_ENABLED", raising=False)

    class Agent:
        def ask(self, question, **kwargs):
            raise AssertionError("disabled map search should not call agent")

    session = TerminalSession(session_id="disabled-map-search-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "/map events at Columbia University", events.append)

    typed_events = [event for event in events if event is not None]
    assert typed_events[-1]["type"] == "final"
    assert "temporarily disabled" in typed_events[-1]["text"]
    assert "`/map YYYY-MM-DD`" in typed_events[-1]["text"]
    assert "result_url" in typed_events[-1]["text"]
    assert session.map_results == {}


def test_map_command_with_plain_date_keeps_static_map_behavior(monkeypatch) -> None:
    monkeypatch.setenv("TWAG_PUBLIC_MAP_BASE_URL", "https://example.test/twag/")

    class Agent:
        def ask(self, question, **kwargs):
            raise AssertionError("date-only map command should not call agent")

    session = TerminalSession(session_id="map-date-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "/map June 3", events.append)

    typed_events = [event for event in events if event is not None]
    assert typed_events[-1]["text"] == (
        "[NY Tech Week map for 2026-06-03]"
        "(https://example.test/twag/events_map_nyc.html#date=2026-06-03)"
    )


def test_map_command_with_relative_date_uses_current_date(monkeypatch) -> None:
    monkeypatch.setenv("TWAG_PUBLIC_MAP_BASE_URL", "https://example.test/twag/")
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-28")

    class Agent:
        def ask(self, question, **kwargs):
            raise AssertionError("date-only map command should not call agent")

    session = TerminalSession(session_id="map-relative-date-session", city="nyc", agent=Agent())  # type: ignore[arg-type]
    events = []

    _answer_in_thread(session, "/map tomorrow", events.append)

    typed_events = [event for event in events if event is not None]
    assert typed_events[-1]["text"] == (
        "[NY Tech Week map for 2026-05-29]"
        "(https://example.test/twag/events_map_nyc.html#date=2026-05-29)"
    )


def test_terminal_result_map_geojson_route_is_not_shadowed() -> None:
    path = "/terminal/map/route-session/map-result.geojson"
    matches = [
        route
        for route in app.routes
        if getattr(route, "path_regex", None)
        and route.path_regex.match(path)  # type: ignore[attr-defined]
    ]

    assert matches
    assert matches[0].path == "/terminal/map/{session_id}/{map_id}.geojson"


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
