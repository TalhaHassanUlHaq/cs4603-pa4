"""Planner node (Task 1.2)."""

from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from agent.prompts import PLANNER_PROMPT
from agent.state import AnalystState, extract_question

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

_MAX_STEPS = 5


def _parse_plan(text: str, question: str) -> list[str]:
    candidates = []
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    array_match = _ARRAY_RE.search(text)
    if array_match:
        candidates.append(array_match.group(0).strip())
    candidates.append(text.strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get("steps") or parsed.get("plan")
        if isinstance(parsed, list):
            steps = [str(s).strip() for s in parsed if str(s).strip()]
            if steps:
                return steps[:_MAX_STEPS]

    return [question] if question else [text.strip() or "Answer the user's question."]


def make_planner(llm):
    def planner(state: AnalystState) -> dict:
        question = extract_question(state)
        try:
            response = llm.invoke(
                [SystemMessage(content=PLANNER_PROMPT), HumanMessage(content=question)]
            )
            text = getattr(response, "content", str(response))
        except Exception:
            text = ""

        plan = _parse_plan(text, question)
        return {"plan": plan, "current_step_index": 0, "step_results": []}

    return planner
