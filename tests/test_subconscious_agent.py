from __future__ import annotations

import pytest

from twag_clickhouse.subconscious_agent import (
    UnsafeQueryError,
    add_default_limit,
    build_keyword_event_query,
    clean_model_answer,
    expanded_keyword_terms,
    format_event_rows,
    is_more_results_request,
    looks_like_planning_leak,
    requested_event_limit,
    validate_nytw_query,
)


def test_validate_nytw_query_accepts_read_only_nytw_select() -> None:
    sql = validate_nytw_query(
        "SELECT title FROM nytw_events WHERE fetch_status = 'ok' LIMIT 10"
    )

    assert sql.startswith("SELECT title")


def test_validate_nytw_query_rejects_mutation() -> None:
    with pytest.raises(UnsafeQueryError):
        validate_nytw_query("DROP TABLE nytw_events")


def test_validate_nytw_query_rejects_unrelated_table() -> None:
    with pytest.raises(UnsafeQueryError):
        validate_nytw_query("SELECT * FROM analytics_events")


def test_add_default_limit_only_adds_to_unlimited_selects() -> None:
    assert add_default_limit("SELECT * FROM nytw_events").endswith("LIMIT 100")
    assert add_default_limit("SELECT * FROM nytw_events LIMIT 5").endswith("LIMIT 5")
    assert add_default_limit("SHOW TABLES") == "SHOW TABLES"


def test_clean_model_answer_removes_thinking_tail_marker() -> None:
    answer = clean_model_answer("scratch notes\n</think>\nThere are 10 events.")

    assert answer == "There are 10 events."


def test_looks_like_planning_leak_detects_verbose_process_output() -> None:
    content = (
        "The user is asking for the top events. I need to query ClickHouse "
        "and I should rank them by relevance before I execute the SQL query."
    )

    assert looks_like_planning_leak(content)
    assert not looks_like_planning_leak("There are 1,360 live events.")


def test_requested_event_limit_reads_top_n() -> None:
    assert requested_event_limit("top 3 AI agent orchestration events") == 3
    assert requested_event_limit("best 50 events") == 10
    assert requested_event_limit("AI events") == 5


def test_build_keyword_event_query_is_limited_and_targets_nytw_events() -> None:
    sql = build_keyword_event_query("top 3 AI agent orchestration events")

    assert "FROM nytw_events" in sql
    assert "LIMIT 3" in sql
    assert "orchestration" in sql


def test_build_keyword_event_query_supports_offset() -> None:
    sql = build_keyword_event_query("list events involving running", offset=5)

    assert "LIMIT 5" in sql
    assert "OFFSET 5" in sql


def test_expanded_keyword_terms_handles_running() -> None:
    terms = expanded_keyword_terms("list events involving running")

    assert "running" in terms
    assert "run" in terms
    assert "5k" in terms


def test_is_more_results_request() -> None:
    assert is_more_results_request("more")
    assert is_more_results_request("show more")
    assert not is_more_results_request("more running events")


def test_format_event_rows_is_deterministic_and_preserves_url() -> None:
    output = format_event_rows(
        {
            "ok": True,
            "rows": [
                {
                    "title": "Founders Running Club",
                    "event_date": "2026-06-06",
                    "start_time": "9:00am ET",
                    "end_time": "",
                    "neighborhood": "West Village",
                    "venue_name": "",
                    "description_excerpt": "A 5K networking run along the Hudson River.",
                    "rsvp_url": "https://partiful.com/e/example",
                }
            ],
        }
    )

    assert "**Founders Running Club**" in output
    assert "https://partiful.com/e/example" in output


def test_format_event_rows_handles_empty_followup_page() -> None:
    assert format_event_rows({"ok": True, "rows": []}, offset=5) == "No more matching events found."
