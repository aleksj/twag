from __future__ import annotations

from typing import Any, Sequence

from twag_clickhouse.senso import (
    SensoNode,
    chunk_text,
    extract_senso_text,
    senso_sync_overview,
    sync_senso_kb,
)


class FakeClickHouse:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: dict[str, list[Sequence[Any]]] = {}

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> None:
        self.commands.append(sql)

    def insert_rows(
        self,
        table: str,
        rows: Sequence[Sequence[Any]],
        column_names: Sequence[str],
    ) -> None:
        self.inserts.setdefault(table, []).extend(rows)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if "FROM senso_kb_documents" in sql and "GROUP BY kb_node_id" in sql:
            return [
                {
                    "kb_node_id": str(row[0]),
                    "title": str(row[2]),
                    "content_hash": str(row[8]),
                    "synced_at": row[9],
                }
                for row in self.inserts.get("senso_kb_documents", [])
            ]
        if "FROM senso_kb_nodes" in sql and "GROUP BY kb_node_id" in sql:
            return [
                {
                    "kb_node_id": str(row[0]),
                    "parent_id": str(row[1]),
                    "path": str(row[2]),
                    "name": str(row[3]),
                    "node_type": str(row[4]),
                    "content_id": str(row[5]),
                    "version": row[6],
                    "processing_status": str(row[7]),
                    "raw_json": str(row[8]),
                    "synced_at": row[9],
                }
                for row in self.inserts.get("senso_kb_nodes", [])
            ]
        return [
            {
                "nodes": len(self.inserts.get("senso_kb_nodes", [])),
                "documents": len(self.inserts.get("senso_kb_documents", [])),
                "chunks": len(self.inserts.get("senso_kb_chunks", [])),
            }
        ]


class FakeSenso:
    def __init__(self) -> None:
        self.content_calls = 0
        self.download_url_calls = 0
        self.download_text_calls = 0

    def iter_nodes(self):
        yield SensoNode(
            kb_node_id="folder-1",
            name="Policies",
            node_type="folder",
            parent_id="",
            path="Policies",
            raw={"kb_node_id": "folder-1", "name": "Policies", "type": "folder"},
        )
        yield SensoNode(
            kb_node_id="doc-1",
            name="Refund Policy",
            node_type="document",
            parent_id="folder-1",
            path="Policies/Refund Policy",
            raw={
                "kb_node_id": "doc-1",
                "name": "Refund Policy",
                "type": "document",
                "content_id": "content-1",
                "processing_status": "complete",
            },
        )

    def get_content(self, node_id: str) -> dict[str, str]:
        self.content_calls += 1
        return {
            "id": "content-1",
            "title": "Refund Policy",
            "summary": "Refund rules",
            "text": "Customers can request a refund within 30 days. Final sale items are excluded.",
        }

    def get_download_url(self, node_id: str) -> dict[str, str]:
        self.download_url_calls += 1
        return {}

    def download_text(self, url: str, *, filename: str = "") -> str:
        self.download_text_calls += 1
        return ""


def test_extract_senso_text_prefers_direct_text_fields() -> None:
    assert extract_senso_text({"content": {"markdown": "# Hello\n\nWorld"}}) == "# Hello World"


def test_chunk_text_adds_overlap() -> None:
    chunks = chunk_text("a" * 120, chunk_chars=50, overlap=10)

    assert len(chunks) == 3
    assert chunks[1].startswith("a" * 10)


def test_sync_senso_kb_creates_schema_and_inserts_nodes_documents_chunks() -> None:
    clickhouse = FakeClickHouse()

    result = sync_senso_kb(
        clickhouse,  # type: ignore[arg-type]
        FakeSenso(),  # type: ignore[arg-type]
        replace=True,
        chunk_chars=80,
        chunk_overlap=5,
    )

    assert result["status"] == "complete"
    assert result["nodes"] == 2
    assert result["documents"] == 1
    assert result["chunks"] >= 1
    assert any("CREATE TABLE IF NOT EXISTS senso_kb_nodes" in sql for sql in clickhouse.commands)
    assert any("CREATE TABLE IF NOT EXISTS senso_sync_changes" in sql for sql in clickhouse.commands)
    assert any("TRUNCATE TABLE IF EXISTS senso_kb_nodes" in sql for sql in clickhouse.commands)
    assert clickhouse.inserts["senso_kb_documents"][0][2] == "Refund Policy"
    assert "30 days" in clickhouse.inserts["senso_kb_chunks"][0][4]
    assert clickhouse.inserts["senso_sync_runs"][0][3] == "complete"
    assert clickhouse.inserts["senso_sync_changes"][0][2] == "inserted"
    assert result["changes"]["inserted"] == 1


def test_sync_senso_kb_skips_content_download_when_node_metadata_is_unchanged() -> None:
    clickhouse = FakeClickHouse()
    first_senso = FakeSenso()

    first = sync_senso_kb(
        clickhouse,  # type: ignore[arg-type]
        first_senso,  # type: ignore[arg-type]
        chunk_chars=80,
        chunk_overlap=5,
    )

    second_senso = FakeSenso()
    second = sync_senso_kb(
        clickhouse,  # type: ignore[arg-type]
        second_senso,  # type: ignore[arg-type]
        chunk_chars=80,
        chunk_overlap=5,
    )

    assert first["changes"]["inserted"] == 1
    assert second["changes"]["unchanged"] == 1
    assert second_senso.content_calls == 0
    assert second_senso.download_url_calls == 0
    assert second_senso.download_text_calls == 0


def test_senso_sync_overview_reads_runs_and_changes() -> None:
    class OverviewClickHouse:
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict]:
            if "FROM senso_sync_runs" in sql:
                return [
                    {
                        "run_id": "run-1",
                        "status": "complete",
                        "nodes": 2,
                        "documents": 1,
                        "chunks": 1,
                    }
                ]
            if "GROUP BY run_id, change_type" in sql:
                return [
                    {"run_id": "run-1", "change_type": "inserted", "count": 1},
                    {"run_id": "run-1", "change_type": "unchanged", "count": 3},
                ]
            if "FROM senso_sync_changes" in sql:
                return [{"run_id": "run-1", "change_type": "inserted", "title": "Refund Policy"}]
            return []

    overview = senso_sync_overview(OverviewClickHouse(), limit=1)  # type: ignore[arg-type]

    assert overview["runs"][0]["changes"]["inserted"] == 1
    assert overview["runs"][0]["changes"]["unchanged"] == 3
    assert overview["changed_items"][0]["title"] == "Refund Policy"
