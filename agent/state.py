"""State schema for the Document Analyst graph (Task 1.1)."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AnalystState(TypedDict):
    messages: Annotated[list, add_messages]
    plan: list[str]
    current_step_index: int
    step_results: list[str]
    next_agent: str
    final_answer: str


def extract_question(state: AnalystState) -> str:
    """Pull the most recent human/user message's text out of `state["messages"]`."""
    messages = state.get("messages") or []
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if content and role in ("human", "user", None):
            return str(content)
    return ""
