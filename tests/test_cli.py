from __future__ import annotations

from twag_clickhouse.cli import build_parser


def test_agent_parser_accepts_verbose_flag() -> None:
    args = build_parser().parse_args(["agent", "--verbose", "hello"])

    assert args.command == "agent"
    assert args.verbose is True
    assert args.question == "hello"


def test_ask_nytw_agent_parser_accepts_verbose_flag() -> None:
    args = build_parser().parse_args(["ask-nytw-agent", "--verbose", "hello"])

    assert args.command == "ask-nytw-agent"
    assert args.verbose is True
    assert args.question == "hello"


def test_terminal_server_parser() -> None:
    args = build_parser().parse_args(["terminal-server"])

    assert args.command == "terminal-server"


def test_sync_senso_parser_accepts_replace_and_chunk_options() -> None:
    args = build_parser().parse_args(
        [
            "sync-senso",
            "--replace",
            "--batch-size",
            "100",
            "--chunk-chars",
            "2000",
            "--chunk-overlap",
            "250",
        ]
    )

    assert args.command == "sync-senso"
    assert args.replace is True
    assert args.batch_size == 100
    assert args.chunk_chars == 2000
    assert args.chunk_overlap == 250


def test_sync_senso_log_parser_accepts_limits() -> None:
    args = build_parser().parse_args(
        [
            "sync-senso-log",
            "--limit",
            "3",
            "--item-limit",
            "10",
        ]
    )

    assert args.command == "sync-senso-log"
    assert args.limit == 3
    assert args.item_limit == 10
