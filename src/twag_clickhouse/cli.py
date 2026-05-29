from __future__ import annotations

import argparse
import json
import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from .city import active_city, load_city
from .calendar_sync import (
    CalendarSyncConfig,
    DEFAULT_CITIES,
    calendar_sync_overview,
    sync_configured_calendars,
)
from .client import ClickHouseService
from .cloud import ClickHouseCloudClient, ClickHouseCloudConfig
from .conversation import AgentConversation
from .config import ClickHouseConfig
from .geocode import geocode_city
from .geojson_export import build_gallery, build_geojson
from .thumbnails import build_thumbnails
from .nytw import NytwDataset, inspect_nytw_dataset, load_nytw_dataset
from .rendering import render_terminal_markdown
from .senso import SensoConfig, SensoService, senso_sync_overview, sync_senso_kb
from .subconscious_agent import NytwSubconsciousAgent
from .subconscious_deploy import build_run_payload, create_run, env_api_key, env_base_url
from .telegram_agent import run_telegram_agent


def _service() -> ClickHouseService:
    return ClickHouseService(ClickHouseConfig.from_env())


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _print_answer(answer: str) -> None:
    print(render_terminal_markdown(answer, enabled=sys.stdout.isatty()))


def health(_: argparse.Namespace) -> int:
    service = _service()
    _print_json({"ok": service.ping(), "config": service.config.safe_dict()})
    return 0


def query(args: argparse.Namespace) -> int:
    _print_json(_service().query(args.sql))
    return 0


def init_demo(_: argparse.Namespace) -> int:
    _service().create_demo_table()
    print("analytics_events table is ready")
    return 0


def insert_event(args: argparse.Namespace) -> int:
    service = _service()
    service.create_demo_table()
    service.insert_event(args.event_name, args.properties)
    print("event inserted")
    return 0


def resolve_cloud_service(_: argparse.Namespace) -> int:
    client = ClickHouseCloudClient(ClickHouseCloudConfig.from_env())
    _print_json(client.connection_defaults())
    return 0


def _resolve_source(args: argparse.Namespace) -> str:
    return args.source or active_city().dataset_path


def inspect_nytw(args: argparse.Namespace) -> int:
    _print_json(inspect_nytw_dataset(NytwDataset.from_path(_resolve_source(args))))
    return 0


def load_nytw(args: argparse.Namespace) -> int:
    counts = load_nytw_dataset(
        _service(),
        NytwDataset.from_path(_resolve_source(args)),
        replace=args.replace,
        batch_size=args.batch_size,
    )
    _print_json({"loaded": counts})
    return 0


def sync_senso(args: argparse.Namespace) -> int:
    config = SensoConfig.from_env()
    if config is None:
        raise ValueError("SENSO_API_KEY is required")
    counts = sync_senso_kb(
        _service(),
        SensoService(config),
        replace=args.replace,
        batch_size=args.batch_size,
        chunk_chars=args.chunk_chars,
        chunk_overlap=args.chunk_overlap,
    )
    _print_json({"synced": counts})
    return 0


def sync_senso_log(args: argparse.Namespace) -> int:
    _print_json(
        senso_sync_overview(
            _service(),
            limit=args.limit,
            item_limit=args.item_limit,
        )
    )
    return 0


def sync_calendars(args: argparse.Namespace) -> int:
    city_slugs = tuple(
        slug.strip().lower()
        for slug in (args.cities or ",".join(DEFAULT_CITIES)).split(",")
        if slug.strip()
    )
    config = CalendarSyncConfig(
        city_slugs=city_slugs,
        interval_seconds=0,
        include_invite_only=args.include_invite_only,
        fetch_concurrency=args.fetch_concurrency,
        batch_size=args.batch_size,
        fetch_timeout_seconds=args.fetch_timeout_seconds,
        max_scroll_ticks=args.max_scroll_ticks,
        scroll_delay_seconds=args.scroll_delay_seconds,
        page_delay_seconds=args.page_delay_seconds,
    )
    _print_json({"synced": sync_configured_calendars(_service(), config)})
    return 0


def sync_calendar_log(args: argparse.Namespace) -> int:
    city_slugs = tuple(slug.strip().lower() for slug in args.cities.split(",") if slug.strip())
    _print_json(
        calendar_sync_overview(
            _service(),
            city_slugs=city_slugs,
            limit=args.limit,
            item_limit=args.item_limit,
        )
    )
    return 0


def ask_nytw_agent(args: argparse.Namespace) -> int:
    agent = NytwSubconsciousAgent.from_env()
    conversation = AgentConversation()

    def ask(question: str) -> str:
        if not args.verbose:
            return conversation.answer(agent, question)

        print("[stream]", flush=True)
        saw_stream = False

        def raw_stream(chunk: str) -> None:
            nonlocal saw_stream
            saw_stream = True
            print(chunk, end="", flush=True)

        answer = conversation.answer(
            agent,
            question,
            stream_callback=lambda _partial: None,
            raw_stream_callback=raw_stream,
        )
        if saw_stream:
            print("\n[/stream]", flush=True)
        else:
            print("[stream unavailable for direct ClickHouse path]", flush=True)
        return answer

    if args.question:
        answer = ask(args.question)
        _print_answer(answer)
        return 0

    print("TWAG agent. Ask a question, type 'more' for more event results, or 'exit'.")

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            return 0

        answer = ask(question)
        _print_answer(answer)

    return 0


def deploy_nytw_agent(args: argparse.Namespace) -> int:
    tool_url = args.tool_url or os.getenv("NYTW_TOOL_URL", "").strip()
    if not tool_url:
        raise ValueError("NYTW_TOOL_URL is required, or pass --tool-url")

    payload = build_run_payload(
        question=args.question,
        tool_url=tool_url,
        engine=args.engine,
        tool_token=args.tool_token,
    )

    if args.print_payload:
        _print_json(payload)
        return 0

    _print_json(
        create_run(
            payload,
            api_key=env_api_key(),
            base_url=env_base_url(),
        )
    )
    return 0


def run_telegram_nytw_agent(_: argparse.Namespace) -> int:
    return run_telegram_agent()


def run_terminal_server(_: argparse.Namespace) -> int:
    from .terminal_server import main as terminal_server_main

    terminal_server_main()
    return 0


def geocode_venues(args: argparse.Namespace) -> int:
    result = geocode_city(refresh=args.refresh, limit=args.limit)
    _print_json(result)
    return 0


def export_geojson(args: argparse.Namespace) -> int:
    _ = args
    result = build_geojson()
    _print_json(result)
    return 0


def export_gallery(args: argparse.Namespace) -> int:
    _ = args
    result = build_gallery()
    _print_json(result)
    return 0


def make_thumbnails(args: argparse.Namespace) -> int:
    result = build_thumbnails(refresh=args.refresh)
    _print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) if sys.argv else "twag",
        description="TWAG Tech Week ClickHouse agent CLI.",
    )
    parser.add_argument(
        "--city",
        default=None,
        help="Override TWAG_CITY for this invocation (e.g. nyc, boston).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health_parser = subparsers.add_parser("health", help="Check ClickHouse connectivity")
    health_parser.set_defaults(func=health)

    query_parser = subparsers.add_parser("query", help="Run a SQL query")
    query_parser.add_argument("sql", help="SQL query to execute")
    query_parser.set_defaults(func=query)

    init_parser = subparsers.add_parser(
        "init-demo",
        help="Create the example analytics_events table",
    )
    init_parser.set_defaults(func=init_demo)

    insert_parser = subparsers.add_parser(
        "insert-event",
        help="Insert one row into analytics_events",
    )
    insert_parser.add_argument("event_name", help="Event name")
    insert_parser.add_argument(
        "properties",
        nargs="?",
        default="{}",
        help="JSON string with event properties",
    )
    insert_parser.set_defaults(func=insert_event)

    resolve_parser = subparsers.add_parser(
        "resolve-cloud-service",
        help="Resolve the ClickHouse Cloud SQL endpoint for CLICKHOUSE_SERVICE_ID",
    )
    resolve_parser.set_defaults(func=resolve_cloud_service)

    inspect_nytw_parser = subparsers.add_parser(
        "inspect-nytw",
        help="Validate and count the local NY Tech Week dataset",
    )
    inspect_nytw_parser.add_argument(
        "--source",
        default=None,
        help="Path containing events/, users.json, and manifest.json (defaults to the active city's dataset)",
    )
    inspect_nytw_parser.set_defaults(func=inspect_nytw)

    load_nytw_parser = subparsers.add_parser(
        "load-nytw",
        help="Create ClickHouse tables and load the NY Tech Week dataset",
    )
    load_nytw_parser.add_argument(
        "--source",
        default=None,
        help="Path containing events/, users.json, and manifest.json (defaults to the active city's dataset)",
    )
    load_nytw_parser.add_argument(
        "--replace",
        action="store_true",
        help="Truncate NYTW ClickHouse tables before loading",
    )
    load_nytw_parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows to insert per ClickHouse batch",
    )
    load_nytw_parser.set_defaults(func=load_nytw)

    agent_parser = subparsers.add_parser(
        "agent",
        help="Ask the Subconscious-backed ClickHouse agent for NYTW events and synced Senso KB context",
    )
    agent_parser.add_argument(
        "question",
        nargs="?",
        help="Question to answer from nytw_* or synced senso_* tables. Omit to start a dialogue.",
    )
    agent_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream the raw Subconscious response, including thinking tags when emitted.",
    )
    agent_parser.set_defaults(func=ask_nytw_agent)

    ask_agent_parser = subparsers.add_parser(
        "ask-nytw-agent",
        help="Ask the Subconscious-backed ClickHouse agent for NYTW events and synced Senso KB context",
    )
    ask_agent_parser.add_argument(
        "question",
        nargs="?",
        help="Question to answer from nytw_* or synced senso_* tables. Omit to start a dialogue.",
    )
    ask_agent_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream the raw Subconscious response, including thinking tags when emitted.",
    )
    ask_agent_parser.set_defaults(func=ask_nytw_agent)

    sync_senso_parser = subparsers.add_parser(
        "sync-senso",
        help="Mirror Senso knowledge-base content into ClickHouse senso_* tables",
    )
    sync_senso_parser.add_argument(
        "--replace",
        action="store_true",
        help="Truncate Senso ClickHouse mirror tables before syncing",
    )
    sync_senso_parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows to insert per ClickHouse batch",
    )
    sync_senso_parser.add_argument(
        "--chunk-chars",
        type=int,
        default=3500,
        help="Approximate characters per senso_kb_chunks row",
    )
    sync_senso_parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=300,
        help="Overlapping characters between neighboring chunks",
    )
    sync_senso_parser.set_defaults(func=sync_senso)

    sync_senso_log_parser = subparsers.add_parser(
        "sync-senso-log",
        help="Show recent Senso sync runs and document-level change summaries",
    )
    sync_senso_log_parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Number of recent sync runs to show",
    )
    sync_senso_log_parser.add_argument(
        "--item-limit",
        type=int,
        default=25,
        help="Maximum inserted/updated/removed document rows to include",
    )
    sync_senso_log_parser.set_defaults(func=sync_senso_log)

    sync_calendars_parser = subparsers.add_parser(
        "sync-calendars",
        help="Crawl NYC/Boston Tech Week calendars and refresh ClickHouse event tables",
    )
    sync_calendars_parser.add_argument(
        "--cities",
        default="nyc,boston",
        help="Comma-separated city slugs to sync (default: nyc,boston)",
    )
    sync_calendars_parser.add_argument(
        "--include-invite-only",
        action="store_true",
        help="Include invite-only calendar rows. Default matches the published dataset convention.",
    )
    sync_calendars_parser.add_argument(
        "--fetch-concurrency",
        type=int,
        default=3,
        help="Concurrent direct Partiful detail fetches",
    )
    sync_calendars_parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows to insert per ClickHouse batch",
    )
    sync_calendars_parser.add_argument(
        "--fetch-timeout-seconds",
        type=float,
        default=25.0,
        help="Timeout for each direct Partiful detail fetch",
    )
    sync_calendars_parser.add_argument(
        "--max-scroll-ticks",
        type=int,
        default=260,
        help="Maximum browser scroll ticks per calendar pass",
    )
    sync_calendars_parser.add_argument(
        "--scroll-delay-seconds",
        type=float,
        default=0.35,
        help="Delay between fallback browser calendar scroll ticks",
    )
    sync_calendars_parser.add_argument(
        "--page-delay-seconds",
        type=float,
        default=0.1,
        help="Delay between Tech Week calendar API page requests",
    )
    sync_calendars_parser.set_defaults(func=sync_calendars)

    sync_calendar_log_parser = subparsers.add_parser(
        "sync-calendar-log",
        help="Show recent Tech Week calendar sync runs and row-level changes",
    )
    sync_calendar_log_parser.add_argument(
        "--cities",
        default="nyc,boston",
        help="Comma-separated city slugs to inspect",
    )
    sync_calendar_log_parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Number of recent sync runs per city to show",
    )
    sync_calendar_log_parser.add_argument(
        "--item-limit",
        type=int,
        default=25,
        help="Maximum changed rows to include",
    )
    sync_calendar_log_parser.set_defaults(func=sync_calendar_log)

    deploy_agent_parser = subparsers.add_parser(
        "deploy-nytw-agent",
        help="Create a hosted Subconscious run that uses a public NYTW ClickHouse tool",
    )
    deploy_agent_parser.add_argument("question", help="Question for the hosted agent run")
    deploy_agent_parser.add_argument(
        "--tool-url",
        default=None,
        help="Public HTTPS base URL for twag-sync-agent, without /query",
    )
    deploy_agent_parser.add_argument(
        "--tool-token",
        default=os.getenv("NYTW_TOOL_TOKEN") or None,
        help="Optional NYTW_TOOL_TOKEN expected by the tool server",
    )
    deploy_agent_parser.add_argument(
        "--engine",
        default=os.getenv("SUBCONSCIOUS_RUN_ENGINE", "tim-gpt"),
        help="Subconscious runs engine to use",
    )
    deploy_agent_parser.add_argument(
        "--print-payload",
        action="store_true",
        help="Print the Subconscious run payload instead of posting it",
    )
    deploy_agent_parser.set_defaults(func=deploy_nytw_agent)

    telegram_agent_parser = subparsers.add_parser(
        "telegram-agent",
        help="Run the TWAG agent as a Telegram long-polling bot",
    )
    telegram_agent_parser.set_defaults(func=run_telegram_nytw_agent)

    terminal_server_parser = subparsers.add_parser(
        "terminal-server",
        help="Run the local-only TWAG terminal WebSocket backend",
    )
    terminal_server_parser.set_defaults(func=run_terminal_server)

    geocode_parser = subparsers.add_parser(
        "geocode-venues",
        help="Geocode each event venue via OpenCage and cache to venues.json",
    )
    geocode_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-geocode every venue, ignoring the existing cache",
    )
    geocode_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only geocode the first N events (debug)",
    )
    geocode_parser.set_defaults(func=geocode_venues)

    geojson_parser = subparsers.add_parser(
        "build-geojson",
        help="Join events + venues.json into events.geojson for the map page",
    )
    geojson_parser.set_defaults(func=export_geojson)

    gallery_parser = subparsers.add_parser(
        "build-gallery",
        help="Emit docs/<city>_gallery.json for the image gallery page",
    )
    gallery_parser.set_defaults(func=export_gallery)

    thumbs_parser = subparsers.add_parser(
        "build-thumbnails",
        help="Resize event images into docs/<city>/thumbs/ (~400px JPEG) for the gallery",
    )
    thumbs_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-render every thumb, ignoring existing files",
    )
    thumbs_parser.set_defaults(func=make_thumbnails)

    return parser


def main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv(".env", override=False)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.city:
        load_city(args.city)  # validate
        os.environ["TWAG_CITY"] = args.city.strip().lower()

    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
