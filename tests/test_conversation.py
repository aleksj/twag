from __future__ import annotations

from twag_clickhouse.conversation import AgentConversation


class RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def ask(self, question, *, event_offset=0, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((question, event_offset))
        return f"{question} @ {event_offset}"


def test_conversation_tracks_event_list_for_more() -> None:
    conversation = AgentConversation()
    agent = RecordingAgent()

    assert conversation.answer(agent, "list events involving running") == (
        "list events involving running @ 0"
    )
    assert conversation.answer(agent, "more") == "list events involving running @ 25"
    assert conversation.answer(agent, "next") == "list events involving running @ 50"

    assert agent.calls == [
        ("list events involving running", 0),
        ("list events involving running", 25),
        ("list events involving running", 50),
    ]


def test_conversation_handles_more_without_previous_event_query() -> None:
    conversation = AgentConversation()
    agent = RecordingAgent()

    assert conversation.answer(agent, "more") == (
        "Ask an event-list question first, then type 'more'."
    )
    assert agent.calls == []


def test_conversation_resets_offset_for_new_event_query() -> None:
    conversation = AgentConversation()
    agent = RecordingAgent()

    conversation.answer(agent, "list events involving running")
    conversation.answer(agent, "more")
    conversation.answer(agent, "top 3 AI events")

    assert conversation.answer(agent, "more") == "top 3 AI events @ 3"
