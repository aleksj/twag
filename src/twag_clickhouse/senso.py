from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence
from uuid import uuid4

from .client import ClickHouseService


DEFAULT_SENSO_BASE_URL = "https://apiv2.senso.ai/api/v1"

SENSO_NODE_COLUMNS = (
    "kb_node_id",
    "parent_id",
    "path",
    "name",
    "node_type",
    "content_id",
    "version",
    "processing_status",
    "raw_json",
    "synced_at",
)
SENSO_DOCUMENT_COLUMNS = (
    "kb_node_id",
    "content_id",
    "title",
    "summary",
    "text",
    "content_json",
    "download_url",
    "filename",
    "content_hash",
    "synced_at",
)
SENSO_CHUNK_COLUMNS = (
    "kb_node_id",
    "chunk_index",
    "path",
    "title",
    "chunk_text",
    "token_estimate",
    "synced_at",
)
SENSO_SYNC_RUN_COLUMNS = (
    "run_id",
    "started_at",
    "finished_at",
    "status",
    "nodes",
    "documents",
    "chunks",
    "error",
)


@dataclass(frozen=True)
class SensoConfig:
    api_key: str
    base_url: str = DEFAULT_SENSO_BASE_URL
    org_id: str = ""
    org_slug: str = ""
    max_download_bytes: int = 2_000_000

    @classmethod
    def from_env(cls) -> "SensoConfig | None":
        api_key = os.getenv("SENSO_API_KEY", "").strip()
        if not api_key:
            return None

        return cls(
            api_key=api_key,
            base_url=os.getenv("SENSO_BASE_URL", DEFAULT_SENSO_BASE_URL).strip()
            or DEFAULT_SENSO_BASE_URL,
            org_id=os.getenv("SENSO_ORG_ID", "").strip(),
            org_slug=os.getenv("SENSO_ORG_SLUG", "").strip(),
            max_download_bytes=int(os.getenv("SENSO_MAX_DOWNLOAD_BYTES", "2000000")),
        )


@dataclass(frozen=True)
class SensoNode:
    kb_node_id: str
    name: str
    node_type: str
    parent_id: str
    path: str
    raw: dict[str, Any]

    @property
    def is_folder(self) -> bool:
        return self.node_type.lower() == "folder"


class SensoService:
    def __init__(self, config: SensoConfig):
        self.config = config

    def list_root_nodes(self) -> list[dict[str, Any]]:
        return self._nodes_from_response(self._request_json("GET", "/org/kb/my-files"))

    def list_content(self, *, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self._request_json(
                "GET",
                "/content",
                query={"limit": str(limit), "offset": str(offset)},
            )
            page = response.get("items") or response.get("content") or response.get("results") or []
            page_items = [item for item in page if isinstance(item, dict)]
            items.extend(page_items)
            if len(page_items) < limit:
                break
            offset += limit
        return items

    def list_children(self, node_id: str) -> list[dict[str, Any]]:
        path = f"/org/kb/nodes/{urllib.parse.quote(node_id)}/children"
        return self._nodes_from_response(self._request_json("GET", path))

    def get_content(self, node_id: str) -> dict[str, Any]:
        path = f"/org/kb/nodes/{urllib.parse.quote(node_id)}/content"
        return self._request_json("GET", path)

    def get_download_url(self, node_id: str) -> dict[str, Any]:
        path = f"/org/kb/nodes/{urllib.parse.quote(node_id)}/download-url"
        return self._request_json("GET", path)

    def iter_nodes(self) -> Iterator[SensoNode]:
        yield from self._iter_nodes(self.list_root_nodes(), parent_id="", parent_path="")

    def download_text(self, url: str, *, filename: str = "") -> str:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=120) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(self.config.max_download_bytes + 1)

        if len(data) > self.config.max_download_bytes:
            data = data[: self.config.max_download_bytes]

        if not _looks_text_download(content_type, filename):
            return ""

        return data.decode("utf-8", errors="replace")

    def _iter_nodes(
        self,
        items: list[dict[str, Any]],
        *,
        parent_id: str,
        parent_path: str,
    ) -> Iterator[SensoNode]:
        for item in items:
            node_id = _first_string(item, "kb_node_id", "node_id", "id", "content_id")
            if not node_id:
                continue
            name = _first_string(item, "name", "filename", "title") or node_id
            node_type = _first_string(item, "type", "node_type", "kind", "content_type") or "document"
            path = f"{parent_path}/{name}" if parent_path else name
            node = SensoNode(
                kb_node_id=node_id,
                name=name,
                node_type=node_type,
                parent_id=parent_id,
                path=path,
                raw=item,
            )
            yield node
            if node.is_folder:
                yield from self._iter_nodes(
                    self.list_children(node_id),
                    parent_id=node_id,
                    parent_path=path,
                )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if query is None:
            query = {}
        if self.config.org_id and "org_id" not in query:
            query["org_id"] = self.config.org_id
        encoded_query = urllib.parse.urlencode(query)
        url = f"{self.config.base_url.rstrip('/')}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "X-API-Key": self.config.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "twag-clickhouse-senso-sync/0.1",
            },
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Senso API error {exc.code}: {detail}") from exc

    @staticmethod
    def _nodes_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = response.get("nodes") or response.get("items") or response.get("results") or []
        return [node for node in nodes if isinstance(node, dict)]


def create_senso_tables(service: ClickHouseService) -> None:
    service.command(
        """
        CREATE TABLE IF NOT EXISTS senso_kb_nodes
        (
            kb_node_id String,
            parent_id String,
            path String,
            name String,
            node_type LowCardinality(String),
            content_id String,
            version Nullable(UInt32),
            processing_status LowCardinality(String),
            raw_json String,
            synced_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(synced_at)
        ORDER BY kb_node_id
        """
    )
    service.command(
        """
        CREATE TABLE IF NOT EXISTS senso_kb_documents
        (
            kb_node_id String,
            content_id String,
            title String,
            summary String,
            text String,
            content_json String,
            download_url String,
            filename String,
            content_hash String,
            synced_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(synced_at)
        ORDER BY kb_node_id
        """
    )
    service.command(
        """
        CREATE TABLE IF NOT EXISTS senso_kb_chunks
        (
            kb_node_id String,
            chunk_index UInt32,
            path String,
            title String,
            chunk_text String,
            token_estimate UInt32,
            synced_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(synced_at)
        ORDER BY (kb_node_id, chunk_index)
        """
    )
    service.command(
        """
        CREATE TABLE IF NOT EXISTS senso_sync_runs
        (
            run_id String,
            started_at DateTime64(3, 'UTC'),
            finished_at DateTime64(3, 'UTC'),
            status LowCardinality(String),
            nodes UInt32,
            documents UInt32,
            chunks UInt32,
            error String
        )
        ENGINE = MergeTree
        ORDER BY (started_at, run_id)
        """
    )


def truncate_senso_tables(service: ClickHouseService) -> None:
    for table in ("senso_kb_nodes", "senso_kb_documents", "senso_kb_chunks"):
        service.command(f"TRUNCATE TABLE IF EXISTS {table}")


def sync_senso_kb(
    service: ClickHouseService,
    senso: SensoService,
    *,
    replace: bool = False,
    batch_size: int = 500,
    chunk_chars: int = 3500,
    chunk_overlap: int = 300,
) -> dict[str, int | str]:
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc)
    create_senso_tables(service)
    if replace:
        truncate_senso_tables(service)

    node_rows: list[tuple[Any, ...]] = []
    document_rows: list[tuple[Any, ...]] = []
    chunk_rows: list[tuple[Any, ...]] = []
    error = ""
    status = "complete"

    try:
        for node in senso.iter_nodes():
            synced_at = datetime.now(timezone.utc)
            content_id = _first_string(node.raw, "content_id", "contentId")
            version = _as_int_or_none(node.raw.get("version") or node.raw.get("version_num"))
            processing_status = _first_string(node.raw, "processing_status", "status")
            node_rows.append(
                (
                    node.kb_node_id,
                    node.parent_id,
                    node.path,
                    node.name,
                    node.node_type,
                    content_id,
                    version,
                    processing_status,
                    json.dumps(node.raw, sort_keys=True, default=str),
                    synced_at,
                )
            )

            if not node.is_folder:
                content = _safe_content(senso, node.kb_node_id)
                download = _safe_download_url(senso, node.kb_node_id)
                filename = _first_string(download, "filename") or node.name
                download_url = _first_string(download, "url", "download_url")
                text = extract_senso_text(content)
                if not text and download_url:
                    text = senso.download_text(download_url, filename=filename)
                title = _first_string(content, "title", "name") or node.name
                summary = _first_string(content, "summary", "description")
                content_hash = _first_string(content, "content_hash_md5", "hash", "md5")
                document_rows.append(
                    (
                        node.kb_node_id,
                        _first_string(content, "content_id", "id") or content_id,
                        title,
                        summary,
                        text,
                        json.dumps(content, sort_keys=True, default=str),
                        download_url,
                        filename,
                        content_hash,
                        synced_at,
                    )
                )
                for index, chunk in enumerate(chunk_text(text, chunk_chars, chunk_overlap)):
                    chunk_rows.append(
                        (
                            node.kb_node_id,
                            index,
                            node.path,
                            title,
                            chunk,
                            max(1, len(chunk) // 4),
                            synced_at,
                        )
                    )

            if len(node_rows) >= batch_size:
                _insert(service, "senso_kb_nodes", node_rows, SENSO_NODE_COLUMNS)
                node_rows = []
            if len(document_rows) >= batch_size:
                _insert(service, "senso_kb_documents", document_rows, SENSO_DOCUMENT_COLUMNS)
                document_rows = []
            if len(chunk_rows) >= batch_size:
                _insert(service, "senso_kb_chunks", chunk_rows, SENSO_CHUNK_COLUMNS)
                chunk_rows = []

        _insert(service, "senso_kb_nodes", node_rows, SENSO_NODE_COLUMNS)
        _insert(service, "senso_kb_documents", document_rows, SENSO_DOCUMENT_COLUMNS)
        _insert(service, "senso_kb_chunks", chunk_rows, SENSO_CHUNK_COLUMNS)
    except Exception as exc:
        status = "failed"
        error = str(exc)
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        counts = _latest_senso_counts(service)
        service.insert_rows(
            "senso_sync_runs",
            [
                (
                    run_id,
                    started_at,
                    finished_at,
                    status,
                    counts["nodes"],
                    counts["documents"],
                    counts["chunks"],
                    error,
                )
            ],
            SENSO_SYNC_RUN_COLUMNS,
        )

    return {
        "run_id": run_id,
        "status": status,
        **_latest_senso_counts(service),
    }


def extract_senso_text(value: Any) -> str:
    direct_keys = (
        "text",
        "raw_text",
        "markdown",
        "raw_markdown",
        "body",
        "content_text",
    )
    if isinstance(value, dict):
        for key in direct_keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return _clean_text(item)
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            return _clean_text(content)
        if isinstance(content, dict):
            nested = extract_senso_text(content)
            if nested:
                return nested
        strings = [
            item
            for item in _walk_strings(value)
            if len(item.strip()) > 200 and not item.strip().startswith(("http://", "https://"))
        ]
        return _clean_text(max(strings, key=len)) if strings else ""
    if isinstance(value, str):
        return _clean_text(value)
    return ""


def chunk_text(text: str, chunk_chars: int = 3500, overlap: int = 300) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            split_at = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if split_at > start + chunk_chars // 2:
                end = split_at + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return [chunk for chunk in chunks if chunk]


def _insert(
    service: ClickHouseService,
    table: str,
    rows: Sequence[Sequence[Any]],
    columns: Sequence[str],
) -> None:
    if rows:
        service.insert_rows(table, rows, columns)


def _safe_content(senso: SensoService, node_id: str) -> dict[str, Any]:
    try:
        return senso.get_content(node_id)
    except Exception as exc:
        return {"error": str(exc)}


def _safe_download_url(senso: SensoService, node_id: str) -> dict[str, Any]:
    try:
        return senso.get_download_url(node_id)
    except Exception:
        return {}


def _latest_senso_counts(service: ClickHouseService) -> dict[str, int]:
    # Counts from local buffers are harder to keep exact after batched inserts.
    # Querying ClickHouse also verifies that the target tables are readable.
    try:
        rows = service.query(
            """
            SELECT
              (SELECT count() FROM senso_kb_nodes) AS nodes,
              (SELECT count() FROM senso_kb_documents) AS documents,
              (SELECT count() FROM senso_kb_chunks) AS chunks
            """
        )
        if rows:
            return {
                "nodes": int(rows[0].get("nodes", 0)),
                "documents": int(rows[0].get("documents", 0)),
                "chunks": int(rows[0].get("chunks", 0)),
            }
    except Exception:
        pass
    return {"nodes": 0, "documents": 0, "chunks": 0}


def _first_string(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if item is not None:
            return str(item)
    return ""


def _as_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _looks_text_download(content_type: str, filename: str) -> bool:
    content_type = content_type.lower()
    filename = filename.lower()
    if content_type.startswith("text/") or "json" in content_type or "markdown" in content_type:
        return True
    return filename.endswith((".txt", ".md", ".markdown", ".json", ".csv", ".html", ".htm"))


def sleep_before_next_sync(interval_seconds: int) -> None:
    if interval_seconds > 0:
        time.sleep(interval_seconds)
