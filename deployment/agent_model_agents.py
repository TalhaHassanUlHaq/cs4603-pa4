"""MLflow models-from-code definition for Bonus B (`agents.deploy()`).

Why this file exists separately from `deployment/agent_model.py`: the
`databricks-agents` SDK's `agents.deploy()` enforces that a logged model's
input/output schema conform to its Agent Framework contract (`ChatCompletionRequest`
in, `ChatCompletionResponse`/`StringResponse`/a bare string out) --
`databricks.agents.utils.mlflow_utils._check_model_is_rag_compatible` raises
`ValueError: The model's schema is not compatible with Agent Framework` otherwise.
`deployment/agent_model.py` serves the raw compiled `StateGraph`, whose invoke()
output is the full `AnalystState` dict (`messages`, `plan`, `step_results`,
`next_agent`, `final_answer`) -- none of which matches that contract, confirmed by
actually running `agents.deploy()` against it (see STUDENT_ANALYSIS.md).

This wrapper reuses the exact same graph construction as Part 2, but exposes it as
a `RunnableLambda` that returns just the final answer as a bare string -- MLflow
infers a plain-string output schema from that, which
`_check_model_is_rag_compatible_legacy_signatures` explicitly accepts
(`output_properties == {"type": "string", "required": True}`).
"""

from __future__ import annotations

import mlflow
from langchain_core.runnables import RunnableLambda

from agent.graph import build_graph, load_mcp_tools
from config import get_chat_llm, get_settings
from rag.store import get_retriever

# Validate required env vars up front (same reasoning as agent_model.py).
get_settings()

_graph = build_graph(llm=get_chat_llm(), retriever=get_retriever(), tools=load_mcp_tools())


def _invoke(request) -> str:
    """Accept either shape MLflow may hand us.

    Confirmed live: `mlflow.langchain`'s scoring path auto-detects the
    `{"messages": [...]}` "ChatCompletionRequest" shape and, for a plain
    `Runnable` (unlike the raw `CompiledStateGraph` `agent_model.py` serves),
    unwraps it to a bare list of `BaseMessage` objects before calling
    `.invoke()` -- so `request` arrives as `[HumanMessage(...), ...]` in the
    real deployed/served path, not the wrapping dict our own local calls (and
    `INPUT_EXAMPLE`) use. Handle both so this works identically either way.
    """
    messages = request["messages"] if isinstance(request, dict) else request
    result = _graph.invoke({"messages": messages})
    return result["messages"][-1].content


chat_model = RunnableLambda(_invoke)

mlflow.models.set_model(chat_model)
