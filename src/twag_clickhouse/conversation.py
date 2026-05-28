from __future__ import annotations

from dataclasses import dataclass
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
        stream_callback: Callable[[str], None] | None = None,
        raw_stream_callback: Callable[[str], None] | None = None,
        token_usage_callback: TokenUsageCallback | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> str:
        ...


@dataclass
class AgentConversation:
    last_event_question: str | None = None
    last_event_offset: int = 0

    def answer(
        self,
        agent: AgentLike,
        text: str,
        *,
        stream_callback: Callable[[str], None] | None = None,
        raw_stream_callback: Callable[[str], None] | None = None,
        token_usage_callback: TokenUsageCallback | None = None,
        progress_callback: Callable[[str], None] | None = None,
        no_previous_more_message: str = "Ask an event-list question first, then type 'more'.",
    ) -> str:
        if is_more_results_request(text):
            if not self.last_event_question:
                return no_previous_more_message
            self.last_event_offset += requested_event_limit(self.last_event_question)
            return agent.ask(
                self.last_event_question,
                event_offset=self.last_event_offset,
                stream_callback=stream_callback,
                raw_stream_callback=raw_stream_callback,
                token_usage_callback=token_usage_callback,
                progress_callback=progress_callback,
            )

        answer = agent.ask(
            text,
            stream_callback=stream_callback,
            raw_stream_callback=raw_stream_callback,
            token_usage_callback=token_usage_callback,
            progress_callback=progress_callback,
        )
        followup_question = _event_followup_question(text, answer)
        if followup_question:
            self.last_event_question = followup_question
            self.last_event_offset = 0
        return answer


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
