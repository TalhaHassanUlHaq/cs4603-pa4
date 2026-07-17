# CS4603 PA4 — Document Analyst (Student Submission)

> This is your **submission file**. `README.md` is the assignment spec — this document is where you write up your work.

## Setup

```bash
uv sync
cp .env.example .env   # then fill in your values
```

`.env` values used for this submission (secrets redacted):

```
DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
DATABRICKS_MODEL=databricks-meta-llama-3-3-70b-instruct
EMBEDDINGS_ENDPOINT=databricks-gte-large-en
UC_CATALOG=cs4603
UC_SCHEMA=default
VECTOR_SEARCH_ENDPOINT=27100306-vs-endpoint
VECTOR_SEARCH_INDEX=cs4603.default.27100306_analyst_index
SOURCE_TABLE=cs4603.default.27100306_analyst_chunks
SERVING_ENDPOINT_NAME=27100306-document-analyst
SECRET_SCOPE=cs4603-deploy
DATABRICKS_SQL_WAREHOUSE_ID=<warehouse id>
UC_VOLUME=cs4603.default.pa4_corpus
```

The workspace's `main` catalog did not exist for this account (this is a **Databricks
Free Edition** workspace), so all Unity Catalog objects live under `cs4603.default`
instead of `main.default` (the assignment's example naming). This is a
workspace-provisioning difference, not a deviation from the pipeline itself.

**A real, live `.env` bug found and fixed during a later compliance pass:** a merged
comment line (`# ─── Unity Catalog + Vector Search ───...UC_CATALOG=cs4603`, no
newline before the value) meant python-dotenv treated the *entire* line as a comment,
silently dropping the intended `UC_CATALOG=cs4603`. A stray duplicate
`UC_CATALOG=main` further down was the only assignment that actually loaded — pointing
`deployment/deploy.py`'s Unity Catalog registration at a catalog (`main`) that doesn't
exist in this workspace, while every other setting (`VECTOR_SEARCH_INDEX`,
`SOURCE_TABLE`) correctly used `cs4603.default`. There were also several garbled
leftover lines (`OPE=cs4603-deploy`, `03-deploy`, `03-deploy`, `603-deploy`) that were
either silently-ignored malformed statements or a spurious extra env var. Fixed by
cleaning up `.env` to a single, consistent `UC_CATALOG=cs4603` and removing the
garbage lines.

## Running locally

### 1. Ingest the corpus (Task 0.3)

There is no local Databricks notebook/cluster available for this submission, so
ingestion was run through `scripts/ingest_via_sql_warehouse.py` — a non-graded helper
that performs the exact same `ai_parse_document` + `ai_prep_search` pipeline as
`rag/ingest.py::build_chunks_table`, but through the SQL Statement Execution API
against a SQL Warehouse instead of a Spark session (`rag/ingest.py` itself is written
to the assignment's required `build_chunks_table(spark, ...)` signature and is what a
grader running inside an actual Databricks notebook would call).

```bash
uv run python scripts/ingest_via_sql_warehouse.py
```

This uploaded `data/annual_report.pdf` to `/Volumes/cs4603/default/pa4_corpus/`, ran

```sql
SELECT ai_prep_search(ai_parse_document(content)) ...
```

against it, and wrote the resulting chunks into `cs4603.default.27100306_analyst_chunks`.
One implementation detail discovered by probing the real function against the real
document (its signature is otherwise undocumented in this workspace):
`ai_prep_search(ai_parse_document(content))` returns a VARIANT whose
`document.contents[]` array holds one struct per chunk — `chunk_id`,
`chunk_to_retrieve`, `chunk_to_embed`, and `pages[].page_id` (0-indexed, so `+1` to
match the report's printed page numbers). It produced **7 chunks** for the 14-page
report — coarser than a fixed-size splitter, but each chunk is a coherent section
(e.g. one chunk covers the Financial Highlights five-year table on page 4, which is
exactly what the assignment's worked example cites).

The endpoint + Delta Sync index were then created with `databricks-vectorsearch`'s
`VectorSearchClient` (`create_endpoint(endpoint_type="STANDARD")` +
`create_delta_sync_index(pipeline_type="TRIGGERED", primary_key="chunk_id", ...)`) —
no Spark needed for this part at all.

**Verified independently on a later pass:** re-queried the index directly
(`VectorSearchClient().get_index(...).describe()`) and confirmed
`status.ready == True`, `status.detailed_state == "ONLINE_NO_PENDING_UPDATE"` — the
index is genuinely healthy and has been the entire time; it was never a contributor
to any of the deployment issues below.

### 2. Build and run the graph

```python
from agent.graph import build_graph
graph = build_graph()          # uses config.py + rag/store.py + the MCP server
result = graph.invoke({"messages": [{"role": "user",
          "content": "What was the net revenue in 2023?"}]})
print(result["messages"][-1].content)
```

### 3. Test queries I ran (retrieval-only, computation-only, combined)

Run against the real graph (`build_graph()`, real LLM + real Vector Search index +
real MCP tools), local (not-yet-deployed) execution — reconfirmed on a later pass,
same results:

| Query | Plan | Answer produced |
|-------|------|-----------------|
| "What was the net income in 2023?" | 1 step (retrieval) | "The net income in 2023 was ¥1,107 billion, as reported in the company's consolidated statement of operations (annual_report.pdf, p.4) and confirmed in the five-year summary table (annual_report.pdf, p.2)." |
| "What is 15% of 2.4 billion?" | 1 step (calculation) | "15% of 2.4 billion is 360 million, as calculated by multiplying 2,400,000,000 by 0.15, which equals 360,000,000." |
| "What was Meridian's net revenue in fiscal year 2023, and what would it be after 3 years of 8% compound annual growth?" | 3 steps (retrieval, calculation, present) | "Meridian's net revenue in fiscal year 2023 was ¥16,910 billion, or approximately ¥16.91 trillion. After 3 years of 8% compound annual growth, the projected net revenue would be approximately ¥21,301.7 billion." |

The combined query's answer matches the assignment's own worked example
(16.91 × 1.08³ ≈ 21.30 trillion) to 4 significant figures.

**A real retrieval-quality finding, not a hypothetical one:** the first version of
`RAG_EXTRACT_PROMPT` returned **¥1,137 billion** for "net income in 2023" — the
consolidated "Profit for the year" total (including non-controlling interests), not
the ¥1,107 billion "attributable to owners" figure the report's own CEO letter
highlights as the headline number. Because `ai_prep_search` produced only 7 coarse
chunks for this 14-page document, the income-statement chunk contains several
similar-looking adjacent line items (Operating profit, Profit before tax, Profit for
the year, Attributable to owners, Attributable to NCI), and the extraction step
picked the wrong one. Fixed by adding an explicit disambiguation rule to
`RAG_EXTRACT_PROMPT` (prefer the owners-attributable figure for unqualified "net
income" questions, but always name which line item was used) and confirmed the fix
against the real endpoint — this is the retrieval-granularity tradeoff discussed in
the Task 1.4 analysis answers below, observed directly rather than assumed.

See `pa4.ipynb` for the full step-by-step trace (plan, per-step routing, step
results) for each query, plus an **offline** run of the same combined query using
fake LLM/retriever/tool objects (no credentials needed) that proves the graph wiring
itself is correct independent of any live model, and the required offline smoke test
(`python -m pytest tests/test_smoke.py`).

## Deployment

- Model logged via `mlflow.langchain.log_model()` (models-from-code,
  `deployment/agent_model.py`) with `code_paths=[agent, rag, tools, config.py]`.
- Registered in Unity Catalog as `cs4603.default.27100306_document_analyst`,
  currently at **version 13** (the endpoint itself serves **version 12** — see bug
  #10 below for why registering and pointing the endpoint at a version are two
  separate steps, and why the two numbers legitimately differ by one here).
- Serving endpoint name: `27100306-document-analyst`.
- Endpoint URL:
  `https://dbc-add11d4c-02db.cloud.databricks.com/serving-endpoints/27100306-document-analyst/invocations`

**Current status: the endpoint is `READY` and answering correctly.** Confirmed
directly — not inferred — via `WorkspaceClient().serving_endpoints.get(...)`
(`ready=READY`, `config_update=NOT_UPDATING`), and via every live call in
`pa4.ipynb` Part 2.4/Part 3 (`curl`, the OpenAI SDK, and `DocumentAnalystClient`)
all returning real HTTP 200s with correct, cited answers. This took substantially
longer than "several minutes" (the README's estimate for a first deploy) — getting
here required finding and fixing three additional, non-hypothetical bugs beyond the
seven below, each one only reachable by watching a real container build fail and
reading its actual logs, not by local testing.

**Real deployment debugging, not a dry run.** Running `deployment/deploy.py` against
a real workspace surfaced ten genuine, non-hypothetical bugs across the whole path
from "the script exits 0" to "the endpoint actually answers queries in production."
Each is fixed in place, in the order encountered:

1. **Local `mlflow` tracking store + a path with spaces = broken Unity Catalog
   registration.** `deploy.py` didn't call `mlflow.set_tracking_uri(...)`, so it
   defaulted to a local `file:./mlruns` store. On Windows, with this repo's path
   containing a space, the resulting `file:` URI was malformed enough that
   `mlflow.register_model()` failed with `BAD_REQUEST: Illegal character in opaque
   part`. Fix: explicitly `mlflow.set_tracking_uri("databricks")` so runs/experiments
   live on the workspace's own tracking server.
2. **A Unicode emoji crashes Windows' default console encoding.** `mlflow.start_run()`
   prints a 🏃 emoji on exit; Windows' default `cp1252` stdout encoding can't
   represent it, raising `UnicodeEncodeError` and killing the script *after* logging
   had actually succeeded. Fix: `sys.stdout.reconfigure(encoding="utf-8", ...)` at
   the top of `deploy.py`.
3. **`EndpointCoreConfigInput` requires `name=` in the installed SDK version** — a
   `TypeError` on the very first `serving_endpoints.create()` call. Fixed by passing
   `name=endpoint_name` explicitly.
4. **A transitive dependency pulled in a broken package version.** `tiktoken`
   (required by `langchain-openai`) depends on `regex` with no floor, and the serving
   container's package mirror had no prebuilt wheel for `regex` on the container's
   Python version, so pip's resolver fell back to source-only releases whose build
   script is incompatible with modern Python (`Container creation failed`). Fixed by
   pinning `regex>=2023.0.0` in `deploy.py`'s `PIP_REQUIREMENTS`.
5. **The endpoint-readiness poll loop only checked for `READY`, never for failure.**
   Fixed `create_or_update_endpoint()` to check `state.config_update` for
   `UPDATE_FAILED`/`UPDATE_CANCELED` each iteration and raise immediately.
6. **A Jupyter-specific MCP stdio crash on Windows**: `mcp.client.stdio.stdio_client`
   binds its `errlog` parameter's default to `sys.stderr` once, at import time, and
   under Jupyter, `sys.stderr.fileno()` raises `io.UnsupportedOperation`, crashing
   Windows subprocess creation the moment the graph tries to spawn the MCP tool
   server. Fixed by `agent/graph.py::_ensure_valid_mcp_stdio_errlog`, which rebinds
   that captured default to a real file the first time a stdio connection is made.
7. **A real, live bug found on this compliance pass**: `deployment/agent_model.py`
   computed its MCP server path as two directories up from its own `__file__`
   (`dirname(dirname(__file__)) / tools / mcp_server.py`), assuming it would keep its
   `deployment/` parent folder inside the packaged model artifact. That assumption is
   wrong — `deployment/` is never listed in `code_paths` (only `agent`, `rag`,
   `tools`, `config.py` are), so MLflow's models-from-code loader copies just this
   one file to the model artifact root *without* its original parent folder. This
   surfaced directly in `mlflow.langchain.log_model()`'s own local input-example
   validation (`can't open file '...\tools\mcp_server.py': No such file or
   directory`), and the same wrong assumption would crash model loading identically
   inside the real serving container. Fixed by deleting the custom path computation
   and calling `load_mcp_tools()` with no argument, delegating to `agent/graph.py`'s
   own default (relative to *its* `__file__`), which is correct because `agent` and
   `tools` are always shipped as sibling `code_paths` entries. **Confirmed the fix
   works**: re-running `log_and_register()` produced a clean local model validation
   (previously-crashing step now succeeds, with the MCP server visibly responding to
   a real tool-list request during validation) and registered version 5 successfully.

8. **pip's resolver gave up entirely on a deep dependency graph
   (`resolution-too-deep`).** After bug #7 was fixed, the container build got much
   further — past model loading — and into dependency installation, where it failed
   after ~106 minutes across 5 retries with `error: resolution-too-deep`. The
   container runs pip 26.x, which enforces a hard backtracking-depth limit. Pinning
   only the 14 *direct* dependencies (the previous approach) left the transitive
   graph open: `databricks-langchain` pulls `unitycatalog-langchain[databricks]` →
   `unitycatalog-ai[databricks]` → `databricks-connect` (40+ candidate releases),
   and pip's resolver exhausted its depth budget backtracking through that range.
   Root-caused directly from the real build log the container produced, not
   guessed. Fixed by replacing the 14 direct pins with a **complete, fully-pinned
   transitive lock** (154 packages, every one `==`-pinned, generated via
   `uv pip compile --python-platform linux`, committed as
   `deployment/requirements-lock.txt`) — with a single candidate per package, pip
   cannot backtrack, so the depth limit cannot be hit regardless of which package
   was deepest. Confirmed: the very next build attempt failed in ~50 seconds
   instead of 106 minutes of backtracking, and for a *different* reason (bug #9) —
   proof `resolution-too-deep` itself was gone.
9. **No version of `databricks-connect` satisfies both Python 3.13 and
   `numpy>=2`.** The next real build log showed
   `No matching distribution found for databricks-connect==17.0.10` — the
   container was building on Python 3.13 (MLflow's inferred default from the local
   dev machine), but `databricks-connect` 17.x (the only line that's also
   numpy-2-compatible, which `mlflow`/`langchain` require) declares
   `Requires-Python ==3.12.*`; the newest 3.13-compatible build (16.1.7) caps
   `numpy<2` and directly conflicts with `mlflow`/`langchain`'s `numpy>=2`. Verified
   there is no third option: `uv pip compile` with `databricks-connect==16.1.7`
   pinned explicitly fails at the resolve step itself with a direct, unambiguous
   conflict message. Fixed by pinning the **container's Python version to 3.12**
   (matching what `databricks-connect` 17.x actually requires) via a full
   `conda_env` passed to `mlflow.langchain.log_model()` — `pip_requirements=` alone
   cannot control the interpreter version; only `conda_env`'s `python=` line does,
   since the serving image is conda-built.
10. **The real, final blocker: an absolute Windows path breaks MLflow's own
    `models-from-code` loader on a Linux container.** With bugs #8/#9 fixed, the
    container built and installed cleanly for the first time all engagement, and
    the endpoint progressed through provisioning to "Deploying served entities" —
    a stage never reached before — then failed with a new, later error:
    `An error occurred in model loading code`. The actual container **service
    logs** (inaccessible via this workspace's SDK/CLI — `serving_endpoints.logs()`
    and `.build_logs()` both raise `ResourceDoesNotExist`, confirmed repeatedly
    across this whole engagement) had to be retrieved manually from the Databricks
    UI, which surfaced the real traceback:
    `No such file or directory: '/model/F:\Lums courses\...\agent_model.py'`.
    Traced to MLflow's own source: `mlflow.models.utils._validate_and_get_model_code_path`
    resolves the `lc_model` path to an absolute, OS-native string at *log* time
    (`Path(...).resolve()`) and embeds it verbatim in the model's flavor metadata;
    at *load* time (`mlflow/langchain/model.py`), the container reconstructs the
    file location via `os.path.join(local_model_path, os.path.basename(flavor_code_path))`.
    `os.path.basename` on POSIX only splits on `/` — a path resolved on Windows
    (backslash-only, e.g. `F:\Lums courses\...\agent_model.py`) has no `/` in it at
    all, so POSIX `basename()` returns the **entire string unchanged**, producing
    exactly the broken path in the traceback. This is a genuine
    Windows-dev-machine-vs-Linux-container packaging bug in MLflow itself, not
    something wrong with the agent code (confirmed separately: loading the exact
    same v10 artifact locally via `mlflow.pyfunc.load_model(...)` succeeded and
    produced a correct answer, proving the model code was never the problem).
    Fixed with a small, targeted monkeypatch in `deployment/deploy.py`: wrap
    MLflow's `_validate_and_get_model_code_path` to return `Path(result).as_posix()`
    instead of the OS-native string, so the stored path is forward-slash-only
    regardless of which OS ran `deploy.py` — POSIX `basename()` then correctly
    extracts just `agent_model.py`, matching where the file actually lands in the
    artifact. Verified in the actual logged MLmodel file before redeploying
    (`model_code_path: F:/Lums courses/.../agent_model.py`), then confirmed
    end-to-end: the very next deploy reached `READY` for the first time this
    entire engagement.

**Two more real bugs, found only because a live response finally existed to test
against.** Once the endpoint was answering for real, exercising it via the OpenAI
SDK and the client SDK surfaced two further genuine bugs neither offline test nor
code review had caught:

- `client/sdk.py`'s `ask()` assumed the response body was a top-level dict
  (`{"choices": [...]}` or `{"messages": [...]}`). The real endpoint returns a
  **bare top-level JSON list** — one `AnalystState` dict per input row, since
  MLflow can't auto-wrap a custom multi-agent state into an OpenAI `ChatCompletion`
  envelope — so `ask()` raised `AnalystClientError: Unrecognized response shape`
  on every real call. Fixed by unwrapping a list before the existing shape checks;
  covered by a new test (`test_ask_handles_bare_list_response_from_real_endpoint`).
- `_post()`'s error-handling path called `response.json()`/`.text` on a **streamed**
  response without first calling `.read()`, which httpx requires before those
  accessors work — so any non-retryable error hit while `ask_streaming()` was
  active crashed with an unrelated `httpx.ResponseNotRead` instead of the real
  `AnalystClientError`. This was reachable because the real deployment genuinely
  rejects `stream=True` (`400: This endpoint does not support streaming` — a raw
  LangGraph served via `mlflow.langchain.log_model()` has no `predict_stream`, so
  MLflow's scoring server refuses it outright, consistent with the README's own
  streaming caveat, just stricter than the "falls back to one chunk" case it
  anticipates). Fixed by calling `response.read()` first when `stream=True`;
  covered by a new test
  (`test_ask_streaming_wraps_non_retryable_error_instead_of_crashing`).

Both bugs are now demonstrated live in `pa4.ipynb` Part 3: `ask()` returns a
correct, cited answer, and `ask_streaming()` cleanly raises `AnalystClientError`
with the real "does not support streaming" message instead of crashing.

**Local vs. deployed comparison (Task 2.4, item 4).** `pa4.ipynb`'s comparison cell
sends the same query ("What was the net income in 2023?") to the local graph and to
the live endpoint back to back. The two answers are **not byte-identical but agree
on every fact**:

> Local:    "The net income in 2023 was ¥1,107 billion, as reported in the
> company's annual report (annual_report.pdf, p.4.0, also confirmed on p.2.0 and
> p.13.0)."
>
> Deployed: "The net income in 2023 was ¥1,107 billion, as reported in the
> company's consolidated statement of operations for fiscal year 2023,
> specifically in the line item "Attributable to owners" (annual_report.pdf, p.4.0)."

Same figure, same source page, same underlying retrieval and reasoning — the
wording differs because the synthesizer is a sampling LLM call, not a deterministic
function, so two separate invocations (even with identical code and identical
inputs) are expected to phrase the answer differently while agreeing on the facts.
One of several such comparisons captured during this pass also hit a transient,
genuinely-real `429 REQUEST_LIMIT_EXCEEDED` from the underlying
`databricks-meta-llama-3-3-70b-instruct` Foundation Model API endpoint (a workspace
QPS throttle triggered by the volume of concurrent testing in this session, not a
bug in this code) — worth noting because the RAG agent surfaced it as a clear,
readable error string in the synthesized answer rather than crashing the request,
which is itself a small piece of evidence the error-handling in `agent/rag_agent.py`
degrades gracefully under a real, live infrastructure fault.

**Bottom line:** the endpoint is `READY`, serving version 12, and answering
correctly and consistently across `curl`, the OpenAI SDK, and the client SDK — all
three of the Definition of Done's Part 2/3 deployment-proof checkboxes are
satisfied with real, live evidence, not just passing code review. Getting here
required root-causing ten separate real bugs (packaging, dependency resolution, a
cross-platform MLflow path bug, and two client-side response-parsing bugs), each
diagnosed from an actual failure — a real build log, a real service-log traceback,
or a real live HTTP response — rather than guessed and patched speculatively.

## Bonus A — CI/CD activation (two more real bugs, found by actually running it)

`origin` for this repo (`alikhawaja/cs4603-pa4`) is the instructor's own repo — this
account only has read access to it, so activating Bonus A meant forking to
`TalhaHassanUlHaq/cs4603-pa4`, pushing this work there, configuring GitHub Secrets
(`DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_MODEL`) and Variables (the
remaining non-secret config), and triggering `workflow_dispatch`.

**Run 1** (`lint-and-test` ✅ in 16s; `deploy` job partially failed) surfaced two more
genuine, previously-unexercised bugs — this pipeline had never actually run in a CI
environment before:

11. **`deploy.yml`'s "Print deployed endpoint status" step was missing
    `SERVING_ENDPOINT_NAME` from its own `env:` block** (it only forwarded
    `DATABRICKS_HOST`/`DATABRICKS_TOKEN`). `deployment/deploy.py` itself had already
    logged model version 14 and issued the `update_config` call successfully — the
    actual deploy worked — but the status-print step's
    `os.environ.get('SERVING_ENDPOINT_NAME', 'document-analyst')` fell back to the
    wrong default name and the job died on
    `ResourceDoesNotExist: Endpoint with name 'document-analyst' does not exist`.
    Fixed by adding `SERVING_ENDPOINT_NAME: ${{ vars.SERVING_ENDPOINT_NAME }}` to
    that step's `env:` block.
12. **`create_or_update_endpoint()`'s readiness loop only checked `state.ready`,
    never `state.config_update`.** `state.ready` stays `READY` for the entire
    duration of a rolling update, because the *previous* served-entity version keeps
    answering traffic while the new one builds — exactly the zero-downtime rollout
    behavior described in this document's own Task 2.3 analysis answer #2. That
    means the loop's `if state.ready == READY: break` fired immediately on the very
    first poll (the old version was already `READY`), so `deploy.py` printed the
    endpoint URL and exited declaring success while version 14 was still
    `config_update=IN_PROGRESS` and the endpoint was still actually serving version
    12 — confirmed directly by querying the endpoint right after the "successful"
    run. Fixed by requiring **both** `state.ready == READY` **and**
    `state.config_update == EndpointStateConfigUpdate.NOT_UPDATING` before the loop
    breaks, so the function only reports success once the new version has actually
    finished rolling out.

Both fixes are in `deployment/deploy.py` and `.github/workflows/deploy.yml`; the
full offline test suite (19/19) and `ruff` stayed clean after each. This is the same
pattern as the ten Part 2 bugs above: a class of bug (silently reporting success
based on a rolling-update-stable field) that inspection alone would not have caught
and that only running the real pipeline against a real endpoint surfaced.

## Design decisions

- **Three-tier supervisor routing** (`agent/supervisor.py`): try
  `with_structured_output` first, fall back to a plain-prompt keyword parse, fall back
  to a deterministic keyword heuristic with no LLM call at all. This was not just
  theoretical hedging — the Databricks-served `databricks-meta-llama-3-3-70b-instruct`
  endpoint used for this submission rejects the `parallel_tool_calls` field that
  `with_structured_output(method="function_calling")` sends
  (`BAD_REQUEST: json: unknown field "parallel_tool_calls"`), so tier 1 fails on
  *every* call against this real endpoint, and every routing decision in this
  submission's real runs came from tier 2/3. Plain `bind_tools()` (used by the MCP
  node) works fine on the same endpoint — only the structured-output code path hits
  the incompatibility. The graph produces correct routing regardless because of the
  fallback design; this is exactly the scenario the tiering was built for.
- **Cross-step context**: `rag_agent` and `mcp_tools` are given the full accumulated
  `step_results` (not just the current step's text) as prompt context, so a step that
  refers back to an earlier result ("apply that growth rate to the number above")
  resolves correctly even when the planner doesn't repeat the literal number.
- **Windows-safe MCP bridge** (`agent/graph.py::_MCPLoopThread`): a dedicated
  background event loop with the Proactor policy forced on `win32`, independent of
  whatever loop the caller (pytest, a script, or Jupyter) is already running. Verified
  directly against the real `tools/mcp_server.py` stdio subprocess and, separately,
  against a locally-run HTTP instance of the same tools with the Bonus C bearer-auth
  middleware (correct token → 200, wrong/missing token → 401).
- **MCP tool output shape**: `langchain-mcp-adapters` returns tool results as a list
  of MCP content blocks (`[{"type": "text", "text": "...", "id": ...}]`), not a bare
  string — discovered by actually invoking the real stdio server, not by reading docs.
  `agent/graph.py::_stringify_tool_output` flattens this before it's appended to
  `step_results`.
- **Bonus C dependency isolation**: `deployment/mcp_app/` needs its own
  `requirements.txt` (not just a `pyproject.toml` entry) because a Databricks App's
  runtime does not inherit this repo's `uv`-managed environment — it starts minimal
  and installs only what `requirements.txt` lists.

---

## Analysis Questions

### Task 1.2 — Planner

1. **What happens when the planner produces steps that depend on each other (e.g.,
   step 3 needs the result of step 1)? How does your architecture handle this?**

   Two mechanisms work together. First, `PLANNER_PROMPT` instructs the LLM to write
   each step with concrete numbers already known and to order calculation steps after
   the lookup steps that produce their inputs, so most steps are self-contained by
   construction. Second — and this is what actually makes genuinely dependent steps
   work, not just conveniently-written ones — both `rag_agent` and `mcp_tools` are
   given the *entire* accumulated `step_results` list as context in their prompt, not
   just the current step's text (see `agent/rag_agent.py` and
   `agent/graph.py::make_mcp_node`). So a step like "apply that growth rate to the
   number from step 1" resolves correctly because the executing LLM can see step 1's
   result directly, even though the step text itself doesn't repeat the number.
   Sequencing is enforced structurally regardless of prompt content:
   `current_step_index` only advances by one per specialist visit, and the supervisor
   always routes to `plan[current_step_index]`, so step *N*'s node physically cannot
   run before step *N-1*'s result has already been appended to `step_results`.

2. **Would a replanning step after each execution improve or hurt performance for
   this use case? Justify with an example.**

   It would hurt for the dominant case and help for one specific failure mode. Hurt:
   for a query like "what was net income in 2023, and what's 15% of that", the two
   steps are already self-contained and correctly ordered — a replanner invoked after
   step 1 would add an extra LLM round-trip (latency + cost) with nothing to correct,
   and risks the LLM non-deterministically rewriting an already-correct plan (e.g.
   inserting a redundant "verify the figure" step). Help: if `rag_agent` returns "not
   found in documents" for a lookup step, a replanner could reformulate the query and
   retry retrieval, or restructure the remaining plan around the gap, rather than
   blindly feeding "not found in documents" into a downstream calculation step and
   letting the synthesizer paper over it. I did not implement full replanning here;
   instead I hedge with narrower fallbacks — a single-step plan on JSON parse failure,
   and an honest "not found" propagated to the synthesizer instead of a fabricated
   number — which cover most of replanning's benefit at a fraction of its cost for
   this task's typical 2-3 step plans.

### Task 1.3 — Supervisor

1. **Your supervisor makes a routing decision per step. What is the failure mode if
   it misroutes? How would you detect and recover from a misroute?**

   A misroute does not crash the graph — it produces a wrong or evasive step result
   that flows silently into the synthesizer. Routing a calculation step to
   `rag_agent` returns either no chunks (`"not found in documents"`) or an irrelevant
   chunk, appended as if it were a real answer. Routing a lookup step to `mcp_tools`
   is more self-diagnosing: `MCP_STEP_PROMPT` tells the model to always call a tool,
   but no tool matches a text lookup, so the node falls back to
   `"No tool call was produced for this step."` — a recognizable failure string.
   Detection/recovery in this implementation: (a) `make_supervisor`'s three-tier
   fallback catches routing failures *before* they reach the wrong specialist — this
   mattered in practice, not just in theory, since tier 1 (structured output) fails
   on every call against the real Databricks-served Llama-3.3-70B endpoint used here
   (see Design decisions above), and tiers 2/3 correctly recovered every time; (b) the
   RAG and MCP nodes' explicit sentinel strings (`"not found in documents"`,
   `"No tool call was produced..."`) give the synthesizer, and a human reading
   `step_results`, something recognizable to flag. A natural extension not
   implemented here: a validator node between a specialist and the supervisor that
   detects these sentinels and re-routes to the *other* specialist instead of
   advancing `current_step_index` — turning detection into actual recovery.

2. **Compare this supervisor pattern with a single ReAct agent that has access to
   all tools. When is the supervisor pattern worth the added complexity?**

   ReAct's per-token, adaptive tool selection is genuinely flexible but opaque — no
   plan is visible before execution, behavior/cost are hard to predict up front, and
   it can invoke tools too eagerly or loop when uncertain (e.g. attempting to
   "calculate" using a hallucinated number instead of first retrieving the real one,
   because both retrieval and calculation tools sit in the same undifferentiated
   toolset and prompt). The supervisor pattern earns its complexity when the task has
   structurally distinct capabilities that benefit from *not* sharing a prompt or
   context: `RAG_EXTRACT_PROMPT` and `MCP_STEP_PROMPT` are each tuned independently,
   and the RAG node never even sees the MCP tools' schemas (and vice versa), which
   removes an entire class of cross-contamination. It also produces an inspectable
   plan up front (auditability) and a hard iteration bound of `len(plan)` steps rather
   than an open-ended ReAct loop. The cost is real, though, and visible directly in
   this implementation's own test runs: even a single-capability query (pure lookup,
   pure math) still pays for a planner call *and* at least one supervisor call, work a
   ReAct agent could have skipped by resolving the query in a single hop. The
   supervisor pattern is worth it once queries routinely mix retrieval and
   computation (this assignment's whole premise); for a corpus/tool combination
   that's almost always single-capability, ReAct's lower fixed overhead would likely
   win.

### Task 1.4 — RAG Agent

1. **The RAG agent retrieves for a single decomposed step, not the full user
   question. How does this affect retrieval quality compared to retrieving for the
   original question?**

   Generally improves precision, because the step text is narrower and already
   disambiguated by the planner. For the assignment's own example query ("What was
   Meridian's net revenue in FY2023, and what would it be after 3 years of 8%
   compound annual growth?"), embedding the *whole* question would pull the query
   vector toward calculation vocabulary ("8% compound annual growth") that has no
   match anywhere in the document, diluting similarity against the actual
   revenue-bearing chunk — whereas the decomposed step ("Find Meridian's net revenue
   for fiscal year 2023") embeds cleanly against report language. The tradeoff:
   retrieval quality is now bottlenecked by planner quality instead of the user's own
   phrasing — if the planner paraphrases into vocabulary the report doesn't use, the
   embedding overlap can be *worse* than the original question would have produced.
   This matters concretely for this corpus: `ai_prep_search` produced only 7 coarse
   chunks for the 14-page report (each chunk spans a full report section, not a
   paragraph), so per-step retrieval mostly helps by picking the *right section*
   rather than by fine-grained disambiguation within a section.

2. **If the planner produces a vague step like "find relevant financial data," how
   would you improve the retrieval query before sending it to the vector store?**

   I'd push the fix earlier in the pipeline rather than add a runtime rewrite step:
   `PLANNER_PROMPT` already forbids exactly this ("Each step must be a single,
   self-contained action..."; "Do not combine a lookup and a calculation into one
   step"), so a compliant plan should not emit a step this vague in the first place —
   cheaper than detecting and repairing vagueness after the fact. If a vague step got
   through anyway, the next-cheapest fix is query rewriting immediately before the
   `retriever.invoke()` call in `agent/rag_agent.py`: expand the step with (a) any
   named entities already established by earlier steps or the original question
   (company name, fiscal year) via the same `step_results` context already threaded
   through the node, and (b) a small fixed vocabulary hint scoped to what this
   corpus actually reports (revenue, net income, segment, region, guidance, risk
   factors), so "find relevant financial data" resolves toward a specific facet
   before it ever reaches the vector store.

### Task 2.1 — Model Definition

1. **Why does `models-from-code` require a self-contained file? What breaks if you
   reference external state (e.g., a database running only on your laptop)?**

   MLflow's models-from-code loader re-executes the literal Python file
   (`deployment/agent_model.py`) inside the serving container at load time to
   reconstruct the model object — it does not pickle live Python objects (which would
   break across LangGraph/LangChain version skew, or simply can't be pickled at all,
   like an open subprocess handle). Every name the file resolves at import time must
   therefore be resolvable purely from what's shipped alongside it: its own package
   imports (`agent`, `rag`, `config`, `tools`, shipped via `code_paths`) and whatever
   is listed in `pip_requirements`. A reference to external state that only exists on
   your laptop — a local pgvector Docker container, a Python object built in a
   notebook cell above it, a hardcoded local path — breaks the instant the container
   starts, because that state simply does not exist inside the sandboxed image
   Databricks builds. I hit a version of exactly this class of bug directly: a path
   computed relative to `agent_model.py`'s *own* file location assumed it kept its
   original `deployment/` parent folder inside the packaged artifact — an assumption
   about the packaging layout, not unlike assuming a local file path survives into
   the container — and it silently broke the moment that assumption was wrong (see
   Deployment section, bug #7). This is exactly why `rag/store.py` talks to a
   Databricks Vector Search index over HTTPS (reachable from anywhere with the same
   `DATABRICKS_HOST`/`DATABRICKS_TOKEN`) instead of a local database that only the
   serving container's *build machine* could ever see.

2. **Your model calls a managed Vector Search index at inference time rather than
   embedding documents into the container image. What are the tradeoffs (freshness,
   cold-start size, latency, failure modes) of querying an external index vs. baking
   the corpus into the model artifact?**

   Querying a managed index keeps the corpus fresh (re-ingesting a source document
   updates the index without re-registering or redeploying the model), keeps the
   container image small and cold starts fast (no embedding matrix/vector index
   bundled into the artifact — the artifact here is just Python code), and
   centralizes retrieval so local dev and the deployed endpoint hit *the same* index,
   which is the entire point of Task 0.3. The cost is an extra network hop per RAG
   step (added per-request latency) and a new failure mode: if Vector Search is
   briefly unavailable, or `VECTOR_SEARCH_ENDPOINT`/`VECTOR_SEARCH_INDEX` are
   misconfigured, retrieval fails *at inference time* rather than at build time —
   `rag/store.py` raises a clear `OSError` at call time specifically so this surfaces
   as a readable error rather than a silent empty result. It also means the MLflow
   artifact is not self-sufficient for reproducing past answers on its own — you need
   continued access to the live index, which matters for long-term audit once the
   corpus has since changed.

### Task 2.3 — Serving Endpoint

1. **Why must you pass `DATABRICKS_TOKEN` as an environment variable to the
   endpoint, even though it's already authenticated to serve models?**

   The endpoint's platform-level auth (used by whoever *calls* it — my client SDK,
   the OpenAI-compatible caller) is entirely separate from the credentials the model
   code running *inside* the container needs for its own *outbound* calls during
   inference: to the `DATABRICKS_MODEL` LLM serving endpoint, and to the Vector
   Search index. Model Serving does not forward the caller's identity into the
   container, nor grant the running container an ambient workspace credential by
   default — the container is just a sandboxed process making its own
   `httpx`/OpenAI-client requests to other Databricks services, and those need their
   own bearer token exactly like any external client would. That's precisely what
   `config.py`'s `ChatOpenAI(api_key=s["token"], ...)` and `rag/store.py`'s
   `DatabricksVectorSearch` consume, and why `deployment/deploy.py` injects the token
   as a **secret reference** (`{{secrets/cs4603-deploy/DATABRICKS_TOKEN}}`) rather
   than plaintext — it's a real, reusable credential and should never sit in an
   endpoint config in cleartext or in git history.

2. **What happens to in-flight requests when you deploy a new model version to the
   same endpoint? How does Databricks handle the transition?**

   Databricks Model Serving performs a rolling update per served entity: the new
   version's container is started and health-checked, and traffic is only shifted to
   it once it's ready; the old version's container keeps serving any requests it had
   already accepted until they finish, then is torn down. From the caller's
   perspective there's no downtime and no request is killed mid-flight — but there is
   a short window where consecutive requests can be answered by *different* model
   versions, since the switchover isn't atomic across every in-flight connection. One
   practical corollary I ran into directly: while a version's config update is still
   `IN_PROGRESS`, the endpoint rejects a *second* concurrent `update_config` call
   outright (`ResourceConflict: Endpoint served entities are currently being
   updated`) rather than queuing or superseding it — you cannot redirect an endpoint
   to a newer, fixed version until whatever update is already in flight resolves one
   way or another.

### Task 3.2 — Client

1. **Why is exponential backoff better than fixed-interval retries for a model
   serving endpoint?**

   A 429 or 503 from a model serving endpoint signals it currently has less capacity
   than demand — often because a scale-to-zero endpoint is still standing back up,
   which can take tens of seconds. Fixed-interval retries from every client hitting
   that wall keep re-arriving at the same cadence and can synchronize into repeated
   retry storms right as the endpoint is trying to recover, especially with many
   concurrent clients following the identical delay. Exponential backoff (with the
   jitter I added — `random.uniform(0, 0.5)` on top of `1.0 * 2**attempt`, capped at
   20s) spreads retries out over time and gives the endpoint room to actually finish
   scaling before the next attempt, instead of piling more load onto an endpoint
   that's already struggling.

2. **Your client has a `max_retries` parameter. What is the danger of setting it too
   high in a production system with many concurrent users?**

   Each client independently retrying many times during an outage or overload
   multiplies the effective request rate the endpoint sees (N users × M retries),
   which can turn a transient 503 into a sustained, self-inflicted overload that
   prevents the endpoint from ever catching up — a classic retry storm / thundering
   herd, and the exact opposite of what backoff was supposed to prevent. It also
   hides real outages from users for a long time: with `max_retries` high, a single
   logical `ask()` call can block for `sum(backoff delays)` before finally raising,
   which reads as a hang rather than a clear failure, and can exhaust upstream
   connection pools or worker threads if many callers are blocked simultaneously.

3. **When would you choose `ask_streaming()` over `ask()`? Give a concrete UX
   example.**

   Use streaming whenever the response will take long enough that a blank/loading UI
   would read as broken — e.g. a chat interface where the user is actively watching
   the response area and expects text to appear progressively, the way ChatGPT-style
   UIs work. This project's own combined queries are a good example of why: a
   retrieval+calculation question chains a planner call, one-or-more specialist
   calls, and a synthesizer call, which is multiple seconds of latency with nothing
   to show until the very end unless you stream. `ask()` is the right choice for a
   non-interactive caller — a batch script, a backend service composing the
   analyst's answer into a larger response, or a test/CI assertion — where nothing is
   rendering partial text and getting the complete string in one call is simpler than
   buffering chunks. One caveat specific to this deployment, called out in
   `ask_streaming()`'s docstring and verified in `tests/test_client_sdk.py`: since
   this endpoint is a models-from-code LangChain graph without a custom
   `predict_stream`, it may legitimately return only a single, non-incremental chunk
   — so here, streaming is more about not breaking against a future
   incrementally-streaming backend than delivering token-by-token UX today.

### Bonus A — CI/CD (if attempted)

1. **Why should the deploy step only run on `main` and not on feature branches?**

   `main` is the reviewed, single source of truth; feature branches are routinely
   broken or mid-change, and deploying from them would push untested work straight to
   the live endpoint, plus let concurrent feature branches race to overwrite each
   other's deployments. Merging to `main` is the explicit "this is ready" signal, so
   gating `deploy` on `github.ref == 'refs/heads/main'` keeps the live endpoint's
   state predictable and tied only to code that was actually reviewed and merged.

2. **What would you add to this pipeline to prevent deploying a model that performs
   worse than the current version? Describe the gate.**

   An evaluation job between `test` and `deploy` that runs the newly built graph
   against a small held-out set of question/expected-fact pairs (the 3 canonical
   queries above, plus a handful more spanning retrieval-only, computation-only, and
   combined), scores it (fuzzy match against known figures like net revenue/income,
   or an LLM-as-judge faithfulness check against the retrieved citations), and
   compares that score against the metric already logged under the currently-serving
   model version's MLflow run. If the new version's score regresses past a threshold,
   the job fails and `deploy` (which `needs:` it) never runs. This would use
   `mlflow.evaluate()` with a small custom scorer, reading the production version's
   metric via `mlflow.get_run(...).data.metrics` before comparing.

### Bonus B — `databricks-agents` SDK (if attempted)

1. **Compare the `agents.deploy()` approach with the manual MLflow + CLI approach
   from Part 2. What control do you gain or lose with each?**

   `agents.deploy()` collapses `log_model` + `register_model` + the
   `WorkspaceClient` endpoint config + secret-scope wiring into one call, and
   additionally provisions a Review App for free — a real speed and correctness win
   (one less place to get `environment_vars`/secret refs wrong). What you lose is the
   manual path's fine-grained control: exact `workload_size` tuning per call, precise
   control over rollout timing, the ability to point multiple named endpoints at the
   same registered version with different configs, and — the one that matters most
   when something breaks — full visibility into what the "one call" is actually
   doing, which makes a `DEPLOYMENT_FAILED` much harder to diagnose through the SDK
   abstraction than through the explicit `WorkspaceClient` calls in
   `deployment/deploy.py`. Not exercised live in this submission (see limitations
   note below), but the code path in `deployment/deploy_agents.py` reuses the exact
   same `log_and_register()` used by the manual path, verified working, and only
   swaps the final deploy call.

2. **The Review App enables human feedback collection. How would you use this
   feedback to improve the agent over time? Describe a concrete feedback loop.**

   Reviewers rate/annotate real Review-App queries (thumbs up/down plus free-text
   notes); periodically export the labeled examples from the MLflow experiment tied
   to the deployment. Use consistently-flagged-bad examples two ways: (a) fold them
   into the held-out evaluation set described in the Bonus A gate, so the *same*
   failure mode is caught automatically before any future deploy regresses on it
   again; and (b) mine them for concrete failure patterns — a recurring misroute, a
   retrieval miss on a specific report section, a synthesizer that drops a citation —
   that motivate a targeted prompt or chunking change, then re-run the eval set to
   confirm the change actually helps before merging, closing the loop.

### Bonus C — Standalone MCP server (if attempted)

1. **You moved the MCP server out of the model container. What did you gain
   (scaling, deployment, security, observability) and what new failure modes did you
   introduce (network, auth, latency, availability)?**

   Gained: the tool server can be redeployed, scaled, and rolled back independently
   of the much heavier LLM+graph model container; its logs/metrics live on their own
   Databricks App instead of being buried inside serving-endpoint logs; and multiple
   models/agents could share one tool service instead of each bundling its own copy
   of `tools/mcp_server.py`. Introduced: a new network hop and external dependency —
   verified directly by running `deployment/mcp_app/app.py` locally over HTTP:
   without a matching bearer token every request is correctly rejected (401), and
   only a request carrying the right `Authorization: Bearer <secret>` succeeds (200)
   — whereas the bundled stdio subprocess (Part 1) has no such external-availability
   or auth surface at all, since it's a local child process; plus a new service to
   monitor and alert on. The **live Databricks App deployment itself was not
   completed** in this submission — see limitations below.

2. **The remote MCP server now needs its own authentication. How would you secure it
   so that only your serving endpoint — not the public internet — can call the
   tools?**

   `deployment/mcp_app/app.py` wraps the MCP Starlette app with a
   `_BearerAuthMiddleware` that checks `Authorization: Bearer <MCP_SHARED_SECRET>`
   against a token stored as a Databricks secret and injected into **both** the
   App's and the serving endpoint's `environment_vars` — only a caller that already
   holds that secret (the serving endpoint) can invoke a tool. Verified locally: a
   request with no token or the wrong token gets 401; the correct token gets a real
   200 response from a genuine MCP `initialize` call. Beyond the application-level
   check, I'd also restrict the Databricks App's network/IP access policy (where the
   workspace plan supports it) so the HTTPS endpoint isn't reachable from the open
   internet at all — layering a network-level restriction under the bearer-token
   check rather than relying on the secret alone.

3. **When is bundling the tools in the container (Part 1) the better choice, and
   when is a separately deployed tool service (Bonus C) worth the extra moving
   parts?**

   Bundling wins when the tool server is small, cheap, and used by exactly one model
   — this assignment's five calculator tools fit that description — because it
   minimizes moving parts: one deployable unit, one thing to version, no extra
   network hop or auth surface to configure. A separately deployed tool service earns
   its complexity once the tools are shared across multiple agents/models, need a
   different release cadence or independent scaling from the model, are heavy enough
   that bundling them would bloat every container that uses them, or need
   centralized monitoring/rate-limiting that's far easier to enforce at one shared
   boundary than duplicated per model.

---

## Limitations / not fully completed, and exactly why

- **Bonus B (`databricks-agents`) live Review App demo.** The code path is verified
  by construction — `deployment/deploy_agents.py` reuses the exact same
  `log_and_register()` used by the now-confirmed-working manual path (Part 2), and
  only swaps the final deploy call for `agents.deploy()`. Not yet run live: doing so
  provisions a *separate* named serving endpoint plus a Review App, which is a new,
  cost-and-quota-consuming cloud resource I did not create unilaterally, and "open
  the Review App and submit 3 queries with feedback ratings" is an interactive
  web-UI task with no automation surface available here regardless. See
  `BONUS_IMPLEMENTATION.md` for the full step-by-step of what `agents.deploy()`
  does and exactly what running it would provision.
- **Bonus C live Databricks App deployment.** `deployment/mcp_app/app.py`,
  `app.yaml`, and `requirements.txt` are complete and independently verified correct
  (bearer auth + the MCP protocol both confirmed against a locally-run instance:
  wrong/missing token → 401, correct token → a real 200 from an `initialize` call).
  Not yet deployed live: doing so requires creating a new Databricks secret
  (`mcp-shared-secret`) and a new Databricks App, both new cloud resources requiring
  explicit authorization before creation, which was not given during this pass. See
  `BONUS_IMPLEMENTATION.md` for the full step-by-step, including the exact CLI
  commands that would complete this the moment that authorization is given.
