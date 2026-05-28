from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import pathlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse, Response
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover - exercised only when deps missing
    raise RuntimeError(
        "FastAPI dependencies are not installed. Run `pip install -e .`."
    ) from exc

from .chat_session import (
    ChatState,
    TokenUsageAccumulator,
    active_city_override,
    answer_route,
    answer_session_message,
    clear_chat_status,
    help_reply,
    question_log_route,
    status_text,
    update_chat_status,
)
from .city import CITIES, active_city, load_city
from .subconscious_agent import NytwSubconsciousAgent


DEFAULT_TERMINAL_HOST = "localhost"
DEFAULT_TERMINAL_PORT = 8765
TERMINAL_TOKEN_HEADER = "x-twag-terminal-token"
TERMINAL_WEB_DIR = pathlib.Path(__file__).with_name("terminal_web")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
LOGGER = logging.getLogger(__name__)
INTERNAL_TERMINAL_ERROR_REPLY = (
    "TWAG hit an internal error while answering. Please try again later."
)


class SessionCreateRequest(BaseModel):
    city: str | None = None


def terminal_operator_token() -> str:
    return os.getenv("TWAG_TERMINAL_TOKEN", "").strip()


def terminal_auth_enabled() -> bool:
    return bool(terminal_operator_token())


def terminal_token_is_valid(token: str | None) -> bool:
    expected = terminal_operator_token()
    if not expected:
        return True
    return bool(token) and secrets.compare_digest(token, expected)


def require_terminal_auth(request: Request) -> None:
    token = request.headers.get(TERMINAL_TOKEN_HEADER) or request.query_params.get("token")
    if terminal_token_is_valid(token):
        return
    raise HTTPException(status_code=401, detail="Operator token required")


@dataclass
class TerminalSession:
    session_id: str
    city: str
    state: ChatState = field(default_factory=ChatState)
    agent: NytwSubconsciousAgent | None = None
    map_results: dict[str, dict[str, Any]] = field(default_factory=dict)


class LazySessionAgent:
    def __init__(self, session: TerminalSession) -> None:
        self.session = session

    def ask(self, *args: Any, **kwargs: Any) -> str:
        return _session_agent(self.session).ask(*args, **kwargs)


app = FastAPI(
    title="TWAG Local Terminal Server",
    description="Local-only WebSocket backend for the TWAG operator terminal.",
    version="0.1.0",
)
_sessions: dict[str, TerminalSession] = {}
_states: dict[str, ChatState] = {}
_city_lock = threading.Lock()


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "TWAG Local Terminal Server",
        "endpoints": {
            "health": "/health",
            "cities": "/cities",
            "sessions": "/sessions",
            "websocket": "/sessions/{session_id}",
            "terminal": "/terminal",
        },
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/terminal", response_class=HTMLResponse)
def terminal_index() -> FileResponse:
    return FileResponse(TERMINAL_WEB_DIR / "index.html")


@app.get("/terminal/{asset}", include_in_schema=False)
def terminal_asset(asset: str) -> FileResponse:
    terminal_assets = {"app.js", "styles.css"}
    map_assets = {"events_map.js", "events_map.css", "config.js"}
    if asset in terminal_assets:
        return FileResponse(TERMINAL_WEB_DIR / asset)
    if asset in map_assets:
        return FileResponse(DOCS_DIR / asset)
    else:
        raise HTTPException(status_code=404, detail="Unknown terminal asset")


@app.get("/terminal/map/{session_id}/{map_id}.geojson")
def terminal_result_map_geojson(session_id: str, map_id: str) -> dict[str, Any]:
    session = _sessions.get(session_id)
    if session is None or map_id not in session.map_results:
        raise HTTPException(status_code=404, detail="Unknown map result")
    result = session.map_results[map_id]
    features = list(result.get("features") or [])
    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "city": result.get("city"),
            "source": "terminal-search-results",
            "count": len(features),
        },
    }


@app.get("/terminal/map/{session_id}/{map_id}", response_class=HTMLResponse)
def terminal_result_map(session_id: str, map_id: str) -> HTMLResponse:
    session = _sessions.get(session_id)
    if session is None or map_id not in session.map_results:
        raise HTTPException(status_code=404, detail="Unknown map result")
    result = session.map_results[map_id]
    city = load_city(str(result["city"]))
    dates = list(result.get("dates") or [city.default_map_date])
    title = f"{city.short_name} search results"
    config = {
        "token": "",
        "centerLat": city.map_center_lat,
        "centerLon": city.map_center_lon,
        "zoom": city.map_zoom,
        "style": {
            "version": 8,
            "glyphs": "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
            "sources": {
                "osm": {
                    "type": "raster",
                    "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                    "tileSize": 256,
                    "attribution": "© OpenStreetMap contributors",
                }
            },
            "layers": [{"id": "osm", "type": "raster", "source": "osm"}],
        },
        "geojsonUrl": f"{map_id}.geojson",
        "dateRange": dates,
        "defaultDate": dates[0],
    }
    config_json = json.dumps(config)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>{html.escape(title)}</title>
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
  <link href="../../events_map.css" rel="stylesheet">
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <div id="count">Loading...</div>
    <div class="credit">Filtered from terminal search results</div>
  </header>
  <nav class="tab-nav">
    <a href="../.." class="active">Terminal</a>
  </nav>
  <div id="date-picker"></div>
  <div id="map"></div>
  <div id="error"></div>

  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <script>window.mapboxgl = window.maplibregl;</script>
  <script src="../../config.js"></script>
  <script src="../../events_map.js"></script>
  <script>initEventMap({config_json});</script>
</body>
</html>
"""
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "twag-terminal-server",
        "city": active_city().slug,
        "operator_auth": terminal_auth_enabled(),
    }


@app.get("/cities")
def cities() -> dict[str, Any]:
    return {
        "cities": [
            {
                "slug": city.slug,
                "display_name": city.display_name,
                "short_name": city.short_name,
                "event_date_range": city.event_date_range,
            }
            for city in CITIES.values()
        ]
    }


@app.post("/sessions")
def create_session_endpoint(
    http_request: Request,
    request: SessionCreateRequest | None = None,
) -> dict[str, Any]:
    require_terminal_auth(http_request)
    return create_session(request)


@app.post("/terminal/sessions", include_in_schema=False)
def create_terminal_session_endpoint(
    http_request: Request,
    request: SessionCreateRequest | None = None,
) -> dict[str, Any]:
    require_terminal_auth(http_request)
    return create_session(request)


def create_session(request: SessionCreateRequest | None = None) -> dict[str, Any]:
    requested_city = request.city if request else None
    city = load_city(requested_city or active_city().slug)
    session_id = uuid.uuid4().hex
    session = TerminalSession(session_id=session_id, city=city.slug)
    _sessions[session_id] = session
    _states[session_id] = session.state
    return {
        "session_id": session_id,
        "city": city.slug,
        "websocket": f"/sessions/{session_id}",
    }


def ready_event(session: TerminalSession) -> dict[str, Any]:
    with active_city_override(session.city):
        greeting = help_reply()
    return {
        "type": "ready",
        "session_id": session.session_id,
        "city": session.city,
        "verbose": session.state.verbose,
        "greeting": greeting,
    }


def _city_geojson(city_slug: str) -> dict[str, Any]:
    city = load_city(city_slug)
    path = DOCS_DIR / f"{city.slug}.geojson"
    if not path.is_file():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _features_for_event_ids(city_slug: str, event_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(event_ids)
    if not wanted:
        return []
    geojson = _city_geojson(city_slug)
    features = []
    for feature in geojson.get("features") or []:
        event_id = str((feature.get("properties") or {}).get("event_id") or "")
        if event_id in wanted:
            features.append(feature)
    order = {event_id: index for index, event_id in enumerate(event_ids)}
    features.sort(
        key=lambda feature: order.get(
            str((feature.get("properties") or {}).get("event_id") or ""),
            len(order),
        )
    )
    return features


def _terminal_map_link(session: TerminalSession) -> str:
    rows = list(getattr(session.agent, "last_event_map_rows", []) or [])
    event_ids = []
    seen = set()
    for row in rows:
        event_id = str(row.get("event_id") or "").strip()
        if event_id and event_id not in seen:
            event_ids.append(event_id)
            seen.add(event_id)

    features = _features_for_event_ids(session.city, event_ids)
    if not features:
        return ""

    map_id = uuid.uuid4().hex
    dates = sorted(
        {
            str((feature.get("properties") or {}).get("event_date") or "")
            for feature in features
            if (feature.get("properties") or {}).get("event_date")
        }
    )
    session.map_results[map_id] = {
        "city": session.city,
        "created_at": time.time(),
        "event_ids": event_ids,
        "features": features,
        "dates": dates or [load_city(session.city).default_map_date],
    }
    if len(session.map_results) > 20:
        oldest = sorted(
            session.map_results,
            key=lambda key: float(session.map_results[key].get("created_at") or 0),
        )
        for old_key in oldest[:-20]:
            session.map_results.pop(old_key, None)
    return f"\n\n[View on map](/terminal/map/{session.session_id}/{map_id})"


@app.websocket("/sessions/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str) -> None:
    await _websocket_session(websocket, session_id)


@app.websocket("/terminal/sessions/{session_id}")
async def terminal_websocket_session(websocket: WebSocket, session_id: str) -> None:
    await _websocket_session(websocket, session_id)


async def _websocket_session(websocket: WebSocket, session_id: str) -> None:
    session = _sessions.get(session_id)
    if session is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    await websocket.send_json(ready_event(session))

    try:
        while True:
            payload = await websocket.receive_json()
            message_type = str(payload.get("type") or "message")
            if message_type == "message":
                text = str(payload.get("text") or "").strip()
                if text:
                    await _handle_user_message(websocket, session, text)
                continue
            if message_type == "set_city":
                await _set_session_city(websocket, session, str(payload.get("city") or ""))
                continue
            if message_type == "ping":
                await websocket.send_json({"type": "pong", "time": time.time()})
                continue
            await websocket.send_json(
                {"type": "error", "error": f"Unknown message type: {message_type}"}
            )
    except WebSocketDisconnect:
        return


async def _set_session_city(
    websocket: WebSocket,
    session: TerminalSession,
    city_slug: str,
) -> None:
    try:
        city = load_city(city_slug)
    except ValueError as exc:
        await websocket.send_json({"type": "error", "error": str(exc)})
        return

    session.city = city.slug
    session.state = ChatState()
    _states[session.session_id] = session.state
    await websocket.send_json(
        {
            "type": "city",
            "city": city.slug,
            "display_name": city.display_name,
            "message": f"Switched to {city.short_name}.",
        }
    )


async def _handle_user_message(
    websocket: WebSocket,
    session: TerminalSession,
    text: str,
) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    worker = asyncio.create_task(asyncio.to_thread(_answer_in_thread, session, text, emit))

    while True:
        event = await queue.get()
        if event is None:
            break
        await websocket.send_json(event)

    await worker


def _answer_in_thread(
    session: TerminalSession,
    text: str,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    state = session.state
    _sessions[session.session_id] = session
    _states[session.session_id] = state
    usage = TokenUsageAccumulator()
    error: str | None = None
    answer = ""
    started_at = time.monotonic()

    def progress(step: str) -> None:
        update_chat_status(state, step=step)
        emit({"type": "status", "text": status_text(state), "step": step})

    def visible_stream(partial: str) -> None:
        if partial:
            emit({"type": "delta", "text": partial, "mode": "replace"})

    def raw_stream(chunk: str) -> None:
        if chunk:
            emit({"type": "delta", "text": chunk, "mode": "append"})

    try:
        with _city_lock:
            with active_city_override(session.city):
                route, first_step = answer_route(text, state)
                update_chat_status(
                    state,
                    question=text,
                    route=route,
                    step="Received your message.",
                )
                emit({"type": "status", "text": status_text(state), "step": "Received your message."})
                update_chat_status(state, step=first_step)
                emit({"type": "status", "text": status_text(state), "step": first_step})

                stream_callback = (lambda _partial: None) if state.verbose else visible_stream
                raw_stream_callback = raw_stream if state.verbose else (lambda _chunk: None)
                answer = answer_session_message(
                    LazySessionAgent(session),
                    _states,
                    session.session_id,
                    text,
                    progress=progress,
                    stream_callback=stream_callback,
                    raw_stream_callback=raw_stream_callback,
                    token_usage_callback=usage.add,
                )
                answer += _terminal_map_link(session)
        emit(
            {
                "type": "final",
                "text": answer,
                "route": question_log_route(text),
                "usage": usage.as_dict(),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            }
        )
    except Exception:
        LOGGER.exception("Unhandled terminal session error")
        emit(
            {
                "type": "error",
                "error": INTERNAL_TERMINAL_ERROR_REPLY,
                "route": question_log_route(text),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            }
        )
    finally:
        clear_chat_status(state)
        emit(None)


def _session_agent(session: TerminalSession) -> NytwSubconsciousAgent:
    if session.agent is None:
        session.agent = NytwSubconsciousAgent.from_env()
    return session.agent


def main() -> None:
    if load_dotenv:
        load_dotenv(".env", override=False)

    import uvicorn

    uvicorn.run(
        "twag_clickhouse.terminal_server:app",
        host=os.getenv("TWAG_TERMINAL_HOST", DEFAULT_TERMINAL_HOST),
        port=int(os.getenv("TWAG_TERMINAL_PORT", str(DEFAULT_TERMINAL_PORT))),
        reload=os.getenv("TWAG_TERMINAL_RELOAD", "").lower() in {"1", "true", "yes"},
        access_log=os.getenv("TWAG_TERMINAL_ACCESS_LOG", "").lower()
        in {"1", "true", "yes"},
        log_level=os.getenv("TWAG_TERMINAL_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
