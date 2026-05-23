from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .client import ClickHouseService


PLATFORM_ADMIN_ID = "7DFu4rITofNzKIjA7hCx"
FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---\n(.*)", re.S)
DESCRIPTION_RE = re.compile(r"\n## Description\n(.*?)(?:\n---\n\n\[(?:Apply|RSVP) on Partiful)", re.S)


@dataclass(frozen=True)
class NytwDataset:
    source_dir: Path
    events_dir: Path
    users_path: Path
    manifest_path: Path

    @classmethod
    def from_path(cls, source_dir: str | Path) -> "NytwDataset":
        root = Path(source_dir).expanduser().resolve()
        return cls(
            source_dir=root,
            events_dir=root / "events",
            users_path=root / "users.json",
            manifest_path=root / "manifest.json",
        )

    def validate(self) -> None:
        if not self.events_dir.is_dir():
            raise FileNotFoundError(f"Missing events directory: {self.events_dir}")
        if not self.users_path.is_file():
            raise FileNotFoundError(f"Missing users file: {self.users_path}")
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"Missing manifest file: {self.manifest_path}")


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"true", "false"}:
        return value == "true"
    if value == "null":
        return None
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
    try:
        return int(value)
    except ValueError:
        return value


def parse_frontmatter(block: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    def flush_multiline() -> None:
        nonlocal current_key, current_lines
        if current_key:
            data[current_key] = "\n".join(current_lines).strip()
            current_key = None
            current_lines = []

    for line in block.splitlines():
        if current_key and (line.startswith(" ") or line.startswith("\t")):
            current_lines.append(line.strip())
            continue

        flush_multiline()
        if ":" not in line:
            continue

        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()

        if raw_value in {"|", ">"}:
            current_key = key
            current_lines = []
        else:
            data[key] = parse_scalar(raw_value)

    flush_multiline()
    return data


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def as_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def as_bool(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else False


def as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [as_string(item) for item in value]


def extract_description(markdown_body: str) -> str:
    match = DESCRIPTION_RE.search("\n" + markdown_body)
    if not match:
        return ""
    return match.group(1).strip()


def parse_event_file(path: Path, source_dir: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"Missing YAML frontmatter in {path}")

    frontmatter = parse_frontmatter(match.group(1))
    markdown_body = match.group(2).strip()
    event_date = parse_date(frontmatter.get("date"))
    start_at = parse_datetime(frontmatter.get("start_iso"))
    end_at = parse_datetime(frontmatter.get("end_iso"))
    canceled_at = parse_datetime(frontmatter.get("canceled_at"))
    owner_ids = as_string_list(frontmatter.get("owner_ids"))
    badges = as_string_list(frontmatter.get("badges"))

    return {
        "event_id": as_string(frontmatter.get("event_id")),
        "source_path": str(path.relative_to(source_dir)),
        "title": as_string(frontmatter.get("title")),
        "event_date": event_date,
        "day": as_string(frontmatter.get("day")),
        "start_time": as_string(frontmatter.get("start_time")),
        "end_time": as_string(frontmatter.get("end_time")),
        "start_at": start_at,
        "end_at": end_at,
        "host": as_string(frontmatter.get("host")),
        "neighborhood": as_string(frontmatter.get("neighborhood")),
        "venue_name": as_string(frontmatter.get("venue_name")),
        "venue_address": as_string(frontmatter.get("venue_address")),
        "rsvp_url": as_string(frontmatter.get("rsvp_url")),
        "public_short_url": as_string(frontmatter.get("public_short_url")),
        "google_maps": as_string(frontmatter.get("google_maps")),
        "image": as_string(frontmatter.get("image")),
        "local_image": as_string(frontmatter.get("local_image")),
        "visibility": as_string(frontmatter.get("visibility")),
        "guest_action": as_string(frontmatter.get("guest_action")),
        "fetch_status": as_string(frontmatter.get("fetch_status")),
        "at_capacity": as_bool(frontmatter.get("at_capacity")),
        "is_capped": as_bool(frontmatter.get("is_capped")),
        "canceled": as_bool(frontmatter.get("canceled")),
        "owner_count": as_int(frontmatter.get("owner_count")),
        "going_guest_count": as_int(frontmatter.get("going_guest_count")),
        "total_guest_count": as_int(frontmatter.get("total_guest_count")),
        "approved_guest_count": as_int(frontmatter.get("approved_guest_count")),
        "max_capacity": as_int(frontmatter.get("max_capacity")),
        "remaining_capacity": as_int(frontmatter.get("remaining_capacity")),
        "badges": badges,
        "owner_ids": owner_ids,
        "calendar_datetime": as_string(frontmatter.get("calendar_datetime")),
        "image_download_error": as_string(frontmatter.get("image_download_error")),
        "canceled_at": canceled_at,
        "canceled_by": as_string(frontmatter.get("canceled_by")),
        "cancellation_message": as_string(frontmatter.get("cancellation_message")),
        "description": extract_description(markdown_body),
        "markdown_body": markdown_body,
        "frontmatter_json": json.dumps(frontmatter, sort_keys=True, default=str),
        "raw_markdown": raw,
    }


EVENT_COLUMNS = [
    "event_id",
    "source_path",
    "title",
    "event_date",
    "day",
    "start_time",
    "end_time",
    "start_at",
    "end_at",
    "host",
    "neighborhood",
    "venue_name",
    "venue_address",
    "rsvp_url",
    "public_short_url",
    "google_maps",
    "image",
    "local_image",
    "visibility",
    "guest_action",
    "fetch_status",
    "at_capacity",
    "is_capped",
    "canceled",
    "owner_count",
    "going_guest_count",
    "total_guest_count",
    "approved_guest_count",
    "max_capacity",
    "remaining_capacity",
    "badges",
    "owner_ids",
    "calendar_datetime",
    "image_download_error",
    "canceled_at",
    "canceled_by",
    "cancellation_message",
    "description",
    "markdown_body",
    "frontmatter_json",
    "raw_markdown",
]

HOST_COLUMNS = [
    "user_id",
    "name",
    "bio",
    "bio_visibility",
    "photo",
    "is_managed",
    "on_partiful",
    "socials_json",
    "tags",
    "raw_json",
]

EVENT_HOST_COLUMNS = [
    "event_id",
    "user_id",
    "host_position",
    "is_platform_admin",
]

MANIFEST_COLUMNS = [
    "event_id",
    "url",
    "title",
    "host",
    "date_time",
    "neighborhood",
    "badges",
    "source",
    "raw_json",
]


def event_rows(dataset: NytwDataset) -> Iterator[tuple[Any, ...]]:
    for path in sorted(dataset.events_dir.glob("*.md")):
        event = parse_event_file(path, dataset.source_dir)
        yield tuple(event[column] for column in EVENT_COLUMNS)


def host_rows(dataset: NytwDataset) -> Iterator[tuple[Any, ...]]:
    users = json.loads(dataset.users_path.read_text(encoding="utf-8"))
    for user_id, user in sorted(users.items()):
        yield (
            user_id,
            as_string(user.get("name")),
            as_string(user.get("bio")),
            as_string(user.get("bio_visibility")),
            as_string(user.get("photo")),
            as_bool(user.get("is_managed")),
            as_bool(user.get("on_partiful")),
            json.dumps(user.get("socials") or {}, sort_keys=True),
            as_string_list(user.get("tags")),
            json.dumps(user, sort_keys=True, default=str),
        )


def event_host_rows(dataset: NytwDataset) -> Iterator[tuple[Any, ...]]:
    for path in sorted(dataset.events_dir.glob("*.md")):
        event = parse_event_file(path, dataset.source_dir)
        for index, user_id in enumerate(event["owner_ids"], start=1):
            yield (
                event["event_id"],
                user_id,
                index,
                user_id == PLATFORM_ADMIN_ID,
            )


def manifest_rows(dataset: NytwDataset) -> Iterator[tuple[Any, ...]]:
    manifest = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("events", []):
        url = as_string(item.get("url"))
        event_id = url.rstrip("/").split("/")[-1] if url else ""
        yield (
            event_id,
            url,
            as_string(item.get("title")),
            as_string(item.get("host")),
            as_string(item.get("dateTime")),
            as_string(item.get("neighborhood")),
            as_string_list(item.get("badges")),
            as_string(item.get("source")),
            json.dumps(item, sort_keys=True, default=str),
        )


def batched(rows: Iterator[tuple[Any, ...]], size: int) -> Iterator[list[tuple[Any, ...]]]:
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def create_nytw_tables(service: ClickHouseService) -> None:
    service.command(
        """
        CREATE TABLE IF NOT EXISTS nytw_events
        (
            event_id String,
            source_path String,
            title String,
            event_date Nullable(Date),
            day LowCardinality(String),
            start_time String,
            end_time String,
            start_at Nullable(DateTime64(3, 'UTC')),
            end_at Nullable(DateTime64(3, 'UTC')),
            host String,
            neighborhood LowCardinality(String),
            venue_name String,
            venue_address String,
            rsvp_url String,
            public_short_url String,
            google_maps String,
            image String,
            local_image String,
            visibility LowCardinality(String),
            guest_action LowCardinality(String),
            fetch_status LowCardinality(String),
            at_capacity Bool,
            is_capped Bool,
            canceled Bool,
            owner_count Nullable(UInt16),
            going_guest_count Nullable(UInt32),
            total_guest_count Nullable(UInt32),
            approved_guest_count Nullable(UInt32),
            max_capacity Nullable(UInt32),
            remaining_capacity Nullable(Int32),
            badges Array(String),
            owner_ids Array(String),
            calendar_datetime String,
            image_download_error String,
            canceled_at Nullable(DateTime64(3, 'UTC')),
            canceled_by String,
            cancellation_message String,
            description String,
            markdown_body String,
            frontmatter_json String,
            raw_markdown String
        )
        ENGINE = MergeTree
        ORDER BY event_id
        """
    )
    service.command(
        """
        CREATE TABLE IF NOT EXISTS nytw_hosts
        (
            user_id String,
            name String,
            bio String,
            bio_visibility String,
            photo String,
            is_managed Bool,
            on_partiful Bool,
            socials_json String,
            tags Array(String),
            raw_json String
        )
        ENGINE = MergeTree
        ORDER BY user_id
        """
    )
    service.command(
        """
        CREATE TABLE IF NOT EXISTS nytw_event_hosts
        (
            event_id String,
            user_id String,
            host_position UInt16,
            is_platform_admin Bool
        )
        ENGINE = MergeTree
        ORDER BY (event_id, host_position, user_id)
        """
    )
    service.command(
        """
        CREATE TABLE IF NOT EXISTS nytw_manifest
        (
            event_id String,
            url String,
            title String,
            host String,
            date_time String,
            neighborhood LowCardinality(String),
            badges Array(String),
            source LowCardinality(String),
            raw_json String
        )
        ENGINE = MergeTree
        ORDER BY event_id
        """
    )


def truncate_nytw_tables(service: ClickHouseService) -> None:
    for table in ("nytw_events", "nytw_hosts", "nytw_event_hosts", "nytw_manifest"):
        service.command(f"TRUNCATE TABLE IF EXISTS {table}")


def drop_nytw_tables(service: ClickHouseService) -> None:
    for table in ("nytw_events", "nytw_hosts", "nytw_event_hosts", "nytw_manifest"):
        service.command(f"DROP TABLE IF EXISTS {table}")


def insert_all(
    service: ClickHouseService,
    table: str,
    rows: Iterator[tuple[Any, ...]],
    columns: Sequence[str],
    batch_size: int,
) -> int:
    count = 0
    for batch in batched(rows, batch_size):
        service.insert_rows(table, batch, columns)
        count += len(batch)
    return count


def load_nytw_dataset(
    service: ClickHouseService,
    dataset: NytwDataset,
    *,
    replace: bool = False,
    batch_size: int = 500,
) -> dict[str, int]:
    dataset.validate()
    if replace:
        drop_nytw_tables(service)
    create_nytw_tables(service)

    return {
        "events": insert_all(service, "nytw_events", event_rows(dataset), EVENT_COLUMNS, batch_size),
        "hosts": insert_all(service, "nytw_hosts", host_rows(dataset), HOST_COLUMNS, batch_size),
        "event_hosts": insert_all(
            service,
            "nytw_event_hosts",
            event_host_rows(dataset),
            EVENT_HOST_COLUMNS,
            batch_size,
        ),
        "manifest": insert_all(
            service,
            "nytw_manifest",
            manifest_rows(dataset),
            MANIFEST_COLUMNS,
            batch_size,
        ),
    }


def inspect_nytw_dataset(dataset: NytwDataset) -> dict[str, int]:
    dataset.validate()
    return {
        "event_files": sum(1 for _ in dataset.events_dir.glob("*.md")),
        "events": sum(1 for _ in event_rows(dataset)),
        "hosts": sum(1 for _ in host_rows(dataset)),
        "event_hosts": sum(1 for _ in event_host_rows(dataset)),
        "manifest": sum(1 for _ in manifest_rows(dataset)),
    }
