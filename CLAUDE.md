# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

CS 4603 PA4 — a LangGraph multi-agent **Document Analyst** system deployed on Databricks. It decomposes natural language queries about an annual report into steps, routes them to a RAG retrieval agent or MCP math-tool agent, then synthesizes a final cited answer. The system is packaged as an MLflow models-from-code endpoint on Databricks Model Serving.

## Commands

```bash
# Install dependencies
uv sync

# Run all offline tests (no Databricks credentials needed)
uv run pytest -q

# Run a single test
uv run pytest tests/test_smoke.py::test_combined_query_runs_both_specialists_and_produces_final_answer -v

# Lint
uv run ruff check agent client

# Ingest corpus + create Vector Search index (run inside Databricks notebook)
# from rag.ingest import build_chunks_table, create_index
# build_chunks_table(spark, "/Volumes/main/default/pa4/annual_report.pdf", "main.default.<name>_analyst_chunks")
# create_index()

# Deploy to Databricks
uv run python deployment/deploy.py

# Bonus B: deploy via databricks-agents SDK
uv run python deployment/deploy_agents.py
```

## Environment Setup

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
|---|---|
| `DATABRICKS_HOST` / `DATABRICKS_TOKEN` | Workspace auth (LLM, VS, deployment) |
| `DATABRICKS_MODEL` | Served LLM endpoint name (e.g. `databricks-meta-llama-3-3-70b-instruct`) |
| `EMBEDDINGS_ENDPOINT` | Managed embeddings for Vector Search |
| `UC_CATALOG` / `UC_SCHEMA` | Unity Catalog location for tables/index |
| `VECTOR_SEARCH_ENDPOINT` / `VECTOR_SEARCH_INDEX` | VS endpoint + index names |
| `SERVING_ENDPOINT_NAME` | Name for the deployed model serving endpoint |
| `SECRET_SCOPE` | Databricks secrets scope (default `cs4603-deploy`) |
| `MCP_SERVER_URL` | Leave empty for stdio transport (Parts 1–2); set to HTTP URL for Bonus C |

For deployment, secrets must also be stored in Databricks:
```bash
databricks secrets create-scope cs4603-deploy
databricks secrets put-secret cs4603-deploy DATABRICKS_TOKEN --string-value "dapi..."
```

## Architecture

### Agent Execution Flow

```
User query
  └─ PLANNER (agent/planner.py)
       Decomposes query into 2–5 atomic steps (JSON list)
  └─ SUPERVISOR (agent/supervisor.py) — called once per step
       Routes each step to: "rag_agent" | "mcp_tools" | "synthesizer"
       Routing priority: structured output → keyword parse → text heuristic
  └─ RAG_AGENT (agent/rag_agent.py)          ← for document lookup steps
       DatabricksVectorSearch retriever → LLM extracts cited fact
  └─ MCP_TOOLS (agent/graph.py::make_mcp_node) ← for calculation steps
       LLM generates tool call → MCP stdio subprocess (tools/mcp_server.py)
  └─ SYNTHESIZER (agent/synthesizer.py)       ← after all steps complete
       Combines step_results → AIMessage appended to messages channel
```

The graph is built in `agent/graph.py::build_graph()` using `StateGraph(AnalystState)`.

### State (agent/state.py)

`AnalystState` TypedDict carries: `messages`, `plan` (list of step strings), `step_results` (list of answers), `current_step` (int), `next_agent` (str), `final_answer` (str).

The `messages` channel is the OpenAI-compatible interface — the deployed endpoint reads `messages[-1]`. The synthesizer **must** append an `AIMessage` to `messages`, not just set `final_answer`.

### Key Module Relationships

- `config.py` — loads `.env`, exposes `get_settings()`, `get_chat_llm()`, `get_embeddings()`; used by all agent and deployment code
- `agent/prompts.py` — all system prompts (no logic); imported by planner, supervisor, rag_agent, synthesizer
- `rag/store.py` — `get_retriever(k=4)` wraps `DatabricksVectorSearch`; same code runs locally and in the serving container
- `deployment/agent_model.py` — MLflow models-from-code entry point; imports all agent/rag/tools code; `mlflow.models.set_model(graph)`
- `deployment/deploy.py` — `log_and_register()` + `create_or_update_endpoint()`; uses `code_paths` to bundle `agent/`, `rag/`, `tools/`, `config.py` into the MLflow artifact

### MCP Integration

`tools/mcp_server.py` is a **given** stdio MCP server with tools: `calculate`, `percentage_change`, `growth_rate`, `compare_values`, `unit_convert`.

`agent/graph.py` uses `langchain-mcp-adapters` (`MultiServerMCPClient`) to bridge MCP tools to LangChain. A dedicated `_MCPLoopThread` runs a background asyncio event loop to avoid nested-event-loop issues on Windows and in Jupyter.

### Deployment

`deployment/deploy.py` logs the model via `mlflow.langchain.log_model(lc_model="deployment/agent_model.py", code_paths=[...])`, registers it in Unity Catalog, then calls `WorkspaceClient().serving_endpoints.create_or_update(...)` with `environment_vars` referencing `{{secrets/cs4603-deploy/VAR}}`. First-time endpoint creation takes 20–40 minutes for container build.

## Tests

- **`tests/test_smoke.py`** — offline graph test with `FakeLLM`, `FakeRetriever`, and fake MCP tools. No credentials required.
- **`tests/test_client_sdk.py`** — client SDK tests using `httpx.MockTransport`. Tests retry/backoff (429, 503), timeout wrapping, streaming SSE, and error wrapping.

## Files You Must Write (Student Tasks)

All files in `agent/`, `rag/`, `client/`, `deployment/`, `tests/`, and `.github/workflows/` are student-authored. Do not modify:
- `config.py`, `pyproject.toml`, `.env.example`, `tools/mcp_server.py`, `tools/__init__.py`
- `data/annual_report.pdf`, `data/annual_report.md`, `data/generate_report.py`
- `README.md`, `DEPLOYMENT_GUIDE.md`, `GITHUB_PIPELINE.md`

## CI/CD (Bonus A)

`.github/workflows/deploy.yml` runs on push to `main`:
1. `ruff check agent client` + `pytest -q`
2. On `main` only: `python deployment/deploy.py`

Requires GitHub Secrets: `DATABRICKS_HOST`, `DATABRICKS_TOKEN`; and GitHub Variables for the remaining env vars.

## Common Pitfalls

- **Missing `code_paths` in MLflow log**: container fails with `ModuleNotFoundError` at startup
- **Synthesizer not appending to `messages`**: deployed endpoint returns empty completion (OpenAI reads `messages[-1]`)
- **Vector Search index not READY**: retrieval returns empty silently; check index state before deploying
- **MCP async on Windows/Jupyter**: must use the `_MCPLoopThread` pattern in `graph.py` to avoid nested event loops
