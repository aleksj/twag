from __future__ import annotations

import logging
import os
import threading
from typing import Annotated, Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from fastapi import FastAPI, Header, HTTPException, Response
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - exercised only when deps missing
    raise RuntimeError(
        "FastAPI dependencies are not installed. Run `pip install -e .`."
    ) from exc

from .client import ClickHouseService
from .config import ClickHouseConfig
from .calendar_sync import CalendarSyncConfig, sync_configured_calendars
from .senso import SensoConfig, SensoService, sleep_before_next_sync, sync_senso_kb
from .subconscious_agent import add_default_limit, validate_nytw_query


logger = logging.getLogger(__name__)
_senso_sync_thread_started = False
_calendar_sync_thread_started = False
UVICORN_SCANNER_NOISE = "Invalid HTTP request received."


class _UvicornScannerNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return UVICORN_SCANNER_NOISE not in record.getMessage()


class QueryRequest(BaseModel):
    sql: str = Field(
        ...,
        description="One read-only SQL statement against nytw_* or synced senso_* ClickHouse tables.",
    )


def _service() -> ClickHouseService:
    return ClickHouseService(ClickHouseConfig.from_env())


def _tool_token() -> str:
    return os.getenv("NYTW_TOOL_TOKEN", "").strip()


def _check_token(x_tool_token: str | None) -> None:
    expected = _tool_token()
    if expected and x_tool_token != expected:
        raise HTTPException(status_code=401, detail="Invalid tool token")


app = FastAPI(
    title="NYTechWeek ClickHouse Tool",
    description="Read-only ClickHouse query tool for Subconscious NYTechWeek agents.",
    version="0.1.0",
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _configure_uvicorn_logging() -> None:
    if not _env_bool("NYTW_TOOL_SUPPRESS_SCANNER_NOISE", True):
        return
    uvicorn_error = logging.getLogger("uvicorn.error")
    if not any(isinstance(filter_, _UvicornScannerNoiseFilter) for filter_ in uvicorn_error.filters):
        uvicorn_error.addFilter(_UvicornScannerNoiseFilter())


def _run_senso_sync_loop() -> None:
    interval = int(os.getenv("SENSO_SYNC_INTERVAL_SECONDS", "3600"))
    replace = _env_bool("SENSO_SYNC_REPLACE", False)
    batch_size = int(os.getenv("SENSO_SYNC_BATCH_SIZE", "500"))
    chunk_chars = int(os.getenv("SENSO_CHUNK_CHARS", "3500"))
    chunk_overlap = int(os.getenv("SENSO_CHUNK_OVERLAP", "300"))

    while True:
        config = SensoConfig.from_env()
        if config is None:
            logger.info("SENSO_API_KEY is not set; Senso ClickHouse sync is disabled.")
            return
        try:
            result = sync_senso_kb(
                _service(),
                SensoService(config),
                replace=replace,
                batch_size=batch_size,
                chunk_chars=chunk_chars,
                chunk_overlap=chunk_overlap,
            )
            logger.info("Senso ClickHouse sync complete: %s", result)
        except Exception:
            logger.exception("Senso ClickHouse sync failed")

        if interval <= 0:
            return
        sleep_before_next_sync(interval)


@app.on_event("startup")
def start_senso_sync() -> None:
    global _senso_sync_thread_started
    if _senso_sync_thread_started:
        return
    if not _env_bool("SENSO_SYNC_ENABLED", True):
        return
    if not os.getenv("SENSO_API_KEY", "").strip():
        return
    _senso_sync_thread_started = True
    thread = threading.Thread(target=_run_senso_sync_loop, daemon=True)
    thread.start()


def _run_calendar_sync_loop() -> None:
    config = CalendarSyncConfig.from_env()
    while True:
        try:
            result = sync_configured_calendars(_service(), config)
            logger.info("Tech Week calendar ClickHouse sync complete: %s", result)
        except Exception:
            logger.exception("Tech Week calendar ClickHouse sync failed")

        if config.interval_seconds <= 0:
            return
        sleep_before_next_sync(config.interval_seconds)


@app.on_event("startup")
def start_calendar_sync() -> None:
    global _calendar_sync_thread_started
    if _calendar_sync_thread_started:
        return
    if not _env_bool("TECHWEEK_CALENDAR_SYNC_ENABLED", True):
        return
    _calendar_sync_thread_started = True
    thread = threading.Thread(target=_run_calendar_sync_loop, daemon=True)
    thread.start()


@app.get("/health")
def health() -> dict[str, Any]:
    service = _service()
    return {"ok": service.ping(), "config": service.config.safe_dict()}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "NYTechWeek ClickHouse Tool",
        "sync": {
            "calendar": "TECHWEEK_CALENDAR_SYNC_ENABLED",
            "senso": "SENSO_SYNC_ENABLED",
        },
        "endpoints": {
            "health": "/health",
            "query": "/query",
        },
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.post("/query")
def query(
    request: QueryRequest,
    x_tool_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_token(x_tool_token)

    try:
        sql = add_default_limit(validate_nytw_query(request.sql))
        rows = _service().query(sql)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "sql": sql,
        "row_count": len(rows),
        "rows": rows,
    }


def main() -> None:
    if load_dotenv:
        load_dotenv(".env", override=False)

    import uvicorn
    _configure_uvicorn_logging()

    uvicorn.run(
        "twag_clickhouse.tool_server:app",
        host=os.getenv("NYTW_TOOL_HOST", "localhost"),
        port=int(os.getenv("NYTW_TOOL_PORT", "8000")),
        reload=os.getenv("NYTW_TOOL_RELOAD", "").lower() in {"1", "true", "yes"},
        access_log=_env_bool("NYTW_TOOL_ACCESS_LOG", False),
        log_level=os.getenv("NYTW_TOOL_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
