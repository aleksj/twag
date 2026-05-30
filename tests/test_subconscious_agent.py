from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from twag_clickhouse.city import BOSTON
from twag_clickhouse.subconscious_agent import (
    FINAL_FORMAT_PROMPT,
    NytwSubconsciousAgent,
    SubconsciousConfig,
    UnsafeQueryError,
    add_default_limit,
    build_event_change_query,
    build_event_search_plan,
    build_query_tool,
    build_system_prompt,
    build_keyword_event_query,
    clean_model_answer,
    expanded_keyword_terms,
    extract_embedded_tool_calls,
    format_event_change_rows,
    format_event_rows,
    infer_change_query_since_date,
    infer_event_query_date,
    infer_event_geo_entity,
    is_more_results_request,
    likely_event_change_question,
    likely_event_list_question,
    likely_event_search_plan_question,
    likely_nytw_data_question,
    looks_like_planning_leak,
    merge_continued_text,
    placeholder_output_detected,
    response_was_truncated,
    requested_event_limit,
    render_event_search_sql,
    validate_nytw_query,
    verified_answer_or_placeholder_warning,
    visible_stream_content,
    wants_open_rsvps,
)


def test_validate_nytw_query_accepts_read_only_nytw_select() -> None:
    sql = validate_nytw_query(
        "SELECT title FROM nytw_events WHERE fetch_status = 'ok' LIMIT 10"
    )

    assert sql.startswith("SELECT title")


def test_validate_nytw_query_accepts_sync_changes_select() -> None:
    sql = validate_nytw_query(
        "SELECT title FROM nytw_sync_changes WHERE change_type = 'updated' LIMIT 10"
    )

    assert sql.startswith("SELECT title")


def test_validate_nytw_query_accepts_synced_senso_select() -> None:
    sql = validate_nytw_query("SELECT title FROM senso_kb_chunks LIMIT 10")

    assert sql.startswith("SELECT title")


def test_validate_nytw_query_accepts_cross_city_union_select() -> None:
    sql = validate_nytw_query(
        "SELECT 'NYC' AS city, title FROM nytw_current_events "
        "UNION ALL "
        "SELECT 'Boston' AS city, title FROM bostw_current_events "
        "LIMIT 10"
    )

    assert "bostw_current_events" in sql


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


def test_visible_stream_content_hides_thinking_until_final_answer() -> None:
    assert visible_stream_content("<think>planning") == ""
    assert visible_stream_content("<think>planning</think>Final answer") == "Final answer"
    assert visible_stream_content("Final answer") == "Final answer"


def test_looks_like_planning_leak_detects_verbose_process_output() -> None:
    content = (
        "The user is asking for the top events. I need to query ClickHouse "
        "and I should rank them by relevance before I execute the SQL query."
    )

    assert looks_like_planning_leak(content)
    assert looks_like_planning_leak("<think>I need to call query_nytw_clickhouse with SQL</think>")
    assert not looks_like_planning_leak("There are 1,360 live events.")


def test_system_prompt_includes_current_local_date_context() -> None:
    prompt = build_system_prompt(
        BOSTON,
        now=datetime(2026, 5, 26, 0, 49, tzinfo=timezone.utc),
    )

    assert "Current local date: Monday, 2026-05-25" in prompt
    assert "Current local datetime: 2026-05-25 20:49:00 EDT" in prompt
    assert "Dataset event date range: May 24-31, 2026" in prompt
    assert 'Interpret relative dates like "today", "tomorrow"' in prompt
    assert "Placeholder content is invalid" in prompt
    assert "morning is 05:00-11:59" in prompt
    assert "preserve the provided result page" in prompt
    assert "hand-picking a smaller subset" in prompt
    assert "nytw_current_events and bostw_current_events" in prompt
    assert "views already select the latest completed sync" in prompt
    assert "UNION ALL matching SELECT lists" in prompt
    assert "start_time is a display string" in prompt
    assert "table_name = 'bostw_events'" in prompt
    assert "senso_* only for general-purpose Tech Week context" in prompt
    assert "First identify hard filters" in prompt
    assert "Only topic words\n  should become text-match terms" in prompt
    assert "Keep SQL succinct" in prompt
    assert "Use only the real columns listed below" in prompt
    assert "Do not invent search_text, tags" in prompt
    assert "Match topic words against title, description, markdown_body" in prompt
    assert "one multiSearchAnyCaseInsensitive(concatWithSeparator" in prompt
    assert "Simple recipe:" in prompt
    assert "ORDER BY event_date ASC, start_at ASC, title ASC" in prompt
    assert "Do not match filler words" in prompt
    assert "The current schema does not provide embedding vectors" in prompt


def test_query_tool_guides_llm_to_current_views_and_cross_city_union() -> None:
    tool = build_query_tool(BOSTON)
    sql_description = tool["function"]["parameters"]["properties"]["sql"]["description"]

    assert "nytw_* or bostw_*" in sql_description
    assert "current_events views" in sql_description
    assert "UNION ALL across nytw_current_events and bostw_current_events" in sql_description
    assert "put hard filters in WHERE" in sql_description
    assert "topic terms only from topic words and synonyms" in sql_description
    assert "keep SQL succinct" in sql_description
    assert "multiSearchAnyCaseInsensitive" in sql_description
    assert "concatWithSeparator" in sql_description
    assert "ORDER BY event_date ASC, start_at ASC, title ASC" in sql_description
    assert "Do not invent search_text" in sql_description


def test_final_format_prompt_preserves_event_page() -> None:
    assert "preserve the page of event rows" in FINAL_FORMAT_PROMPT
    assert "Do not reduce it to a hand-picked top 5" in FINAL_FORMAT_PROMPT


def test_placeholder_output_is_rejected() -> None:
    answer = (
        "**Startup Networking Mixer** — June 2, 2026 — Chelsea — "
        "[RSVP](https://example.com/rsvp1)"
    )

    assert placeholder_output_detected(answer)
    assert "could not verify" in verified_answer_or_placeholder_warning(answer)


def test_response_was_truncated_detects_length_finish_reason() -> None:
    assert response_was_truncated({"choices": [{"finish_reason": "length"}]})
    assert response_was_truncated({"choices": [{"stop_reason": "max_tokens"}]})
    assert not response_was_truncated({"choices": [{"finish_reason": "stop"}]})


def test_merge_continued_text_deduplicates_repeated_prefix() -> None:
    assert (
        merge_continued_text(
            "The answer starts but stops",
            "The answer starts but stops and now finishes.",
        )
        == "The answer starts but stops and now finishes."
    )
    assert merge_continued_text("First half", " second half") == "First half second half"


def test_extract_embedded_tool_calls_recovers_json_tool_content() -> None:
    content = (
        '<think>{"name":"query_nytw_clickhouse","arguments":'
        '{"sql":"SELECT count() FROM nytw_events"}}</think>'
    )

    calls = extract_embedded_tool_calls(content)

    assert len(calls) == 1
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["sql"] == "SELECT count() FROM nytw_events"


def test_requested_event_limit_reads_top_n() -> None:
    assert requested_event_limit("top 3 AI agent orchestration events") == 3
    assert requested_event_limit("best 50 events") == 50
    assert requested_event_limit("best 200 events") == 100
    assert requested_event_limit("AI events") == 25


def test_build_keyword_event_query_is_limited_and_targets_current_events() -> None:
    sql = build_keyword_event_query("top 3 AI agent orchestration events")

    assert "FROM nytw_current_events" in sql
    assert "event_id" in sql
    assert "count() OVER () AS total_matches" in sql
    assert "LIMIT 3" in sql
    assert "orchestration" in sql
    assert "multiSearchAnyCaseInsensitive" in sql
    assert "concatWithSeparator" in sql
    assert "neighborhood" in sql
    assert "venue_name" in sql
    assert "arrayStringConcat(badges" in sql
    assert "retrieval_context" not in sql
    assert " OR " not in sql
    assert "relevance_score" not in sql
    assert "ORDER BY event_date ASC, start_at ASC, title ASC" in sql


def test_build_keyword_event_query_supports_offset() -> None:
    sql = build_keyword_event_query("list events involving running", offset=5)

    assert "LIMIT 25" in sql
    assert "OFFSET 5" in sql


def test_build_keyword_event_query_filters_open_rsvps_without_polluting_keywords() -> None:
    sql = build_keyword_event_query("Show cybersecurity events with open RSVPs")

    assert wants_open_rsvps("Show cybersecurity events with open RSVPs")
    assert "rsvp_url != ''" in sql
    assert "NOT at_capacity" in sql
    assert "remaining_capacity IS NULL OR remaining_capacity > 0" in sql
    assert "cybersecurity" in sql
    assert "%open%" not in sql
    assert "%rsvps%" not in sql


def test_build_keyword_event_query_hard_filters_neighborhood_weekday_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-30")

    sql = build_keyword_event_query("events in east village on tuesday morning")

    assert infer_event_query_date("events in east village on tuesday morning") == "2026-06-02"
    assert "event_date = '2026-06-02'" in sql
    assert "neighborhood ILIKE '%east village%'" in sql
    assert "toHour(start_at) >= 5 AND toHour(start_at) < 12" in sql
    assert "%east%" not in sql
    assert "%village%" not in sql
    assert "%tuesday%" not in sql
    assert "%morning%" not in sql
    assert "ORDER BY event_date ASC, start_at ASC, title ASC" in sql


def test_build_keyword_event_query_treats_weekday_abbreviation_as_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-30")

    sql = build_keyword_event_query("gaming events in flatiron on tue")

    assert infer_event_query_date("gaming events in flatiron on tue") == "2026-06-02"
    assert "event_date = '2026-06-02'" in sql
    assert "neighborhood ILIKE '%flatiron%'" in sql
    assert "'gaming'" in sql
    assert "%tue%" not in sql


def test_build_keyword_event_query_localizes_known_geo_entities() -> None:
    entity = infer_event_geo_entity("events near Columbia University")
    sql = build_keyword_event_query("events near Columbia University")

    assert entity is not None
    assert entity.neighborhood == "upper manhattan"
    assert "neighborhood ILIKE '%upper manhattan%'" in sql
    assert "relevance_score" not in sql
    assert "%near%" not in sql
    assert "%columbia%" not in sql
    assert "%university%" not in sql
    assert "ORDER BY event_date ASC, start_at ASC, title ASC" in sql


def test_build_keyword_event_query_combines_topic_with_geo_entity() -> None:
    sql = build_keyword_event_query("ai events near Columbia University")

    assert "neighborhood ILIKE '%upper manhattan%'" in sql
    assert "'ai'" in sql
    assert "%columbia%" not in sql
    assert "ORDER BY event_date ASC, start_at ASC, title ASC" in sql


def test_expanded_keyword_terms_handles_running() -> None:
    terms = expanded_keyword_terms("list events involving running")

    assert "running" in terms
    assert "run" in terms
    assert "5k" in terms


def test_expanded_keyword_terms_handles_hacker_queries() -> None:
    terms = expanded_keyword_terms("hacker events")

    assert terms[:3] == ["hacker", "hackers", "hackathon"]
    assert "cybersecurity" in terms


def test_event_change_query_uses_sync_changes_since_yesterday(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-30")

    question = "what has changed from yesterday?"
    sql = build_event_change_query(question)

    assert likely_event_change_question(question)
    assert infer_change_query_since_date(question) == "2026-05-29"
    assert "FROM nytw_sync_changes" in sql
    assert "table_name = 'nytw_events'" in sql
    assert "synced_at >= toDateTime64('2026-05-29 00:00:00', 3, 'UTC')" in sql
    assert "change_type IN ('inserted', 'updated', 'removed')" in sql


def test_event_change_query_can_filter_added_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-30")

    sql = build_event_change_query("events added in the last 3 days")

    assert "change_type = 'inserted'" in sql
    assert "2026-05-27 00:00:00" in sql
    assert "LIMIT 25" in sql


def test_format_event_change_rows_is_deterministic() -> None:
    output = format_event_change_rows(
        {
            "ok": True,
            "rows": [
                {
                    "event_id": "abc",
                    "title": "New event",
                    "change_type": "inserted",
                    "changed_fields": ["title", "event_date"],
                    "synced_at": "2026-05-30 01:02:03",
                    "total_matches": 1,
                }
            ],
        },
        since_date="2026-05-29",
    )

    assert "Showing 1-1 of 1 event change since 2026-05-29." in output
    assert "**New event** — added — title, event_date — 2026-05-30 01:02:03" in output


def test_is_more_results_request() -> None:
    assert is_more_results_request("more")
    assert is_more_results_request("show more")
    assert not is_more_results_request("more running events")


def test_likely_nytw_data_question_detects_event_data_requests() -> None:
    assert likely_nytw_data_question("How many NY Tech Week events are in SoHo?")
    assert likely_nytw_data_question("How many events are in SoHo?")
    assert not likely_nytw_data_question("What is our refund policy?")
    assert not likely_nytw_data_question("What is our refund policy for events?")


def test_likely_event_list_question_requires_event_search_intent() -> None:
    assert likely_event_list_question("list events involving running")
    assert likely_event_list_question("top 3 AI events")
    assert likely_event_list_question("events in upper west side?")
    assert not likely_event_list_question("What is our refund policy for events?")


def test_likely_event_search_plan_question_covers_topic_shorthand() -> None:
    assert likely_event_search_plan_question("hacker")
    assert likely_event_search_plan_question("events about AI & data")
    assert not likely_event_search_plan_question("what is our refund policy for events?")
    assert not likely_event_search_plan_question("what changed from yesterday?")


def test_event_search_plan_is_structured_and_rendered_succinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-30")

    plan = build_event_search_plan("open RSVP events in east village on tuesday morning")
    sql = render_event_search_sql(plan)

    assert plan.as_dict() == {
        "intent": "event_search",
        "city": "nyc",
        "terms": [],
        "date": "2026-06-02",
        "neighborhood": "east village",
        "time_filter": "(toHour(start_at) >= 5 AND toHour(start_at) < 12)",
        "open_rsvp": True,
        "limit": 25,
        "offset": 0,
    }
    assert len(sql) < 650
    assert "multiSearchAnyCaseInsensitive" not in sql
    assert "rsvp_url != ''" in sql
    assert "event_date = '2026-06-02'" in sql
    assert "neighborhood ILIKE '%east village%'" in sql


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


def test_format_event_rows_adds_more_hint_only_when_extra_row_exists() -> None:
    rows = [
        {
            "title": f"Event {index}",
            "event_date": "2026-06-06",
            "start_time": "9:00am ET",
            "neighborhood": "Upper West Side",
            "description_excerpt": "A focused event.",
            "rsvp_url": f"https://partiful.com/e/{index}",
        }
        for index in range(1, 4)
    ]

    output = format_event_rows(
        {"ok": True, "rows": rows},
        page_size=2,
        more_hint=True,
    )

    assert "Event 1" in output
    assert "Event 2" in output
    assert "Event 3" not in output
    assert "Send `more` for the next page" in output

    output_without_extra = format_event_rows(
        {"ok": True, "rows": rows[:2]},
        page_size=2,
        more_hint=True,
    )

    assert "Send `more` for the next page" not in output_without_extra


def test_format_event_rows_summarizes_total_matches_without_expanding_page() -> None:
    rows = [
        {
            "title": f"Event {index}",
            "event_date": "2026-06-06",
            "start_time": "9:00am ET",
            "neighborhood": "Upper West Side",
            "description_excerpt": "A focused event.",
            "rsvp_url": f"https://partiful.com/e/{index}",
            "total_matches": 68,
        }
        for index in range(1, 4)
    ]

    output = format_event_rows(
        {"ok": True, "rows": rows},
        offset=25,
        page_size=2,
        more_hint=True,
    )

    assert output.startswith("Showing 26-27 of 68 matching events.")
    assert "Event 1" in output
    assert "Event 2" in output
    assert "Event 3" not in output


class RecordingClickHouse:
    def __init__(self) -> None:
        self.sql: str | None = None

    def query(self, sql: str) -> list[dict[str, str]]:
        self.sql = sql
        return [
            {
                "title": f"Upper West Side Founder Breakfast {index}",
                "event_id": f"event-{index}",
                "event_date": "2026-06-03",
                "start_time": "9:00am ET",
                "end_time": "",
                "neighborhood": "Upper West Side",
                "venue_name": "Cafe",
                "description_excerpt": "Founders and operators meet over breakfast.",
                "rsvp_url": f"https://partiful.com/e/uws-{index}",
                "total_matches": "72",
            }
            for index in range(1, 28)
        ]


def test_agent_uses_structured_plan_for_plain_location_event_question() -> None:
    clickhouse = RecordingClickHouse()

    class PlannedSearchAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=clickhouse,  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Showing 1-25 of 72 matching events.",
                        }
                    }
                ]
            }

    agent = PlannedSearchAgent()

    answer = agent.ask("events in upper west side?")

    assert clickhouse.sql is not None
    assert "neighborhood ILIKE" in clickhouse.sql
    assert "LIMIT 25" in clickhouse.sql
    assert "count() OVER () AS total_matches" in clickhouse.sql
    assert answer == "Showing 1-25 of 72 matching events."
    assert [row["event_id"] for row in agent.last_event_map_rows] == [
        f"event-{index}" for index in range(1, 26)
    ]
    assert agent.calls == 1


def test_agent_returns_deterministic_error_when_tool_query_fails() -> None:
    class FailingClickHouse:
        def query(self, sql: str) -> list[dict[str, str]]:
            raise RuntimeError("Unknown expression identifier relevance_score")

    class FailingToolAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=FailingClickHouse(),  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "query_nytw_clickhouse",
                                        "arguments": json.dumps(
                                            {
                                                "sql": (
                                                    "SELECT title FROM nytw_current_events "
                                                    "ORDER BY relevance_score DESC LIMIT 25"
                                                )
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }

    agent = FailingToolAgent()

    answer = agent.ask("hacker events")

    assert "I couldn't run the data query" in answer
    assert "Unknown expression identifier relevance_score" in answer
    assert agent.calls == 0


def test_agent_uses_llm_tool_call_for_changed_since_yesterday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWAG_CURRENT_DATE", "2026-05-30")

    class ChangeClickHouse:
        def __init__(self) -> None:
            self.sql: str | None = None

        def query(self, sql: str) -> list[dict[str, object]]:
            self.sql = sql
            return [
                {
                    "event_id": "event-1",
                    "title": "Fresh event",
                    "change_type": "inserted",
                    "changed_fields": ["title"],
                    "synced_at": "2026-05-30 02:00:00",
                    "total_matches": 1,
                }
            ]

    clickhouse = ChangeClickHouse()

    class ChangeToolCallingAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=clickhouse,  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "query_nytw_clickhouse",
                                            "arguments": json.dumps(
                                                {
                                                    "sql": (
                                                        "SELECT record_id AS event_id, title, "
                                                        "change_type, changed_fields, synced_at, "
                                                        "count() OVER () AS total_matches "
                                                        "FROM nytw_sync_changes "
                                                        "WHERE table_name = 'nytw_events' "
                                                        "AND synced_at >= toDateTime64('2026-05-29 00:00:00', 3, 'UTC') "
                                                        "AND change_type IN ('inserted', 'updated', 'removed') "
                                                        "ORDER BY synced_at DESC, title ASC LIMIT 25"
                                                    )
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Fresh event changed yesterday.",
                        }
                    }
                ]
            }

    agent = ChangeToolCallingAgent()

    answer = agent.ask("what has changed from yesterday?")

    assert clickhouse.sql is not None
    assert "FROM nytw_sync_changes" in clickhouse.sql
    assert "2026-05-29 00:00:00" in clickhouse.sql
    assert answer == "Fresh event changed yesterday."
    assert agent.calls == 2


def test_agent_tool_query_compacts_large_rows() -> None:
    class VerboseClickHouse:
        def query(self, sql: str) -> list[dict[str, str]]:
            return [
                {
                    "event_id": "event-1",
                    "title": "Long event",
                    "raw_markdown": "should not reach model",
                    "description": "word " * 500,
                }
            ]

    agent = NytwSubconsciousAgent(
        clickhouse=VerboseClickHouse(),  # type: ignore[arg-type]
        subconscious=SubconsciousConfig(api_key="test"),
    )

    result = agent._query_sql("SELECT * FROM nytw_current_events LIMIT 1")

    assert result["rows"][0]["event_id"] == "event-1"
    assert "raw_markdown" not in result["rows"][0]
    assert len(result["rows"][0]["description"]) < 800


def test_agent_executes_embedded_tool_call_in_thinking_content() -> None:
    class AgentWithEmbeddedTool(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=RecordingClickHouse(),  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '<think>{"name":"query_nytw_clickhouse","arguments":'
                                    '{"sql":"SELECT title FROM nytw_events"}}</think>'
                                ),
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "Final answer"}}]}

    agent = AgentWithEmbeddedTool()

    assert agent.ask("how many events?") == "Final answer"
    assert agent.clickhouse.sql is not None  # type: ignore[union-attr]


def test_agent_falls_back_when_planning_stream_never_emits_sql() -> None:
    clickhouse = RecordingClickHouse()

    class PlanningOnlyAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=clickhouse,  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "I need to query nytw_current_events for events about AI "
                                    "and data. Let me write the SQL query:"
                                ),
                            },
                        }
                    ]
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "Fallback answer"}}
                ]
            }

    agent = PlanningOnlyAgent()

    answer = agent.ask("tell me about AI & data")

    assert answer == "Fallback answer"
    assert clickhouse.sql is not None
    assert "multiSearchAnyCaseInsensitive" in clickhouse.sql
    assert "AI & data" not in clickhouse.sql
    assert agent.calls == 2


def test_agent_rejects_empty_sql_without_clickhouse_validation_error() -> None:
    clickhouse = RecordingClickHouse()

    class EmptySqlAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=clickhouse,  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "query_nytw_clickhouse",
                                            "arguments": json.dumps({"sql": ""}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "Fallback answer"}}
                ]
            }

    agent = EmptySqlAgent()

    answer = agent.ask("tell me about AI & data")

    assert "I couldn't run the data query" in answer
    assert "The model did not produce a SQL query" in answer
    assert "SQL query is empty" not in answer
    assert clickhouse.sql is None
    assert agent.calls == 1


def test_agent_from_env_does_not_require_clickhouse_until_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBCONSCIOUS_API_KEY", "subconscious-test")
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)
    monkeypatch.delenv("CLICKHOUSE_API_KEY", raising=False)

    agent = NytwSubconsciousAgent.from_env()

    assert agent.clickhouse is None


def test_subconscious_config_enables_thinking_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBCONSCIOUS_API_KEY", "subconscious-test")
    monkeypatch.delenv("SUBCONSCIOUS_ENABLE_THINKING", raising=False)

    assert SubconsciousConfig.from_env().enable_thinking is True

    monkeypatch.setenv("SUBCONSCIOUS_ENABLE_THINKING", "false")

    assert SubconsciousConfig.from_env().enable_thinking is False


def test_chat_request_includes_thinking_flag() -> None:
    agent = NytwSubconsciousAgent(subconscious=SubconsciousConfig(api_key="test"))
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        agent._chat([{"role": "user", "content": "hi"}])

    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_chat_request_can_override_thinking_flag() -> None:
    agent = NytwSubconsciousAgent(subconscious=SubconsciousConfig(api_key="test"))
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        agent._chat([{"role": "user", "content": "hi"}], enable_thinking=False)

    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_agent_uses_thinking_for_query_generation_and_disables_it_for_presentation() -> None:
    class PresentationThinkingAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=RecordingClickHouse(),  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0
            self.thinking_flags = []

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            self.thinking_flags.append(kwargs.get("enable_thinking"))
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "query_nytw_clickhouse",
                                            "arguments": json.dumps(
                                                {"sql": "SELECT title FROM nytw_events"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "Final answer"}}
                ]
            }

    agent = PresentationThinkingAgent()

    assert agent.ask("how many events?") == "Final answer"
    assert agent.thinking_flags == [True, False]


def test_agent_reports_nonstream_token_usage() -> None:
    class UsageAgent(NytwSubconsciousAgent):
        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "choices": [{"message": {"role": "assistant", "content": "Final answer"}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "total_tokens": 15,
                },
            }

    agent = UsageAgent(subconscious=SubconsciousConfig(api_key="test"))
    usage = []

    assert agent.ask("how many events?", token_usage_callback=usage.append) == "Final answer"
    assert usage == [{"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}]


def test_agent_continues_truncated_nonstream_response() -> None:
    class TruncatedAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(subconscious=SubconsciousConfig(api_key="test"))
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "role": "assistant",
                                "content": "The answer starts but stops",
                            },
                        }
                    ]
                }
            assert "cut off" in messages[-1]["content"]
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "The answer starts but stops and now finishes.",
                        },
                    }
                ]
            }

    agent = TruncatedAgent()

    assert agent.ask("how many events?") == "The answer starts but stops and now finishes."
    assert agent.calls == 2


def test_agent_streams_tool_final_answer_to_visible_callback() -> None:
    class BufferedFinalAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=RecordingClickHouse(),  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0
            self.streamed_final = False

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "query_nytw_clickhouse",
                                            "arguments": json.dumps(
                                                {"sql": "SELECT title FROM nytw_events"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            if kwargs.get("stream_callback"):
                self.streamed_final = True
                kwargs["stream_callback"]("No matching events found.")
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "No matching events found.",
                            }
                        }
                    ]
                }

            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "No matching events found."}}
                ]
            }

    agent = BufferedFinalAgent()
    streamed = []

    answer = agent.ask("what about knowledge graph context", stream_callback=streamed.append)

    assert answer == "No matching events found."
    assert streamed == ["No matching events found."]
    assert agent.streamed_final is True


def test_agent_continues_truncated_verbose_stream_response() -> None:
    class TruncatedStreamAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=RecordingClickHouse(),  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "query_nytw_clickhouse",
                                            "arguments": json.dumps(
                                                {"sql": "SELECT title FROM nytw_events"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            if self.calls == 2:
                kwargs["raw_stream_callback"]("The previous query returned 0 rows and")
                kwargs["stream_callback"]("The previous query returned 0 rows and")
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "role": "assistant",
                                "content": "The previous query returned 0 rows and",
                            },
                        }
                    ]
                }

            assert "cut off" in messages[-1]["content"]
            kwargs["raw_stream_callback"](" now it finishes.")
            kwargs["stream_callback"](" now it finishes.")
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": " now it finishes.",
                        },
                    }
                ]
            }

    agent = TruncatedStreamAgent()
    visible = []
    raw = []

    answer = agent.ask(
        "what about knowledge graph context",
        stream_callback=visible.append,
        raw_stream_callback=raw.append,
    )

    assert answer == "The previous query returned 0 rows and now it finishes."
    assert agent.calls == 3
    assert raw == ["The previous query returned 0 rows and", " now it finishes."]


def test_agent_can_still_verbose_stream_tool_final_answer() -> None:
    class VerboseFinalAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=RecordingClickHouse(),  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "query_nytw_clickhouse",
                                            "arguments": json.dumps(
                                                {"sql": "SELECT title FROM nytw_events"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            if kwargs.get("stream_callback"):
                kwargs["raw_stream_callback"]("<think>plan</think>Answer")
                kwargs["stream_callback"]("Answer")
            return {"choices": [{"message": {"role": "assistant", "content": "Answer"}}]}

    agent = VerboseFinalAgent()
    visible = []
    raw = []

    answer = agent.ask(
        "what about knowledge graph context",
        stream_callback=visible.append,
        raw_stream_callback=raw.append,
    )

    assert answer == "Answer"
    assert visible == ["Answer"]
    assert raw == ["<think>plan</think>Answer"]


def test_agent_streams_query_generation_planning_separately_from_final_answer() -> None:
    class PlanningStreamAgent(NytwSubconsciousAgent):
        def __init__(self) -> None:
            super().__init__(
                clickhouse=RecordingClickHouse(),  # type: ignore[arg-type]
                subconscious=SubconsciousConfig(api_key="test"),
            )
            self.calls = 0

        def _chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                assert kwargs.get("enable_thinking") is True
                assert callable(kwargs.get("stream_callback"))
                kwargs["raw_stream_callback"]("<think>build sql</think>")
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "query_nytw_clickhouse",
                                            "arguments": json.dumps(
                                                {"sql": "SELECT title FROM nytw_events"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            if kwargs.get("stream_callback"):
                kwargs["stream_callback"]("Answer")
            return {"choices": [{"message": {"role": "assistant", "content": "Answer"}}]}

    agent = PlanningStreamAgent()
    planning = []
    visible = []

    answer = agent.ask(
        "what about knowledge graph context",
        stream_callback=visible.append,
        planning_stream_callback=planning.append,
    )

    assert answer == "Answer"
    assert planning == ["<think>build sql</think>"]
    assert visible == ["Answer"]


def test_chat_stream_accumulates_visible_content_after_thinking() -> None:
    agent = NytwSubconsciousAgent(subconscious=SubconsciousConfig(api_key="test"))
    updates = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            chunks = [
                {"choices": [{"delta": {"content": "<think>plan"}}]},
                {"choices": [{"delta": {"content": "</think>Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
            ]
            for chunk in chunks:
                yield f"data: {json.dumps(chunk)}\n".encode()
            yield b"data: [DONE]\n"

    with patch("urllib.request.urlopen", return_value=Response()):
        response = agent._chat(
            [{"role": "user", "content": "hi"}],
            stream_callback=updates.append,
        )

    assert response["choices"][0]["message"]["content"] == "<think>plan</think>Hello world"
    assert response["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert updates == ["Hello", " world"]


def test_chat_stream_can_emit_raw_thinking_content() -> None:
    agent = NytwSubconsciousAgent(subconscious=SubconsciousConfig(api_key="test"))
    visible_updates = []
    raw_updates = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            chunks = [
                {"choices": [{"delta": {"content": "<think>plan"}}]},
                {"choices": [{"delta": {"content": "</think>Answer"}}]},
            ]
            for chunk in chunks:
                yield f"data: {json.dumps(chunk)}\n".encode()
            yield b"data: [DONE]\n"

    with patch("urllib.request.urlopen", return_value=Response()):
        agent._chat(
            [{"role": "user", "content": "hi"}],
            stream_callback=visible_updates.append,
            raw_stream_callback=raw_updates.append,
        )

    assert visible_updates == ["Answer"]
    assert raw_updates == ["<think>plan", "</think>Answer"]
