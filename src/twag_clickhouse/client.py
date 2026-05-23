from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .config import ClickHouseConfig


class ClickHouseService:
    def __init__(self, config: ClickHouseConfig):
        self.config = config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import clickhouse_connect
            except ImportError as exc:
                raise RuntimeError(
                    "clickhouse-connect is not installed. Run `pip install -e .`."
                ) from exc

            self._client = clickhouse_connect.get_client(
                host=self.config.host,
                port=self.config.port,
                username=self.config.username,
                password=self.config.password,
                database=self.config.database,
                secure=self.config.secure,
                connect_timeout=self.config.connect_timeout,
                send_receive_timeout=self.config.send_receive_timeout,
            )

        return self._client

    def ping(self) -> bool:
        return bool(self.client.ping())

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict]:
        result = self.client.query(sql, parameters=parameters or {})
        return list(result.named_results())

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        return self.client.command(sql, parameters=parameters or {})

    def insert_rows(
        self,
        table: str,
        rows: Sequence[Sequence[Any]],
        column_names: Sequence[str],
    ) -> None:
        self.client.insert(table, rows, column_names=list(column_names))

    def create_demo_table(self) -> None:
        self.command(
            """
            CREATE TABLE IF NOT EXISTS analytics_events
            (
                event_time DateTime64(3) DEFAULT now64(3),
                event_name LowCardinality(String),
                properties String
            )
            ENGINE = MergeTree
            ORDER BY (event_time, event_name)
            """
        )

    def insert_event(self, event_name: str, properties_json: str = "{}") -> None:
        self.insert_rows(
            "analytics_events",
            rows=[(event_name, properties_json)],
            column_names=["event_name", "properties"],
        )
