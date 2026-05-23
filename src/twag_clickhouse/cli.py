from __future__ import annotations

import argparse
import json
import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from .client import ClickHouseService
from .cloud import ClickHouseCloudClient, ClickHouseCloudConfig
from .config import ClickHouseConfig
from .nytw import NytwDataset, inspect_nytw_dataset, load_nytw_dataset
from .subconscious_agent import (
    NytwSubconsciousAgent,
    is_more_results_request,
    likely_event_list_question,
    requested_event_limit,
)
from .subconscious_deploy import build_run_payload, create_run, env_api_key, env_base_url
from .telegram_agent import run_telegram_agent


def _service() -> ClickHouseService:
    return ClickHouseService(ClickHouseConfig.from_env())


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


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


def inspect_nytw(args: argparse.Namespace) -> int:
    _print_json(inspect_nytw_dataset(NytwDataset.from_path(args.source)))
    return 0


def load_nytw(args: argparse.Namespace) -> int:
    counts = load_nytw_dataset(
        _service(),
        NytwDataset.from_path(args.source),
        replace=args.replace,
        batch_size=args.batch_size,
    )
    _print_json({"loaded": counts})
    return 0


def ask_nytw_agent(args: argparse.Namespace) -> int:
    agent = NytwSubconsciousAgent.from_env()
    if args.question:
        answer = agent.ask(args.question)
        print(answer)
        return 0

    print("TWAG agent. Ask a question, type 'more' for more event results, or 'exit'.")
    last_event_question: str | None = None
    last_event_offset = 0

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

        if is_more_results_request(question):
            if not last_event_question:
                print("Ask an event-list question first, then type 'more'.")
                continue
            last_event_offset += requested_event_limit(last_event_question)
            answer = agent.ask(last_event_question, event_offset=last_event_offset)
            print(answer)
            continue

        answer = agent.ask(question)
        if likely_event_list_question(question):
            last_event_question = question
            last_event_offset = 0
        print(answer)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) if sys.argv else "twag",
        description="TWAG NY Tech Week ClickHouse agent CLI.",
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
        default="data/nytw-2026-for-agents",
        help="Path containing events/, users.json, and manifest.json",
    )
    inspect_nytw_parser.set_defaults(func=inspect_nytw)

    load_nytw_parser = subparsers.add_parser(
        "load-nytw",
        help="Create ClickHouse tables and load the NY Tech Week dataset",
    )
    load_nytw_parser.add_argument(
        "--source",
        default="data/nytw-2026-for-agents",
        help="Path containing events/, users.json, and manifest.json",
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
        help="Ask the Subconscious-backed agent, using Senso by default and ClickHouse for NYTW events",
    )
    agent_parser.add_argument(
        "question",
        nargs="?",
        help="Question to answer from Senso or nytw_* tables. Omit to start a dialogue.",
    )
    agent_parser.set_defaults(func=ask_nytw_agent)

    ask_agent_parser = subparsers.add_parser(
        "ask-nytw-agent",
        help="Ask the Subconscious-backed agent, using Senso by default and ClickHouse for NYTW events",
    )
    ask_agent_parser.add_argument(
        "question",
        nargs="?",
        help="Question to answer from Senso or nytw_* tables. Omit to start a dialogue.",
    )
    ask_agent_parser.set_defaults(func=ask_nytw_agent)

    deploy_agent_parser = subparsers.add_parser(
        "deploy-nytw-agent",
        help="Create a hosted Subconscious run that uses a public NYTW ClickHouse tool",
    )
    deploy_agent_parser.add_argument("question", help="Question for the hosted agent run")
    deploy_agent_parser.add_argument(
        "--tool-url",
        default=None,
        help="Public HTTPS base URL for twag-nytw-tool-server, without /query",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv(".env", override=False)

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
