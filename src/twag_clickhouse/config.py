from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Expected integer value, got {value!r}") from exc


@dataclass(frozen=True)
class ClickHouseConfig:
    host: str
    username: str
    password: str
    service_id: str = ""
    database: str = "default"
    port: int = 8443
    secure: bool = True
    connect_timeout: int = 10
    send_receive_timeout: int = 30

    @classmethod
    def from_env(cls, *, env_file: str | None = ".env") -> "ClickHouseConfig":
        if env_file and load_dotenv:
            load_dotenv(env_file, override=False)

        host = os.getenv("CLICKHOUSE_HOST", "").strip()
        username = os.getenv("CLICKHOUSE_USERNAME", "default").strip()
        password = (
            os.getenv("CLICKHOUSE_PASSWORD", "").strip()
            or os.getenv("CLICKHOUSE_API_KEY", "").strip()
        )

        if not host:
            raise ValueError("CLICKHOUSE_HOST is required")

        if not password:
            raise ValueError("CLICKHOUSE_PASSWORD or CLICKHOUSE_API_KEY is required")

        secure = _as_bool(os.getenv("CLICKHOUSE_SECURE"), True)
        default_port = 8443 if secure else 8123

        return cls(
            host=host,
            username=username,
            password=password,
            service_id=os.getenv("CLICKHOUSE_SERVICE_ID", "").strip(),
            database=os.getenv("CLICKHOUSE_DATABASE", "default").strip() or "default",
            port=_as_int(os.getenv("CLICKHOUSE_PORT"), default_port),
            secure=secure,
            connect_timeout=_as_int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT"), 10),
            send_receive_timeout=_as_int(
                os.getenv("CLICKHOUSE_SEND_RECEIVE_TIMEOUT"), 30
            ),
        )

    def safe_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "username": self.username,
            "password": "***",
            "service_id": self.service_id,
            "database": self.database,
            "port": self.port,
            "secure": self.secure,
            "connect_timeout": self.connect_timeout,
            "send_receive_timeout": self.send_receive_timeout,
        }
