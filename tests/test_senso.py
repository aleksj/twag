from __future__ import annotations

from typing import Any, Sequence

from twag_clickhouse.senso import (
    SensoNode,
    chunk_text,
    extract_senso_text,
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

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, int]]:
        return [
            {
                "nodes": len(self.inserts.get("senso_kb_nodes", [])),
                "documents": len(self.inserts.get("senso_kb_documents", [])),
                "chunks": len(self.inserts.get("senso_kb_chunks", [])),
            }
        ]


class FakeSenso:
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
        return {
            "id": "content-1",
            "title": "Refund Policy",
            "summary": "Refund rules",
            "text": "Customers can request a refund within 30 days. Final sale items are excluded.",
        }

    def get_download_url(self, node_id: str) -> dict[str, str]:
        return {}


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
    assert any("TRUNCATE TABLE IF EXISTS senso_kb_nodes" in sql for sql in clickhouse.commands)
    assert clickhouse.inserts["senso_kb_documents"][0][2] == "Refund Policy"
    assert "30 days" in clickhouse.inserts["senso_kb_chunks"][0][4]
    assert clickhouse.inserts["senso_sync_runs"][0][3] == "complete"
