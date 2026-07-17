"""MLflow models-from-code definition (Task 2.1).

Self-contained so MLflow's models-from-code loader can serialize + later re-execute
this exact file inside the serving container. It imports the modular Part 1 code
(agent/, rag/, config.py, tools/) -- those must be shipped via `code_paths` in
deployment/deploy.py, or the container fails at startup with
`ModuleNotFoundError: No module named 'agent'` (see DEPLOYMENT_GUIDE.md).

Must import cleanly given a valid .env / environment:
    python -c "import deployment.agent_model"
"""

from __future__ import annotations

import mlflow

from agent.graph import build_graph, load_mcp_tools
from config import get_chat_llm, get_settings
from rag.store import get_retriever

# ─── Validate required env vars up front ────────────────────────────────────
# A missing var here raises a clear message at import/log_model time, instead of a
# cryptic DEPLOYMENT_FAILED with no explanation in the serving logs.
get_settings()

# ─── Load MCP tools ──────────────────────────────────────────────────────────
# Deliberately no explicit server path here. load_mcp_tools()'s own default
# resolves tools/mcp_server.py relative to agent/graph.py's location, and `agent`
# + `tools` are always shipped as sibling code_paths entries -- so that path is
# correct regardless of how deep the packaged "code/" bundle is nested. This file
# (deployment/agent_model.py) is NOT itself part of code_paths -- MLflow's
# models-from-code loader copies just this one file to the model artifact root
# without preserving its original `deployment/` parent folder, so a path computed
# relative to *this* file's own location (the previous approach) resolves one
# directory too shallow inside both the serving container and MLflow's local
# input-example validation, which reproduces the same layout.
graph = build_graph(llm=get_chat_llm(), retriever=get_retriever(), tools=load_mcp_tools())

mlflow.models.set_model(graph)
