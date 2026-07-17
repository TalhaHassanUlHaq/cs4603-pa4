"""Synthesizer node (Task 1.6)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.prompts import SYNTHESIZER_PROMPT
from agent.state import AnalystState, extract_question


def _format_step_results(plan: list[str], step_results: list[str]) -> str:
    lines = []
    for i, result in enumerate(step_results):
        step_text = plan[i] if i < len(plan) else f"step {i + 1}"
        lines.append(f"{i + 1}. {step_text}\n   Result: {result}")
    return "\n".join(lines)


def make_synthesizer(llm):
    def synthesizer(state: AnalystState) -> dict:
        question = extract_question(state)
        plan = state.get("plan", [])
        step_results = state.get("step_results", [])
        context = _format_step_results(plan, step_results)

        try:
            response = llm.invoke(
                [
                    SystemMessage(content=SYNTHESIZER_PROMPT),
                    HumanMessage(
                        content=f"Original question: {question}\n\nStep results:\n{context}"
                    ),
                ]
            )
            answer = getattr(response, "content", str(response)).strip()
        except Exception as exc:
            answer = (
                f"I gathered the following results but could not synthesize a final "
                f"answer due to an error ({exc}):\n{context}"
            )

        if not answer:
            answer = context or "No results were produced for this query."

        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}

    return synthesizer
