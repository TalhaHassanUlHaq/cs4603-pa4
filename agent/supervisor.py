"""Supervisor node + routing edge (Task 1.3)."""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.prompts import SUPERVISOR_PROMPT
from agent.state import AnalystState

RAG = "rag_agent"
MCP = "mcp_tools"
SYNTH = "synthesizer"

_MATH_KEYWORDS = (
    "calculate", "calculation", "compute", "percentage", "percent", "growth",
    "compare", "comparison", "convert", "conversion", "%", "rate", "cagr",
    "ratio", "multiply", "divide", "sum", "total of", "projected", "increase by",
    "decrease by",
)


class RouteDecision(BaseModel):
    """Routing decision for a single plan step."""

    agent: Literal["rag_agent", "mcp_tools"] = Field(
        description="'rag_agent' if the step needs a document fact, "
        "'mcp_tools' if the step needs a calculation."
    )


def _keyword_route(step: str) -> str:
    lowered = step.lower()
    return MCP if any(kw in lowered for kw in _MATH_KEYWORDS) else RAG


def make_supervisor(llm):
    router = None
    if hasattr(llm, "with_structured_output"):
        try:
            router = llm.with_structured_output(RouteDecision, method="function_calling")
        except TypeError:
            router = llm.with_structured_output(RouteDecision)

    def supervisor(state: AnalystState) -> dict:
        plan = state.get("plan") or []
        idx = state.get("current_step_index", 0)

        if idx >= len(plan):
            return {"next_agent": SYNTH}

        step = plan[idx]
        next_agent = None

        if router is not None:
            try:
                decision = router.invoke(
                    [
                        SystemMessage(content=SUPERVISOR_PROMPT),
                        HumanMessage(content=f"Step {idx + 1}/{len(plan)}: {step}"),
                    ]
                )
                next_agent = decision.agent
            except Exception:
                next_agent = None

        if next_agent is None:
            try:
                response = llm.invoke(
                    [SystemMessage(content=SUPERVISOR_PROMPT), HumanMessage(content=step)]
                )
                text = getattr(response, "content", str(response)).lower()
                if "mcp" in text or "tool" in text or "calc" in text:
                    next_agent = MCP
                elif "rag" in text or "doc" in text:
                    next_agent = RAG
            except Exception:
                next_agent = None

        if next_agent is None:
            next_agent = _keyword_route(step)

        return {"next_agent": next_agent}

    return supervisor


def route_from_supervisor(state: AnalystState) -> str:
    return state["next_agent"]
