# PA4 Audit & Completion Guide

Supplementary doc (not part of the graded submission structure) summarizing a full
audit of this repo against `README.md`, what was live-verified against the real
Databricks workspace, and exact steps for the parts that still require your
explicit go-ahead (they'd provision new billable cloud resources or need an
interactive web UI step no agent can do).

---

## 1. Audit result: what's implemented and verified

All 100 base points' worth of code matches the spec's required architecture. Verified
directly against the live workspace (not just read as code):

| Check | Result |
|---|---|
| `uv run pytest -q` | 19/19 pass, offline, no credentials touched |
| `uv run ruff check agent client` | clean |
| Vector Search index (`VectorSearchClient().get_index(...).describe()`) | `ready=True`, `ONLINE_NO_PENDING_UPDATE`, 7 indexed rows |
| Serving endpoint (`WorkspaceClient().serving_endpoints.get(...)`) | `READY`, serving `cs4603.default.27100306_document_analyst` v12 |
| Live HTTP call to the deployed endpoint | 200 OK, correct cited answer |
| Workspace inventory (`serving_endpoints.list()`, `apps.list()`, `secrets.list_scopes()`) | Only one PA4 endpoint exists; no Bonus B endpoint/Review App, no Databricks App, no extra secret scope |

### Part-by-part

- **Part 1 (40 pts)** — [agent/state.py](agent/state.py), [agent/planner.py](agent/planner.py),
  [agent/supervisor.py](agent/supervisor.py), [agent/rag_agent.py](agent/rag_agent.py),
  [agent/synthesizer.py](agent/synthesizer.py), [agent/graph.py](agent/graph.py): all
  match the spec's required shapes exactly — state fields, 3-tier supervisor routing
  (structured output → keyword-parse → deterministic heuristic), the `messages`-channel
  write in the synthesizer, and a dedicated MCP event-loop thread for Windows/Jupyter
  stdio safety. Exceeds the minimum bar (persistent MCP session instead of one-per-call,
  cross-step context threading so dependent steps resolve correctly).
- **Part 2 (40 pts)** — [deployment/agent_model.py](deployment/agent_model.py),
  [deployment/deploy.py](deployment/deploy.py): validates env vars at import, a
  self-contained models-from-code file, a full transitive dependency lock +
  Python 3.12 `conda_env` pin, and a real fix for an MLflow Windows-path packaging bug.
  Confirmed live: endpoint `READY`, correct answers.
- **Part 3 (20 pts)** — [client/sdk.py](client/sdk.py): retry/backoff, timeout,
  streaming SSE parsing, `AnalystClientError`, `health_check()` — all implemented,
  covered by 12 offline tests in [tests/test_client_sdk.py](tests/test_client_sdk.py),
  and exercised live in the notebook.
- **Bonus A (15 pts)** — [.github/workflows/deploy.yml](.github/workflows/deploy.yml):
  correctly gated (`needs: lint-and-test`, `if: refs/heads/main`), splits GitHub
  Secrets vs. Variables correctly. **Never actually run** — no secrets/variables are
  configured on the GitHub remote yet.
- **Bonus B (15 pts)** — [deployment/deploy_agents.py](deployment/deploy_agents.py)
  reuses `log_and_register()` unchanged, code-complete. `agents.deploy()` itself
  never invoked.
- **Bonus C (15 pts)** — [deployment/mcp_app/app.py](deployment/mcp_app/app.py),
  `app.yaml`, `requirements.txt`: bearer-auth middleware, correct
  `DATABRICKS_APP_PORT`/DNS-rebinding handling, wiring through `agent/graph.py` and
  `deploy.py`'s `_secret_env_vars()`. Verified locally (401/200 against a local HTTP
  instance). Never deployed as an actual Databricks App.

---

## 2. Issues found

1. ~~`pa4.ipynb`'s last cell didn't actually execute~~ — **fixed**: it showed a
   Jupyter kernel error instead of real output; re-executed the equivalent code
   live and patched the cell's output/execution_count in place.
2. **Two `[GIVEN]` files were modified**: [config.py](config.py) (added an explicit
   `timeout=60.0` on `ChatOpenAI` — a real, defensible fix, but the file is listed
   as do-not-modify) and `.env.example` (added `MCP_SERVER_URL`/SQL-warehouse vars —
   arguably required by Bonus C's own instructions). Worth a conscious decision
   before submitting.
3. **Stale duplicate drafts**: `Analysis_1.md` and `pa4_1.ipynb` are earlier drafts,
   fully superseded by `STUDENT_ANALYSIS.md` and `pa4.ipynb`. Not part of the
   prescribed structure — **exclude both from the submission zip** (left in place
   per your choice, not deleted).
4. **`build_logs.txt` / `service_logs.txt`** (120KB combined) aren't in the
   prescribed directory structure — fine to keep locally as debugging evidence, but
   leave out of the submission zip.
5. **Registered UC model is v13 but the endpoint serves v12** — a v13 rollout hit
   `ResourceConflict` because a prior update was still in-flight. Not broken (v12
   answers correctly), but if you want them to match, re-run
   `create_or_update_endpoint()` once the endpoint is idle (see §3.4 below).

---

## 3. Step-by-step: completing the parts that need your go-ahead

These all either provision new billable cloud resources or require an interactive
web UI step — not something to do unilaterally. `BONUS_IMPLEMENTATION.md` has more
narrative detail; this is the condensed action list.

### 3.1 Activate Bonus A (CI/CD) — needs GitHub repo access

```bash
# Secrets (masked in logs)
gh secret set DATABRICKS_HOST
gh secret set DATABRICKS_TOKEN
gh secret set DATABRICKS_MODEL

# Variables (plaintext, non-sensitive)
gh variable set EMBEDDINGS_ENDPOINT --body "databricks-gte-large-en"
gh variable set UC_CATALOG --body "cs4603"
gh variable set UC_SCHEMA --body "default"
gh variable set VECTOR_SEARCH_ENDPOINT --body "27100306-vs-endpoint"
gh variable set VECTOR_SEARCH_INDEX --body "cs4603.default.27100306_analyst_index"
gh variable set SERVING_ENDPOINT_NAME --body "27100306-document-analyst"
gh variable set SECRET_SCOPE --body "cs4603-deploy"
```

Then push to `main` (or use the Actions tab's "Run workflow" for
`workflow_dispatch`) and screenshot the green run as submission evidence.

### 3.2 Complete Bonus B (`databricks-agents` Review App) — provisions a new endpoint

```bash
uv run python deployment/deploy_agents.py
```

This prints an `endpoint_name` and a `review_app_url`. Then, interactively:

1. Open the printed `review_app_url` in a browser.
2. Submit the 3 canonical queries (net income; 15% of 2.4bn; revenue + 10% growth)
   and rate each response.
3. Confirm the feedback landed in the MLflow experiment
   (`mlflow.get_run(...)` or the Experiments UI) for your write-up.

### 3.3 Complete Bonus C (standalone MCP Databricks App) — provisions a new secret + app

```bash
# 1. Shared secret (same scope Part 2 already uses)
databricks secrets put-secret cs4603-deploy mcp-shared-secret --string-value "<a long random token>"

# 2. Create + deploy the app
databricks apps create cs4603-mcp-tools
databricks apps deploy cs4603-mcp-tools --source-code-path <workspace path containing deployment/mcp_app/>

# 3. Confirm it's running
databricks apps list   # look for cs4603-mcp-tools in "running" state

# 4. Point both local and deployed runs at it -- add to .env:
MCP_SERVER_URL=https://<the-app's-url>
MCP_SHARED_SECRET=<same token from step 1>

# 5. Re-run deploy.py so _secret_env_vars() forwards both into the endpoint's env vars
uv run python deployment/deploy.py

# 6. Prove it's genuinely remote (README requirement #3): ask a calculation query,
#    confirm it works, then:
databricks apps stop cs4603-mcp-tools
#    ask the same kind of query again -- the calculation step should now fail,
#    proving the deployed model was calling the remote app, not a bundled stdio fallback.
```

### 3.4 Optional: reconcile the endpoint to serve v13

```bash
uv run python -c "
from deployment.deploy import create_or_update_endpoint
create_or_update_endpoint('cs4603.default.27100306_document_analyst', '13')
"
```
Run this once `databricks serving-endpoints get 27100306-document-analyst` shows
`config_update=NOT_UPDATING` (i.e. no update already in flight).

### 3.5 Before zipping the submission

- Exclude `Analysis_1.md`, `pa4_1.ipynb`, `build_logs.txt`, `service_logs.txt`.
- Exclude `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`,
  `uv.lock` is fine to include (small, no secrets) but not required.
- Re-open `pa4.ipynb`, select the project's `.venv` kernel, and do a final
  top-to-bottom "Restart & Run All" if you want fully fresh timestamps — the
  current outputs are all genuine (live-captured), so this is optional polish,
  not a correctness requirement.
