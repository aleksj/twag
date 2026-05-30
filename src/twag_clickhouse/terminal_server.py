from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import pathlib
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

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
    map_command_query,
    map_url_for,
    question_log_route,
    update_chat_status,
)
from .city import CITIES, active_city, load_city
from .client import ClickHouseService
from .config import ClickHouseConfig
from .conversation import ConversationTurn
from .subconscious_agent import NytwSubconsciousAgent
from .terminal_assets import render_terminal_index


DEFAULT_TERMINAL_HOST = "localhost"
DEFAULT_TERMINAL_PORT = 8765
TERMINAL_TOKEN_HEADER = "x-twag-terminal-token"
TERMINAL_WEB_DIR = pathlib.Path(__file__).with_name("terminal_web")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
LOGGER = logging.getLogger(__name__)
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
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


def terminal_session_dir() -> pathlib.Path | None:
    raw = os.getenv("TWAG_TERMINAL_SESSION_DIR")
    if raw is not None:
        raw = raw.strip()
        return pathlib.Path(raw) if raw else None
    default = pathlib.Path("/var/log/twag/terminal-sessions")
    return default if default.parent.is_dir() else None


def terminal_agent_max_turns() -> int:
    raw = os.getenv("TWAG_TERMINAL_AGENT_MAX_TURNS", "").strip()
    if not raw:
        return 4
    try:
        return max(1, int(raw))
    except ValueError:
        LOGGER.warning("Ignoring invalid TWAG_TERMINAL_AGENT_MAX_TURNS=%r", raw)
        return 4


def terminal_agent_enable_thinking() -> bool:
    raw = os.getenv("TWAG_TERMINAL_ENABLE_THINKING", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        LOGGER.warning("Ignoring invalid %s=%r", name, raw)
        return default


def terminal_query_timeout_seconds() -> float:
    return _env_float("TWAG_TERMINAL_QUERY_TIMEOUT_SECONDS", 180.0, minimum=1.0)


def terminal_query_heartbeat_seconds() -> float:
    return _env_float("TWAG_TERMINAL_QUERY_HEARTBEAT_SECONDS", 20.0, minimum=0.5)


def public_terminal_base_url() -> str:
    return os.getenv("TWAG_PUBLIC_TERMINAL_BASE_URL", "").strip().rstrip("/")


def terminal_result_maps_enabled() -> bool:
    raw = os.getenv("TWAG_TERMINAL_RESULT_MAPS_ENABLED", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def terminal_elapsed_label(elapsed: float) -> str:
    if elapsed < 1:
        return "less than 1s"
    return f"{int(elapsed)}s"


def terminal_query_timeout_summary(elapsed: float) -> str:
    return f"Stopped after {terminal_elapsed_label(elapsed)}. Try a narrower query."


def terminal_query_timeout_reply(text: str, elapsed: float) -> str:
    return (
        f"TWAG is still working after {terminal_elapsed_label(elapsed)}, so this browser turn was "
        "stopped instead of waiting silently.\n\n"
        "Try a narrower follow-up, for example:\n"
        "- events added since yesterday\n"
        "- events updated since yesterday\n"
        "- new open RSVP events since yesterday\n\n"
        f"Original query: {text}"
    )


def terminal_status_step(step: str) -> str | None:
    if step == "Received your message.":
        return None
    if step.startswith("Handing the request to the "):
        return None
    if step.startswith("Letting the agent choose between "):
        return "Searching the event data and synced context."
    if step.startswith("Choosing the smallest useful data query"):
        return None
    if step.startswith("Preparing a ranked event search"):
        return "Searching events by topic, venue, host, and location."
    if step.startswith("Expanded search terms:"):
        return None
    if step.startswith("Building a ranked event query"):
        return None
    if step == "Validating the selected ClickHouse query before execution.":
        return "Running the ClickHouse query."
    if step.startswith("ClickHouse returned ") and "sending rows back for synthesis" in step:
        return step.replace("sending rows back for synthesis", "composing the answer")
    if step == "Formatting the database rows into the final Telegram answer.":
        return None
    if step.startswith("ClickHouse returned ") and "formatting" in step:
        return step
    if step.startswith("Reusing the previous event query"):
        return "Loading the next page of saved results."
    return step


def _initial_backend_status() -> dict[str, dict[str, Any]]:
    model_state = "configured" if os.getenv("SUBCONSCIOUS_API_KEY", "").strip() else "unconfigured"
    return {
        "clickhouse": {"state": "unknown", "label": "ClickHouse"},
        "subconscious": {"state": model_state, "label": "Subconscious"},
    }


@dataclass
class TerminalSession:
    session_id: str
    city: str
    state: ChatState = field(default_factory=ChatState)
    agent: NytwSubconsciousAgent | None = None
    map_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    backend_status: dict[str, dict[str, Any]] = field(
        default_factory=_initial_backend_status
    )


class LazySessionAgent:
    def __init__(self, session: TerminalSession) -> None:
        self.session = session

    def ask(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("max_turns", terminal_agent_max_turns())
        kwargs.setdefault("enable_thinking", terminal_agent_enable_thinking())
        return _session_agent(self.session).ask(*args, **kwargs)


app = FastAPI(
    title="TWAG Local Terminal Server",
    description="Local-only WebSocket backend for the TWAG operator terminal.",
    version="0.1.0",
)
_sessions: dict[str, TerminalSession] = {}
_states: dict[str, ChatState] = {}
_city_lock = threading.Lock()


def _session_path(session_id: str) -> pathlib.Path | None:
    base = terminal_session_dir()
    if base is None or not SESSION_ID_RE.fullmatch(session_id):
        return None
    return base / f"{session_id}.json"


def _state_snapshot(state: ChatState) -> dict[str, Any]:
    return {
        "conversation": {
            "last_event_question": state.conversation.last_event_question,
            "last_event_offset": state.conversation.last_event_offset,
            "recent_turns": [
                {
                    "user_text": turn.user_text,
                    "effective_question": turn.effective_question,
                    "answer_summary": turn.answer_summary,
                    "sql_queries": turn.sql_queries,
                }
                for turn in state.conversation.recent_turns
            ],
        },
        "verbose": state.verbose,
    }


def _restore_state(snapshot: dict[str, Any] | None) -> ChatState:
    state = ChatState()
    if not isinstance(snapshot, dict):
        return state
    conversation = snapshot.get("conversation") or {}
    if isinstance(conversation, dict):
        last_question = conversation.get("last_event_question")
        state.conversation.last_event_question = (
            str(last_question) if last_question else None
        )
        try:
            state.conversation.last_event_offset = int(
                conversation.get("last_event_offset") or 0
            )
        except (TypeError, ValueError):
            state.conversation.last_event_offset = 0
        recent_turns = conversation.get("recent_turns")
        if isinstance(recent_turns, list):
            for item in recent_turns[-4:]:
                if not isinstance(item, dict):
                    continue
                sql_queries = item.get("sql_queries") or []
                state.conversation.recent_turns.append(
                    ConversationTurn(
                        user_text=str(item.get("user_text") or ""),
                        effective_question=str(item.get("effective_question") or ""),
                        answer_summary=str(item.get("answer_summary") or ""),
                        sql_queries=[
                            str(sql)
                            for sql in sql_queries
                            if isinstance(sql, str) and sql.strip()
                        ][:3],
                    )
                )
    state.verbose = bool(snapshot.get("verbose"))
    return state


def _save_session(session: TerminalSession) -> None:
    path = _session_path(session.session_id)
    if path is None:
        return
    snapshot = {
        "version": 1,
        "session_id": session.session_id,
        "city": session.city,
        "updated_at": time.time(),
        "state": _state_snapshot(session.state),
        "map_results": session.map_results,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(json.dumps(snapshot), encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        LOGGER.exception("Could not persist terminal session")


def _load_session(session_id: str) -> TerminalSession | None:
    path = _session_path(session_id)
    if path is None or not path.is_file():
        return None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        city = load_city(str(snapshot.get("city") or active_city().slug))
        state = _restore_state(snapshot.get("state"))
        map_results = snapshot.get("map_results")
        session = TerminalSession(
            session_id=session_id,
            city=city.slug,
            state=state,
            map_results=map_results if isinstance(map_results, dict) else {},
        )
    except Exception:
        LOGGER.exception("Could not restore terminal session")
        return None
    _sessions[session_id] = session
    _states[session_id] = session.state
    return session


def _get_session(session_id: str) -> TerminalSession | None:
    return _sessions.get(session_id) or _load_session(session_id)


def _set_backend_service(
    session: TerminalSession,
    service: str,
    state: str,
    *,
    detail: str = "",
) -> None:
    current = dict(session.backend_status.get(service) or {})
    current["state"] = state
    if detail:
        current["detail"] = detail
    else:
        current.pop("detail", None)
    session.backend_status[service] = current


def _backend_status_event(session: TerminalSession) -> dict[str, Any]:
    return {
        "type": "backend_status",
        "services": json.loads(json.dumps(session.backend_status)),
        "updated_at": time.time(),
    }


class _TerminalToolCallFilter:
    start_marker = "<tool_call"
    end_marker = "</tool_call>"

    def __init__(self) -> None:
        self.buffer = ""
        self.in_tool_call = False

    def feed(self, chunk: str) -> str:
        text = self.buffer + chunk
        self.buffer = ""
        output: list[str] = []

        while text:
            if self.in_tool_call:
                end = text.find(self.end_marker)
                if end < 0:
                    self.buffer = text[-(len(self.end_marker) - 1) :]
                    return "".join(output)
                text = text[end + len(self.end_marker) :]
                self.in_tool_call = False
                continue

            start = text.find(self.start_marker)
            if start >= 0:
                output.append(text[:start])
                text = text[start:]
                self.in_tool_call = True
                continue

            suffix = self._partial_start_suffix(text)
            if suffix:
                output.append(text[: -len(suffix)])
                self.buffer = suffix
            else:
                output.append(text)
            return "".join(output)

        return "".join(output)

    def _partial_start_suffix(self, text: str) -> str:
        limit = min(len(text), len(self.start_marker) - 1)
        for size in range(limit, 0, -1):
            suffix = text[-size:]
            if self.start_marker.startswith(suffix):
                return suffix
        return ""


def _probe_clickhouse_status() -> tuple[str, str]:
    try:
        service = ClickHouseService(ClickHouseConfig.from_env(env_file=None))
        service.ping()
        return "ready", ""
    except ValueError as exc:
        LOGGER.info("Terminal ClickHouse readiness is not configured: %s", exc)
        return "unconfigured", "ClickHouse is not configured"
    except Exception:
        LOGGER.exception("Terminal ClickHouse readiness check failed")
        return "error", "ClickHouse is not ready"


async def _send_backend_status(websocket: WebSocket, session: TerminalSession) -> None:
    try:
        await websocket.send_json(_backend_status_event(session))
    except Exception:
        LOGGER.debug("Could not send terminal backend status", exc_info=True)


async def _warm_session_services(websocket: WebSocket, session: TerminalSession) -> None:
    _set_backend_service(session, "clickhouse", "warming")
    await _send_backend_status(websocket, session)
    state, detail = await asyncio.to_thread(_probe_clickhouse_status)
    _set_backend_service(session, "clickhouse", state, detail=detail)
    _save_session(session)
    await _send_backend_status(websocket, session)


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
def terminal_index() -> HTMLResponse:
    return HTMLResponse(render_terminal_index(asset_base="/terminal"))


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
def terminal_result_map_geojson(
    session_id: str,
    map_id: str,
    response: Response,
) -> dict[str, Any]:
    session = _get_session(session_id)
    if session is None or map_id not in session.map_results:
        raise HTTPException(status_code=404, detail="Unknown map result")
    result = session.map_results[map_id]
    features = list(result.get("features") or [])
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET"
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
    session = _get_session(session_id)
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
    _save_session(session)
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
        "backend_status": session.backend_status,
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


def _terminal_result_geojson_url(session: TerminalSession, map_id: str) -> str:
    public_base = public_terminal_base_url()
    if public_base:
        return f"{public_base}/map/{session.session_id}/{map_id}.geojson"
    return f"/terminal/map/{session.session_id}/{map_id}.geojson"


def _terminal_map_link(session: TerminalSession) -> str:
    if not terminal_result_maps_enabled():
        return ""

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

    date_iso = (dates or [load_city(session.city).default_map_date])[0]
    public_url = map_url_for(date_iso)
    result_url = _terminal_result_geojson_url(session, map_id)
    if public_url and public_terminal_base_url():
        separator = "&" if "#" in public_url else "#"
        fragment = urlencode(
            {
                "result_url": result_url,
                "event_ids": ",".join(event_ids),
            }
        )
        return f"\n\n[View on map]({public_url}{separator}{fragment})"
    return f"\n\n[View on map](/terminal/map/{session.session_id}/{map_id})"


def _terminal_map_command_search_query(text: str) -> str:
    query = map_command_query(text)
    if not query:
        return ""
    lowered = query.lower()
    if lowered.startswith("list "):
        return query
    if lowered.startswith(("event ", "events ")):
        return f"list {query}"
    return f"list events matching {query}"


def _terminal_result_map_disabled_reply() -> str:
    return (
        "Filtered terminal result maps are temporarily disabled. "
        "The public event maps still work for date browsing with `/map YYYY-MM-DD`, "
        "but search-result maps need the canonical map page to support `result_url` first."
    )


@app.websocket("/sessions/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str) -> None:
    await _websocket_session(websocket, session_id)


@app.websocket("/terminal/sessions/{session_id}")
async def terminal_websocket_session(websocket: WebSocket, session_id: str) -> None:
    await _websocket_session(websocket, session_id)


async def _websocket_session(websocket: WebSocket, session_id: str) -> None:
    session = _get_session(session_id)
    if session is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    await websocket.send_json(ready_event(session))
    await _send_backend_status(websocket, session)
    asyncio.create_task(_warm_session_services(websocket, session))

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
    session.map_results = {}
    session.backend_status = _initial_backend_status()
    _states[session.session_id] = session.state
    _save_session(session)
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
    started_at = time.monotonic()
    timeout_seconds = terminal_query_timeout_seconds()
    heartbeat_seconds = terminal_query_heartbeat_seconds()
    last_heartbeat_elapsed = 0.0

    def emit(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    worker = asyncio.create_task(asyncio.to_thread(_answer_in_thread, session, text, emit))

    async def send_timeout_error() -> None:
        elapsed = time.monotonic() - started_at
        _set_backend_service(session, "clickhouse", "error")
        _set_backend_service(session, "subconscious", "error")
        _save_session(session)
        await websocket.send_json(_backend_status_event(session))
        await websocket.send_json(
            {
                "type": "error",
                "error": terminal_query_timeout_reply(text, elapsed),
                "summary": terminal_query_timeout_summary(elapsed),
                "route": question_log_route(text),
                "duration_ms": int(elapsed * 1000),
            }
        )
        LOGGER.warning(
            "Terminal query timed out after %.1fs for session %s",
            elapsed,
            session.session_id,
        )

    while True:
        elapsed = time.monotonic() - started_at
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            await send_timeout_error()
            return
        wait_seconds = min(heartbeat_seconds, remaining)
        try:
            event = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started_at
            if elapsed >= timeout_seconds:
                await send_timeout_error()
                return
            if elapsed - last_heartbeat_elapsed >= heartbeat_seconds - 0.05:
                await websocket.send_json(
                    {
                        "type": "status",
                        "step": f"Still working after {int(elapsed)}s.",
                        "text": f"Still working after {int(elapsed)}s.",
                    }
                )
                last_heartbeat_elapsed = elapsed
            continue
        if event is None:
            await worker
            return
        await websocket.send_json(event)


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
    last_status_step: str | None = None

    def emit_status(step: str) -> None:
        nonlocal last_status_step
        display_step = terminal_status_step(step)
        if not display_step or display_step == last_status_step:
            return
        last_status_step = display_step
        update_chat_status(state, step=step)
        emit({"type": "status", "text": display_step, "step": display_step})

    def progress(step: str) -> None:
        emit_status(step)

    def visible_stream(partial: str) -> None:
        if partial:
            emit({"type": "delta", "text": partial, "mode": "replace"})

    def raw_stream(chunk: str) -> None:
        if chunk:
            emit({"type": "thinking_delta", "text": chunk, "expanded": False})

    try:
        with _city_lock:
            with active_city_override(session.city):
                if map_command_query(text) and not terminal_result_maps_enabled():
                    answer = _terminal_result_map_disabled_reply()
                    emit(
                        {
                            "type": "final",
                            "text": answer,
                            "route": question_log_route(text),
                            "usage": usage.as_dict(),
                            "duration_ms": int((time.monotonic() - started_at) * 1000),
                        }
                    )
                    return
                agent_text = _terminal_map_command_search_query(text)
                question_text = agent_text or text
                route, first_step = answer_route(question_text, state)
                uses_clickhouse = route.startswith("ClickHouse")
                uses_model = route == "ClickHouse agent query"
                update_chat_status(
                    state,
                    question=text,
                    route=route,
                    step=first_step,
                )
                emit_status(first_step)
                if uses_clickhouse:
                    _set_backend_service(session, "clickhouse", "working")
                if uses_model:
                    _set_backend_service(session, "subconscious", "working")
                if uses_clickhouse or uses_model:
                    emit(_backend_status_event(session))

                answer = answer_session_message(
                    LazySessionAgent(session),
                    _states,
                    session.session_id,
                    question_text,
                    progress=progress,
                    stream_callback=visible_stream,
                    raw_stream_callback=raw_stream if state.verbose else None,
                    token_usage_callback=usage.add,
                )
                map_link = _terminal_map_link(session)
                if agent_text:
                    if map_link:
                        answer += map_link
                    else:
                        answer += "\n\nNo mapped matching events found."
                else:
                    answer += map_link
                if uses_clickhouse:
                    _set_backend_service(session, "clickhouse", "ready")
                if uses_model and usage.calls:
                    _set_backend_service(session, "subconscious", "ready")
                elif uses_model:
                    _set_backend_service(session, "subconscious", "configured")
                if uses_clickhouse or uses_model:
                    emit(_backend_status_event(session))
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
        _set_backend_service(session, "clickhouse", "error")
        _set_backend_service(session, "subconscious", "error")
        emit(_backend_status_event(session))
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
        _save_session(session)
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
