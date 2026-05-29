from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .city import CITIES, CityConfig, load_city
from .client import ClickHouseService
from .nytw import (
    EVENT_COLUMNS,
    MANIFEST_COLUMNS,
    as_string,
    as_string_list,
    batched,
    create_nytw_tables,
    insert_all,
    parse_event_markdown,
)


logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
DEFAULT_CITIES = ("nyc", "boston")
CALENDAR_EVENTS_API_URL = "https://www.tech-week.com/calendar/api/trpc/calendar.events"
CALENDAR_SYNC_RUN_COLUMNS = [
    "run_id",
    "city_slug",
    "table_prefix",
    "started_at",
    "finished_at",
    "status",
    "calendar_events",
    "event_rows",
    "manifest_rows",
    "inserted",
    "updated",
    "unchanged",
    "removed",
    "error",
]
CALENDAR_SYNC_CHANGE_COLUMNS = [
    "run_id",
    "city_slug",
    "table_name",
    "change_type",
    "record_id",
    "title",
    "changed_fields",
    "previous_hash",
    "content_hash",
    "previous_json",
    "content_json",
    "synced_at",
]
CALENDAR_EVENT_SNAPSHOT_COLUMNS = ["run_id", "synced_at", *EVENT_COLUMNS]
CALENDAR_MANIFEST_SNAPSHOT_COLUMNS = ["run_id", "synced_at", *MANIFEST_COLUMNS]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.+?)</script>', re.S)
SCROLL_STEP = 0.6
SCROLL_DELAY = 0.35
STABLE_TICKS_TO_FINISH = 12
MAX_TICKS_PER_PASS = 260
EXTRACT_JS = r"""
() => {
  const rows = document.querySelectorAll('tbody tr');
  const out = [];
  for (const tr of rows) {
    const key = Object.keys(tr).find(k => k.startsWith('__reactFiber'));
    if (!key) continue;
    let f = tr[key];
    let depth = 0;
    while (f && depth < 6) {
      const p = f.memoizedProps || {};
      const orig = p.row && p.row.original;
      if (orig && orig.id != null) {
        const hosts = (orig.facets && orig.facets.hosts)
          ? orig.facets.hosts.map(h => h.label)
          : [];
        out.push({
          id: orig.id,
          name: orig.name,
          date: orig.date,
          time: orig.time,
          location: orig.location,
          company: orig.company,
          externalHref: orig.externalHref,
          isInviteOnly: !!orig.isInviteOnly,
          hosts,
        });
        break;
      }
      f = f.return;
      depth++;
    }
  }
  return out;
}
"""
COUNTER_JS = r"""
() => {
  const m = (document.body.innerText || '').match(/(\d+)\s+matching events/);
  return m ? parseInt(m[1], 10) : null;
}
"""


@dataclass(frozen=True)
class CalendarSyncConfig:
    city_slugs: tuple[str, ...] = DEFAULT_CITIES
    interval_seconds: int = 21600
    include_invite_only: bool = False
    fetch_concurrency: int = 3
    batch_size: int = 500
    fetch_timeout_seconds: float = 25.0
    max_scroll_ticks: int = MAX_TICKS_PER_PASS
    scroll_delay_seconds: float = SCROLL_DELAY
    page_delay_seconds: float = 0.1

    @classmethod
    def from_env(cls) -> "CalendarSyncConfig":
        import os

        city_slugs = tuple(
            slug.strip().lower()
            for slug in os.getenv("TECHWEEK_CALENDAR_SYNC_CITIES", "nyc,boston").split(",")
            if slug.strip()
        )
        return cls(
            city_slugs=city_slugs or DEFAULT_CITIES,
            interval_seconds=int(os.getenv("TECHWEEK_CALENDAR_SYNC_INTERVAL_SECONDS", "21600")),
            include_invite_only=_env_bool("TECHWEEK_CALENDAR_SYNC_INCLUDE_INVITE_ONLY", False),
            fetch_concurrency=max(1, int(os.getenv("TECHWEEK_EVENT_FETCH_CONCURRENCY", "3"))),
            batch_size=max(1, int(os.getenv("TECHWEEK_CALENDAR_SYNC_BATCH_SIZE", "500"))),
            fetch_timeout_seconds=float(os.getenv("TECHWEEK_EVENT_FETCH_TIMEOUT_SECONDS", "25")),
            max_scroll_ticks=max(1, int(os.getenv("TECHWEEK_CALENDAR_MAX_SCROLL_TICKS", str(MAX_TICKS_PER_PASS)))),
            scroll_delay_seconds=max(0.05, float(os.getenv("TECHWEEK_CALENDAR_SCROLL_DELAY_SECONDS", str(SCROLL_DELAY)))),
            page_delay_seconds=max(0.0, float(os.getenv("TECHWEEK_CALENDAR_PAGE_DELAY_SECONDS", "0.1"))),
        )


def _env_bool(name: str, default: bool) -> bool:
    import os

    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def create_calendar_sync_tables(service: ClickHouseService, prefix: str) -> None:
    create_nytw_tables(service, prefix=prefix)
    service.command(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}_calendar_events
        (
            run_id String,
            synced_at DateTime64(3, 'UTC'),
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
        ORDER BY (run_id, event_id)
        """
    )
    service.command(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}_calendar_manifest
        (
            run_id String,
            synced_at DateTime64(3, 'UTC'),
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
        ORDER BY (run_id, event_id)
        """
    )
    service.command(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}_sync_runs
        (
            run_id String,
            city_slug LowCardinality(String),
            table_prefix LowCardinality(String),
            started_at DateTime64(3, 'UTC'),
            finished_at DateTime64(3, 'UTC'),
            status LowCardinality(String),
            calendar_events UInt32,
            event_rows UInt32,
            manifest_rows UInt32,
            inserted UInt32,
            updated UInt32,
            unchanged UInt32,
            removed UInt32,
            error String
        )
        ENGINE = MergeTree
        ORDER BY (started_at, run_id)
        """
    )
    service.command(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}_sync_changes
        (
            run_id String,
            city_slug LowCardinality(String),
            table_name LowCardinality(String),
            change_type LowCardinality(String),
            record_id String,
            title String,
            changed_fields Array(String),
            previous_hash String,
            content_hash String,
            previous_json String,
            content_json String,
            synced_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree
        ORDER BY (synced_at, run_id, table_name, record_id)
        """
    )
    service.command(
        f"""
        CREATE VIEW IF NOT EXISTS {prefix}_current_events AS
        SELECT {', '.join(EVENT_COLUMNS)}
        FROM {prefix}_calendar_events
        WHERE run_id = (
            SELECT run_id
            FROM {prefix}_sync_runs
            WHERE status = 'complete'
            ORDER BY finished_at DESC
            LIMIT 1
        )
        """
    )
    service.command(
        f"""
        CREATE VIEW IF NOT EXISTS {prefix}_current_manifest AS
        SELECT {', '.join(MANIFEST_COLUMNS)}
        FROM {prefix}_calendar_manifest
        WHERE run_id = (
            SELECT run_id
            FROM {prefix}_sync_runs
            WHERE status = 'complete'
            ORDER BY finished_at DESC
            LIMIT 1
        )
        """
    )


def crawl_calendar(
    city: CityConfig,
    *,
    include_invite_only: bool = False,
    page_delay_seconds: float = 0.1,
    max_scroll_ticks: int = MAX_TICKS_PER_PASS,
    scroll_delay_seconds: float = SCROLL_DELAY,
) -> list[dict[str, Any]]:
    return crawl_calendar_api(
        city,
        include_invite_only=include_invite_only,
        page_delay_seconds=page_delay_seconds,
    )


def crawl_calendar_api(
    city: CityConfig,
    *,
    include_invite_only: bool = False,
    page_delay_seconds: float = 0.1,
) -> list[dict[str, Any]]:
    logger.info("Fetching %s calendar via public Tech Week API", city.slug)
    page = 1
    total = None
    rows: list[dict[str, Any]] = []
    while True:
        data = _fetch_calendar_api_page(city.slug, page)
        rows.extend(data.get("results") or [])
        total = int(data.get("total") or total or 0)
        per_page = int(data.get("perPage") or len(data.get("results") or []) or 48)
        current_page = int(data.get("page") or page)
        if current_page * per_page >= total:
            break
        page += 1
        if page > 200:
            raise RuntimeError(f"Calendar API pagination exceeded 200 pages for {city.slug}")
        if page_delay_seconds > 0:
            time.sleep(page_delay_seconds)

    if not include_invite_only:
        rows = [row for row in rows if not row.get("isInviteOnly")]
    return sorted(rows, key=lambda row: (str(row.get("date") or ""), str(row.get("time") or ""), str(row.get("id") or "")))


def _fetch_calendar_api_page(city_slug: str, page: int) -> dict[str, Any]:
    input_obj = {
        "city": city_slug,
        "q": "",
        "featured": False,
        "day": "all",
        "track": [],
        "sponsor": [],
        "theme": [],
        "format": [],
        "location": [],
        "time": [],
        "host": [],
        "sortBy": "time",
        "sortOrder": "asc",
        "cursor": page,
    }
    url = CALENDAR_EVENTS_API_URL + "?" + urllib.parse.urlencode(
        {"input": json.dumps(input_obj, separators=(",", ":"))}
    )
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("result", {}).get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected calendar API response for {city_slug} page {page}")
    return data


def crawl_calendar_with_browser(
    city: CityConfig,
    *,
    include_invite_only: bool = False,
    max_scroll_ticks: int = MAX_TICKS_PER_PASS,
    scroll_delay_seconds: float = SCROLL_DELAY,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is required for calendar sync. Run `pip install -e .` "
            "and `python3 -m playwright install chromium`."
        ) from exc

    logger.info("Crawling %s calendar: %s", city.slug, city.calendar_url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1600})
        page = ctx.new_page()
        page.goto(city.calendar_url, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_selector("tbody tr", timeout=20000)
        except PWTimeout as exc:
            raise RuntimeError(f"Timed out waiting for event rows at {city.calendar_url}") from exc

        expected = page.evaluate(COUNTER_JS)
        all_events: dict[str, dict[str, Any]] = {}
        _scroll_pass(page, all_events, max_ticks=max_scroll_ticks, delay=scroll_delay_seconds)
        _scroll_pass(page, all_events, max_ticks=max_scroll_ticks, delay=scroll_delay_seconds)
        if expected and len(all_events) < int(expected * 0.99):
            _scroll_pass(page, all_events, max_ticks=max_scroll_ticks, delay=scroll_delay_seconds)
        browser.close()

    rows = list(all_events.values())
    if not include_invite_only:
        rows = [row for row in rows if not row.get("isInviteOnly")]
    return sorted(rows, key=lambda row: (str(row.get("date") or ""), str(row.get("time") or ""), str(row.get("id") or "")))


def _scroll_pass(
    page: Any,
    all_events: dict[str, dict[str, Any]],
    *,
    max_ticks: int,
    delay: float,
) -> None:
    page.evaluate("() => window.scrollTo(0, 0)")
    time.sleep(delay)
    stable = 0
    last_size = -1
    for _tick in range(max_ticks):
        for event in page.evaluate(EXTRACT_JS):
            event_id = str(event.get("id") or "")
            if event_id:
                all_events.setdefault(event_id, event)

        page.evaluate("(step) => window.scrollBy(0, window.innerHeight * step)", SCROLL_STEP)
        time.sleep(delay)
        at_bottom = page.evaluate(
            "() => window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 5"
        )
        if len(all_events) == last_size:
            stable += 1
        else:
            stable = 0
        last_size = len(all_events)
        if at_bottom and stable >= STABLE_TICKS_TO_FINISH:
            break


def sync_city_calendar(
    service: ClickHouseService,
    city: CityConfig,
    *,
    include_invite_only: bool = False,
    fetch_concurrency: int = 3,
    batch_size: int = 500,
    fetch_timeout_seconds: float = 25.0,
    max_scroll_ticks: int = MAX_TICKS_PER_PASS,
    scroll_delay_seconds: float = SCROLL_DELAY,
    page_delay_seconds: float = 0.1,
    calendar_rows: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc)
    prefix = city.table_prefix
    status = "complete"
    error = ""
    event_count = 0
    manifest_count = 0
    changes: list[tuple[Any, ...]] = []

    create_calendar_sync_tables(service, prefix)
    try:
        crawled = list(calendar_rows) if calendar_rows is not None else crawl_calendar(
            city,
            include_invite_only=include_invite_only,
            max_scroll_ticks=max_scroll_ticks,
            scroll_delay_seconds=scroll_delay_seconds,
            page_delay_seconds=page_delay_seconds,
        )
        manifests = [_manifest_from_calendar_row(row) for row in crawled]
        manifest_count = len(manifests)

        old_events = _latest_table_rows(service, f"{prefix}_current_events", EVENT_COLUMNS, "event_id")
        if not old_events:
            old_events = _latest_table_rows(service, f"{prefix}_events", EVENT_COLUMNS, "event_id")
        old_manifests = _latest_table_rows(service, f"{prefix}_current_manifest", MANIFEST_COLUMNS, "event_id")
        if not old_manifests:
            old_manifests = _latest_table_rows(service, f"{prefix}_manifest", MANIFEST_COLUMNS, "event_id")
        new_manifests = {
            _manifest_event_id(item): _manifest_projection(item) for item in manifests
        }

        synced_at = datetime.now(timezone.utc)
        manifest_changes = _build_changes(
            run_id,
            city.slug,
            f"{prefix}_manifest",
            old_manifests,
            new_manifests,
            synced_at,
        )
        event_ids_to_fetch = _changed_record_ids(old_manifests, new_manifests)
        event_ids_to_fetch.update(event_id for event_id in new_manifests if event_id not in old_events)
        manifests_to_fetch = [
            manifest for manifest in manifests if _manifest_event_id(manifest) in event_ids_to_fetch
        ]
        fetched_events = (
            _fetch_event_dicts(
                manifests_to_fetch,
                fetch_concurrency=fetch_concurrency,
                timeout=fetch_timeout_seconds,
            )
            if manifests_to_fetch
            else []
        )
        fetched_events_by_id = {event["event_id"]: _event_projection(event) for event in fetched_events}
        new_events = {}
        for manifest in manifests:
            event_id = _manifest_event_id(manifest)
            if event_id in fetched_events_by_id:
                new_events[event_id] = fetched_events_by_id[event_id]
            elif event_id in old_events:
                new_events[event_id] = old_events[event_id]
        event_count = len(new_events)

        changes.extend(
            _build_changes(
                run_id,
                city.slug,
                f"{prefix}_events",
                old_events,
                new_events,
                synced_at,
            )
        )
        changes.extend(manifest_changes)

        insert_all(
            service,
            f"{prefix}_calendar_events",
            (
                (run_id, synced_at, *(new_events[event_id][column] for column in EVENT_COLUMNS))
                for event_id in new_events
            ),
            CALENDAR_EVENT_SNAPSHOT_COLUMNS,
            batch_size,
        )
        insert_all(
            service,
            f"{prefix}_calendar_manifest",
            ((run_id, synced_at, *row) for row in _manifest_tuples(manifests)),
            CALENDAR_MANIFEST_SNAPSHOT_COLUMNS,
            batch_size,
        )
        _insert(service, f"{prefix}_sync_changes", changes, CALENDAR_SYNC_CHANGE_COLUMNS)
    except Exception as exc:
        status = "failed"
        error = str(exc)
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        summary = summarize_calendar_changes(changes)
        service.insert_rows(
            f"{prefix}_sync_runs",
            [
                (
                    run_id,
                    city.slug,
                    prefix,
                    started_at,
                    finished_at,
                    status,
                    len(calendar_rows) if calendar_rows is not None else manifest_count,
                    event_count,
                    manifest_count,
                    summary["inserted"],
                    summary["updated"],
                    summary["unchanged"],
                    summary["removed"],
                    error,
                )
            ],
            CALENDAR_SYNC_RUN_COLUMNS,
        )

    return {
        "run_id": run_id,
        "city": city.slug,
        "status": status,
        "events": event_count,
        "manifest": manifest_count,
        "fetched_events": len(event_ids_to_fetch) if status == "complete" else 0,
        "changes": summarize_calendar_changes(changes),
    }


def sync_configured_calendars(
    service: ClickHouseService,
    config: CalendarSyncConfig,
) -> dict[str, Any]:
    results = []
    for slug in config.city_slugs:
        results.append(
            sync_city_calendar(
                service,
                load_city(slug),
                include_invite_only=config.include_invite_only,
                fetch_concurrency=config.fetch_concurrency,
                batch_size=config.batch_size,
                fetch_timeout_seconds=config.fetch_timeout_seconds,
                max_scroll_ticks=config.max_scroll_ticks,
                scroll_delay_seconds=config.scroll_delay_seconds,
                page_delay_seconds=config.page_delay_seconds,
            )
        )
    return {"cities": results}


def calendar_sync_overview(
    service: ClickHouseService,
    *,
    city_slugs: Sequence[str] = DEFAULT_CITIES,
    limit: int = 1,
    item_limit: int = 25,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    changed_items: list[dict[str, Any]] = []
    for slug in city_slugs:
        city = CITIES.get(slug)
        if city is None:
            continue
        prefix = city.table_prefix
        city_runs = _safe_query(
            service,
            f"""
            SELECT *
            FROM {prefix}_sync_runs
            ORDER BY started_at DESC
            LIMIT {max(1, min(limit, 50))}
            """,
        )
        runs.extend(city_runs)
        if item_limit:
            changed_items.extend(
                _safe_query(
                    service,
                    f"""
                    SELECT
                      run_id,
                      city_slug,
                      table_name,
                      change_type,
                      record_id,
                      title,
                      changed_fields,
                      synced_at
                    FROM {prefix}_sync_changes
                    WHERE change_type IN ('inserted', 'updated', 'removed')
                    ORDER BY synced_at DESC, title ASC
                    LIMIT {max(0, min(item_limit, 200))}
                    """,
                )
            )
    return {"runs": runs, "changed_items": changed_items[:item_limit]}


def summarize_calendar_changes(rows: Sequence[Sequence[Any]]) -> dict[str, int]:
    summary = {"inserted": 0, "updated": 0, "unchanged": 0, "removed": 0}
    for row in rows:
        change_type = str(row[3]) if len(row) > 3 else ""
        if change_type in summary:
            summary[change_type] += 1
    return summary


def _fetch_event_dicts(
    manifests: Sequence[dict[str, Any]],
    *,
    fetch_concurrency: int,
    timeout: float,
) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, fetch_concurrency)) as pool:
        futures = {
            pool.submit(_fetch_event_dict, manifest, timeout): manifest
            for manifest in manifests
        }
        for future in cf.as_completed(futures):
            event = future.result()
            results[event["event_id"]] = event
    return [results[_manifest_event_id(manifest)] for manifest in manifests if _manifest_event_id(manifest) in results]


def _fetch_event_dict(manifest: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        html = _fetch(manifest["url"], timeout=timeout)
        partiful = _parse_partiful(html)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        partiful = None
    filename, raw_markdown = render_event_markdown(manifest, partiful)
    return parse_event_markdown(raw_markdown, source_path=f"live/{filename}")


def _fetch(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def _parse_partiful(html: str) -> dict[str, Any] | None:
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    event = data.get("props", {}).get("pageProps", {}).get("event")
    return event if isinstance(event, dict) else None


def render_event_markdown(manifest_row: dict[str, Any], partiful: dict[str, Any] | None) -> tuple[str, str]:
    url = manifest_row["url"]
    event_id = url.rstrip("/").split("/")[-1]
    calendar_title = as_string(manifest_row.get("title"))
    calendar_host = as_string(manifest_row.get("host"))
    calendar_neighborhood = as_string(manifest_row.get("neighborhood"))
    calendar_datetime = as_string(manifest_row.get("dateTime"))
    badges = as_string_list(manifest_row.get("badges"))

    if partiful is None:
        slug = _slugify(calendar_title or event_id)
        filename = f"unknown-date-{slug}-{event_id}.md"
        body = [
            "---",
            f"title: {_yaml_escape(calendar_title)}",
            f"event_id: {_yaml_escape(event_id)}",
            f"rsvp_url: {_yaml_escape(url)}",
            f"host: {_yaml_escape(calendar_host)}",
            f"neighborhood: {_yaml_escape(calendar_neighborhood)}",
            f"calendar_datetime: {_yaml_escape(calendar_datetime)}",
            f"badges: {json.dumps(badges)}",
            "fetch_status: failed",
            "---",
            "",
            f"# {calendar_title}",
            "",
            f"**Host:** {calendar_host}",
            f"**When:** {calendar_datetime}",
            f"**Where:** {calendar_neighborhood}",
            f"**RSVP:** {url}",
            "",
            "## Description",
            "",
            "_Could not fetch Partiful page at sync time. Click the RSVP link for full details._",
            "",
            f"[RSVP / Apply on Partiful]({url})",
            "",
        ]
        return filename, "\n".join(body)

    partiful_title = as_string(partiful.get("title") or calendar_title).strip()
    display_title = re.sub(r"\s*-?\s*#(?:NY|BOS)?TechWeek\s*$", "", partiful_title).strip()
    start_iso = as_string(partiful.get("startDate"))
    end_iso = as_string(partiful.get("endDate"))
    start_dt = _parse_iso_et(start_iso)
    end_dt = _parse_iso_et(end_iso)
    date_str = start_dt.strftime("%Y-%m-%d") if start_dt else "unknown-date"
    day_str = start_dt.strftime("%A") if start_dt else ""
    start_time = _fmt_time(start_dt) if start_dt else ""
    end_time = _fmt_time(end_dt) if end_dt else ""
    file_time = start_dt.strftime("%H%M") if start_dt else "0000"
    when = (
        f"{day_str}, {start_dt.strftime('%B')} {start_dt.day}, {start_dt.year} "
        f"- {start_time}{('-' + end_time) if end_time else ''} ET"
        if start_dt
        else calendar_datetime
    )

    location = partiful.get("locationInfo") if isinstance(partiful.get("locationInfo"), dict) else {}
    maps = location.get("mapsInfo") if isinstance(location.get("mapsInfo"), dict) else {}
    venue_name = as_string(maps.get("name"))
    address_lines = maps.get("addressLines") or location.get("displayAddressLines") or []
    venue_address = ", ".join(as_string(line) for line in address_lines if line)
    google_maps = as_string(maps.get("googleMapsUrl"))
    image = partiful.get("image") if isinstance(partiful.get("image"), dict) else {}
    image_url = as_string(image.get("url"))
    public_short = as_string(partiful.get("publicShortUrl"))
    visibility = as_string(partiful.get("visibility"))
    guest_action = as_string(partiful.get("guestAction"))
    at_capacity = bool(partiful.get("atCapacity"))
    guest_count = partiful.get("goingGuestCount") or partiful.get("guestCount") or 0
    description = as_string(partiful.get("description")).strip()
    filename = f"{date_str}-{file_time}-{_slugify(display_title)}.md"

    frontmatter = [
        "---",
        f"title: {_yaml_escape(display_title)}",
        f"event_id: {_yaml_escape(event_id)}",
        f"date: {date_str}",
    ]
    if day_str:
        frontmatter.append(f"day: {_yaml_escape(day_str)}")
    if start_time:
        frontmatter.append(f"start_time: {_yaml_escape(start_time + ' ET')}")
    if end_time:
        frontmatter.append(f"end_time: {_yaml_escape(end_time + ' ET')}")
    if start_iso:
        frontmatter.append(f"start_iso: {_yaml_escape(start_iso)}")
    if end_iso:
        frontmatter.append(f"end_iso: {_yaml_escape(end_iso)}")
    frontmatter.extend(
        [
            f"host: {_yaml_escape(calendar_host)}",
            *( [f"venue_name: {_yaml_escape(venue_name)}"] if venue_name else [] ),
            *( [f"venue_address: {_yaml_escape(venue_address)}"] if venue_address else [] ),
            f"neighborhood: {_yaml_escape(calendar_neighborhood)}",
            f"rsvp_url: {_yaml_escape(url)}",
            *( [f"public_short_url: {_yaml_escape(public_short)}"] if public_short else [] ),
            *( [f"google_maps: {_yaml_escape(google_maps)}"] if google_maps else [] ),
            *( [f"image: {_yaml_escape(image_url)}"] if image_url else [] ),
            *( [f"visibility: {_yaml_escape(visibility)}"] if visibility else [] ),
            *( [f"guest_action: {_yaml_escape(guest_action)}"] if guest_action else [] ),
            f"at_capacity: {'true' if at_capacity else 'false'}",
            *( [f"going_guest_count: {int(guest_count)}"] if isinstance(guest_count, int) and guest_count else [] ),
            f"badges: {json.dumps(badges)}",
            "fetch_status: ok",
            "---",
        ]
    )
    where = " - ".join(part for part in [venue_name, venue_address, calendar_neighborhood] if part)
    action_label = "Apply" if guest_action == "APPLY" else "RSVP"
    body = [
        "",
        f"# {display_title}",
        "",
        f"**Host:** {calendar_host}" if calendar_host else "",
        f"**When:** {when}" if when else "",
        f"**Where:** {where}" if where else "",
        f"**RSVP:** {url}",
        f"**Map:** {google_maps}" if google_maps else "",
        "",
        "## Description",
        "",
        description or "_(No description provided.)_",
        "",
        "---",
        "",
        f"[{action_label} on Partiful]({url})",
        "",
    ]
    return filename, "\n".join(frontmatter + [line for line in body if line is not None])


def _manifest_from_calendar_row(row: dict[str, Any]) -> dict[str, Any]:
    hosts = row.get("hosts") if isinstance(row.get("hosts"), list) else []
    return {
        "badges": [],
        "dateTime": _calendar_datetime(str(row.get("date") or ""), str(row.get("time") or "")),
        "host": as_string(row.get("company")) or (as_string(hosts[0]) if hosts else ""),
        "neighborhood": as_string(row.get("location")),
        "source": "live-calendar-sync",
        "title": as_string(row.get("name")),
        "url": as_string(row.get("externalHref")),
    }


def _manifest_tuples(manifests: Sequence[dict[str, Any]]):
    for item in manifests:
        event_id = _manifest_event_id(item)
        yield (
            event_id,
            as_string(item.get("url")),
            as_string(item.get("title")),
            as_string(item.get("host")),
            as_string(item.get("dateTime")),
            as_string(item.get("neighborhood")),
            as_string_list(item.get("badges")),
            as_string(item.get("source")),
            json.dumps(item, sort_keys=True, default=str),
        )


def _manifest_event_id(item: dict[str, Any]) -> str:
    url = as_string(item.get("url"))
    return url.rstrip("/").split("/")[-1] if url else ""


def _event_projection(event: dict[str, Any]) -> dict[str, Any]:
    return {column: event.get(column) for column in EVENT_COLUMNS}


def _manifest_projection(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(zip(MANIFEST_COLUMNS, next(_manifest_tuples([item])), strict=False))
    return row


def _latest_table_rows(
    service: ClickHouseService,
    table: str,
    columns: Sequence[str],
    key_column: str,
) -> dict[str, dict[str, Any]]:
    rows = _safe_query(service, f"SELECT {', '.join(columns)} FROM {table}")
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get(key_column) or "")
        if key:
            latest[key] = {column: row.get(column) for column in columns}
    return latest


def _build_changes(
    run_id: str,
    city_slug: str,
    table_name: str,
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    synced_at: datetime,
) -> list[tuple[Any, ...]]:
    changes = []
    all_ids = sorted(set(previous) | set(current))
    for record_id in all_ids:
        old = previous.get(record_id)
        new = current.get(record_id)
        old_json = _canonical_json(old) if old is not None else ""
        new_json = _canonical_json(new) if new is not None else ""
        old_hash = _sha256(old_json) if old_json else ""
        new_hash = _sha256(new_json) if new_json else ""
        if old is None:
            change_type = "inserted"
        elif new is None:
            change_type = "removed"
        elif old_hash != new_hash:
            change_type = "updated"
        else:
            change_type = "unchanged"
        title = as_string((new or old or {}).get("title"))
        changes.append(
            (
                run_id,
                city_slug,
                table_name,
                change_type,
                record_id,
                title,
                _changed_fields(old, new),
                old_hash,
                new_hash,
                old_json,
                new_json,
                synced_at,
            )
        )
    return changes


def _changed_record_ids(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> set[str]:
    changed = set()
    for record_id, new in current.items():
        old = previous.get(record_id)
        if old is None:
            changed.add(record_id)
            continue
        if _sha256(_canonical_json(old)) != _sha256(_canonical_json(new)):
            changed.add(record_id)
    return changed


def _changed_fields(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[str]:
    if old is None:
        return sorted(new.keys()) if new else []
    if new is None:
        return sorted(old.keys())
    return sorted(key for key in set(old) | set(new) if _normalize(old.get(key)) != _normalize(new.get(key)))


def _canonical_json(value: Any) -> str:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert(
    service: ClickHouseService,
    table: str,
    rows: Sequence[Sequence[Any]],
    columns: Sequence[str],
) -> None:
    for batch in batched(iter(rows), 500):
        service.insert_rows(table, batch, columns)


def _safe_query(service: ClickHouseService, sql: str) -> list[dict[str, Any]]:
    try:
        return service.query(sql)
    except Exception:
        return []


def _parse_iso_et(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ET)
    except ValueError:
        return None


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return ""
    hour = value.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{value.strftime('%M')}{value.strftime('%p').lower()}"


def _calendar_datetime(date_iso: str, time_iso: str) -> str:
    try:
        parsed = datetime.strptime(date_iso, "%Y-%m-%d")
        hour, minute = time_iso.split(":")[:2]
        hour_int = int(hour)
    except Exception:
        return ""
    ampm = "am" if hour_int < 12 else "pm"
    hour_12 = hour_int % 12 or 12
    minute_part = f":{minute}" if minute != "00" else ""
    return f"{parsed.strftime('%A')} - {parsed.strftime('%B')} {parsed.day} - {hour_12}{minute_part}{ampm}"


def _yaml_escape(value: str | None) -> str:
    if value is None:
        return '""'
    text = str(value)
    if '"' in text or "\\" in text or ":" in text or "\n" in text:
        return json.dumps(text, ensure_ascii=False)
    return f'"{text}"'


def _slugify(text: str, maxlen: int = 80) -> str:
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:maxlen].rstrip("-") or "event"
