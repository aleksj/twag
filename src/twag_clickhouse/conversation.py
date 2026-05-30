from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Protocol

from .subconscious_agent import (
    TokenUsageCallback,
    is_more_results_request,
    likely_event_list_question,
    requested_event_limit,
)


class AgentLike(Protocol):
    def ask(
        self,
        question: str,
        *,
        event_offset: int = 0,
        enable_thinking: bool | None = None,
        stream_callback: Callable[[str], None] | None = None,
        raw_stream_callback: Callable[[str], None] | None = None,
        planning_stream_callback: Callable[[str], None] | None = None,
        detail_callback: Callable[[str], None] | None = None,
        token_usage_callback: TokenUsageCallback | None = None,
        progress_callback: Callable[[str], None] | None = None,
        conversation_context: str | None = None,
    ) -> str:
        ...


@dataclass
class ConversationTurn:
    user_text: str
    effective_question: str
    answer_summary: str
    sql_queries: list[str] = field(default_factory=list)


@dataclass
class AgentConversation:
    last_event_question: str | None = None
    last_event_offset: int = 0
    recent_turns: list[ConversationTurn] = field(default_factory=list)

    def answer(
        self,
        agent: AgentLike,
        text: str,
        *,
        stream_callback: Callable[[str], None] | None = None,
        raw_stream_callback: Callable[[str], None] | None = None,
        planning_stream_callback: Callable[[str], None] | None = None,
        detail_callback: Callable[[str], None] | None = None,
        enable_thinking: bool | None = None,
        token_usage_callback: TokenUsageCallback | None = None,
        progress_callback: Callable[[str], None] | None = None,
        no_previous_more_message: str = "Ask an event-list question first, then type 'more'.",
    ) -> str:
        if is_more_results_request(text):
            if not self.last_event_question:
                return no_previous_more_message
            self.last_event_offset += requested_event_limit(self.last_event_question)
            ask_kwargs = {
                "event_offset": self.last_event_offset,
                "stream_callback": stream_callback,
                "raw_stream_callback": raw_stream_callback,
                "planning_stream_callback": planning_stream_callback,
                "detail_callback": detail_callback,
                "token_usage_callback": token_usage_callback,
                "progress_callback": progress_callback,
                "conversation_context": self.context_block(),
            }
            if enable_thinking is not None:
                ask_kwargs["enable_thinking"] = enable_thinking
            return agent.ask(
                self.last_event_question,
                **ask_kwargs,
            )

        context = self.context_block()
        effective_text = self.effective_question(text)
        ask_kwargs = {
            "stream_callback": stream_callback,
            "raw_stream_callback": raw_stream_callback,
            "planning_stream_callback": planning_stream_callback,
            "detail_callback": detail_callback,
            "token_usage_callback": token_usage_callback,
            "progress_callback": progress_callback,
            "conversation_context": context,
        }
        if enable_thinking is not None:
            ask_kwargs["enable_thinking"] = enable_thinking
        answer = agent.ask(effective_text, **ask_kwargs)
        self.add_turn(
            user_text=text,
            effective_question=effective_text,
            answer=answer,
            sql_queries=list(getattr(agent, "last_sql_queries", []) or []),
        )
        followup_question = _event_followup_question(effective_text, answer)
        if followup_question:
            self.last_event_question = followup_question
            self.last_event_offset = 0
        return answer

    def effective_question(self, text: str) -> str:
        if (
            self.last_event_question
            and not likely_event_list_question(text)
            and _looks_like_event_followup(text)
        ):
            return f"{self.last_event_question}; follow-up constraint: {text}"
        return text

    def add_turn(
        self,
        *,
        user_text: str,
        effective_question: str,
        answer: str,
        sql_queries: list[str],
    ) -> None:
        self.recent_turns.append(
            ConversationTurn(
                user_text=user_text.strip(),
                effective_question=effective_question.strip(),
                answer_summary=_answer_summary(answer),
                sql_queries=[_compact_sql(sql) for sql in sql_queries if sql.strip()][:3],
            )
        )
        self.recent_turns = self.recent_turns[-4:]

    def context_block(self, *, max_chars: int = 1400) -> str:
        if not self.recent_turns:
            return ""
        lines = ["Context of previous queries:"]
        for index, turn in enumerate(self.recent_turns[-4:], start=1):
            lines.append(f"{index}. User asked: {turn.user_text}")
            if turn.effective_question != turn.user_text:
                lines.append(f"   Effective query: {turn.effective_question}")
            if turn.sql_queries:
                lines.append(f"   ClickHouse SQL: {turn.sql_queries[-1]}")
            if turn.answer_summary:
                lines.append(f"   Result: {turn.answer_summary}")
        block = "\n".join(lines)
        if len(block) <= max_chars:
            return block
        return block[-max_chars:].lstrip()


EVENT_ROW_ANSWER_PATTERN = re.compile(
    r"(?m)^\*\*.+?\*\*\s+—\s+\d{4}-\d{2}-\d{2},"
)


def _event_followup_question(text: str, answer: str) -> str | None:
    if likely_event_list_question(text):
        return text
    if _looks_like_pageable_event_answer(answer) or _looks_like_event_row_answer(answer):
        return f"list events matching {text}"
    return None


def _looks_like_pageable_event_answer(answer: str) -> bool:
    return "More results are available. Send `more`" in answer


def _looks_like_event_row_answer(answer: str) -> bool:
    return bool(EVENT_ROW_ANSWER_PATTERN.search(answer))


EVENT_FOLLOWUP_PATTERN = re.compile(
    r"\b("
    r"what about|how about|only|just|instead|same|there|nearby|"
    r"open|rsvp|rsvps|available|capacity|tomorrow|today|tonight|"
    r"morning|afternoon|evening|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|soho|midtown|brooklyn|cambridge|seaport"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_event_followup(text: str) -> bool:
    return bool(EVENT_FOLLOWUP_PATTERN.search(text))


def _answer_summary(answer: str, *, max_chars: int = 220) -> str:
    text = re.sub(r"https?://\S+", "", answer)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _compact_sql(sql: str, *, max_chars: int = 420) -> str:
    text = re.sub(r"\s+", " ", sql).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."
