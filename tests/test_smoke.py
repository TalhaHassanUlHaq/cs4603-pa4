"""Offline smoke test for the Document Analyst graph (Bonus A test target).

Builds the graph with fake LLM / retriever / tool objects -- no Databricks, no
network -- and proves the wiring itself is correct: a plan gets produced, both
specialist branches (RAG and MCP) run for a combined query, and the final answer
lands on `messages[-1]` (the channel the deployed OpenAI-compatible endpoint reads).

Run:  uv run pytest -q
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from agent.prompts import (  # noqa: E402
    PLANNER_PROMPT,
    RAG_EXTRACT_PROMPT,
    SUPERVISOR_PROMPT,
    SYNTHESIZER_PROMPT,
)


def test_graph_module_imports():
    """Minimal collection guard: the graph module must import cleanly."""
    from agent.graph import build_graph  # noqa: F401


class _FakeDoc:
    def __init__(self, content: str, source: str, page: int):
        self.page_content = content
        self.metadata = {"source": source, "page": page}


class FakeRetriever:
    """Always returns one canned chunk, regardless of query."""

    def invoke(self, query: str):
        return [
            _FakeDoc(
                "Meridian's net revenue in FY2023 was ¥16.91 trillion.",
                "annual_report.pdf",
                4,
            )
        ]


class _FakeToolCallingLLM:
    """Returned by FakeLLM.bind_tools(); always calls the first bound tool."""

    def __init__(self, tools):
        self._tool_name = tools[0].name if tools else None

    def invoke(self, messages):
        if self._tool_name is None:
            return AIMessage(content="No tools available.")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self._tool_name,
                    "args": {"expression": "16.91 * (1.08 ** 3)"},
                    "id": "call_1",
                }
            ],
        )


class FakeLLM:
    """Deterministic stand-in for a chat model. No with_structured_output, so the
    supervisor exercises its keyword-fallback tiers (exactly what we want offline)."""

    def invoke(self, messages):
        system = messages[0].content if messages else ""
        if system == PLANNER_PROMPT:
            steps = [
                "Find Meridian's net revenue for fiscal year 2023",
                "Calculate compound growth: revenue x (1.08)^3",
            ]
            return AIMessage(content=json.dumps(steps))
        if system == RAG_EXTRACT_PROMPT:
            return AIMessage(
                content="Meridian's net revenue in FY2023 was ¥16.91 trillion "
                "[source: annual_report.pdf, p.4]"
            )
        if system == SUPERVISOR_PROMPT:
            return AIMessage(content="")  # empty -> forces the keyword-heuristic tier
        if system == SYNTHESIZER_PROMPT:
            return AIMessage(
                content="Net revenue was ¥16.91 trillion in FY2023; at 8% CAGR "
                "over 3 years it would grow to approximately ¥21.30 trillion."
            )
        return AIMessage(content="")

    def bind_tools(self, tools):
        return _FakeToolCallingLLM(tools)


@tool
def fake_calculate(expression: str) -> str:
    """Evaluate a math expression (fake, deterministic result for tests)."""
    return "16.91 * (1.08 ** 3) = 21.2977"


def test_combined_query_runs_both_specialists_and_produces_final_answer():
    from agent.graph import build_graph

    graph = build_graph(llm=FakeLLM(), retriever=FakeRetriever(), tools=[fake_calculate])

    result = graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content="What was Meridian's net revenue in FY2023, and what "
                    "would it be after 3 years of 8% compound annual growth?"
                )
            ]
        }
    )

    assert len(result["plan"]) == 2
    assert result["current_step_index"] == 2

    step_results = result["step_results"]
    assert len(step_results) == 2
    assert "16.91" in step_results[0]  # rag_agent ran
    assert "fake_calculate" in step_results[1]  # mcp_tools ran

    assert result["final_answer"]
    final_message = result["messages"][-1]
    assert final_message.content == result["final_answer"]


def test_retrieval_only_query_produces_single_step_plan_fallback_or_short_plan():
    from agent.graph import build_graph

    class _SingleStepLLM(FakeLLM):
        def invoke(self, messages):
            system = messages[0].content if messages else ""
            if system == PLANNER_PROMPT:
                return AIMessage(content=json.dumps(["Find the net income in 2023"]))
            return super().invoke(messages)

    graph = build_graph(llm=_SingleStepLLM(), retriever=FakeRetriever(), tools=[fake_calculate])
    result = graph.invoke(
        {"messages": [HumanMessage(content="What was the net income in 2023?")]}
    )

    assert result["plan"] == ["Find the net income in 2023"]
    assert len(result["step_results"]) == 1
    assert result["messages"][-1].content == result["final_answer"]
