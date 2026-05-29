from __future__ import annotations

from typing import Any, Sequence

from twag_clickhouse import calendar_sync
from twag_clickhouse.calendar_sync import render_event_markdown, sync_city_calendar
from twag_clickhouse.city import NYC
from twag_clickhouse.nytw import EVENT_COLUMNS, MANIFEST_COLUMNS, parse_event_markdown


class FakeClickHouse:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: dict[str, list[Sequence[Any]]] = {}

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> None:
        self.commands.append(sql)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return []

    def insert_rows(
        self,
        table: str,
        rows: Sequence[Sequence[Any]],
        column_names: Sequence[str],
    ) -> None:
        self.inserts.setdefault(table, []).extend(rows)


def test_sync_city_calendar_refreshes_tables_and_records_changes(monkeypatch) -> None:
    calendar_row = {
        "id": "evt-1",
        "name": "AI Builders Breakfast",
        "date": "2026-06-02",
        "time": "09:00",
        "location": "SoHo",
        "company": "Data.Flowers",
        "externalHref": "https://partiful.com/e/evt-1",
        "isInviteOnly": False,
        "hosts": ["Data.Flowers"],
    }
    manifest = calendar_sync._manifest_from_calendar_row(calendar_row)
    filename, raw_markdown = render_event_markdown(manifest, None)
    event = parse_event_markdown(raw_markdown, source_path=f"live/{filename}")

    monkeypatch.setattr(
        calendar_sync,
        "_fetch_event_dicts",
        lambda manifests, **kwargs: [event],
    )

    clickhouse = FakeClickHouse()
    result = sync_city_calendar(
        clickhouse,  # type: ignore[arg-type]
        NYC,
        calendar_rows=[calendar_row],
    )

    assert result["status"] == "complete"
    assert result["events"] == 1
    assert result["manifest"] == 1
    assert result["changes"]["inserted"] == 2
    assert any("CREATE TABLE IF NOT EXISTS nytw_sync_runs" in sql for sql in clickhouse.commands)
    assert any("CREATE TABLE IF NOT EXISTS nytw_sync_changes" in sql for sql in clickhouse.commands)
    assert not any("TRUNCATE TABLE" in sql for sql in clickhouse.commands)
    assert clickhouse.inserts["nytw_calendar_events"][0][2] == "evt-1"
    assert clickhouse.inserts["nytw_calendar_manifest"][0][2] == "evt-1"
    assert {row[2] for row in clickhouse.inserts["nytw_sync_changes"]} == {
        "nytw_events",
        "nytw_manifest",
    }
    assert clickhouse.inserts["nytw_sync_runs"][0][5] == "complete"


def test_sync_city_calendar_reuses_unchanged_event_rows(monkeypatch) -> None:
    calendar_row = {
        "id": "evt-1",
        "name": "AI Builders Breakfast",
        "date": "2026-06-02",
        "time": "09:00",
        "location": "SoHo",
        "company": "Data.Flowers",
        "externalHref": "https://partiful.com/e/evt-1",
        "isInviteOnly": False,
        "hosts": ["Data.Flowers"],
    }
    manifest = calendar_sync._manifest_from_calendar_row(calendar_row)
    filename, raw_markdown = render_event_markdown(manifest, None)
    event = parse_event_markdown(raw_markdown, source_path=f"live/{filename}")
    existing_event = {column: event.get(column) for column in EVENT_COLUMNS}
    existing_manifest = dict(
        zip(
            MANIFEST_COLUMNS,
            next(calendar_sync._manifest_tuples([manifest])),
            strict=False,
        )
    )

    class FakeWithExisting(FakeClickHouse):
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "FROM nytw_events" in sql:
                return [existing_event]
            if "FROM nytw_manifest" in sql:
                return [existing_manifest]
            return []

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("unchanged rows should not be refetched")

    monkeypatch.setattr(calendar_sync, "_fetch_event_dicts", fail_fetch)

    clickhouse = FakeWithExisting()
    result = sync_city_calendar(
        clickhouse,  # type: ignore[arg-type]
        NYC,
        calendar_rows=[calendar_row],
    )

    assert result["status"] == "complete"
    assert result["fetched_events"] == 0
    assert result["changes"]["unchanged"] == 2
    assert clickhouse.inserts["nytw_calendar_events"][0][2] == "evt-1"
