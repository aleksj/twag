from __future__ import annotations

import os
from typing import Annotated, Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - exercised only when deps missing
    raise RuntimeError(
        "FastAPI dependencies are not installed. Run `pip install -e .`."
    ) from exc

from .client import ClickHouseService
from .config import ClickHouseConfig
from .subconscious_agent import add_default_limit, validate_nytw_query


class QueryRequest(BaseModel):
    sql: str = Field(
        ...,
        description="One read-only SQL statement against nytw_* ClickHouse tables.",
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


@app.get("/health")
def health() -> dict[str, Any]:
    service = _service()
    return {"ok": service.ping(), "config": service.config.safe_dict()}


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

    uvicorn.run(
        "twag_clickhouse.tool_server:app",
        host=os.getenv("NYTW_TOOL_HOST", "0.0.0.0"),
        port=int(os.getenv("NYTW_TOOL_PORT", "8000")),
        reload=os.getenv("NYTW_TOOL_RELOAD", "").lower() in {"1", "true", "yes"},
    )


if __name__ == "__main__":
    main()
