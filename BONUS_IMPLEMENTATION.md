# PA4 Bonus Parts — Implementation Walkthrough

This document is a step-by-step account of how each bonus part (A, B, C) was
actually implemented in this repository: what was built, why, in what order, what
broke along the way, and — honestly — what is and isn't live-verified as of this
write-up. It supplements `STUDENT_ANALYSIS.md` (which answers the graded ANALYSIS
QUESTIONS) with the "how it was built" narrative the questions don't ask for.

For status at a glance: **Bonus A is fully implemented and ready to run on
push/dispatch.** **Bonus B and Bonus C are code-complete and independently verified
by every means that doesn't require provisioning a new live cloud resource** (a
second serving endpoint + Review App for B; a new Databricks secret + Databricks App
for C) — provisioning those was intentionally left for explicit authorization rather
than done unilaterally, consistent with how this whole engagement has treated
costly, hard-to-reverse cloud actions. Each section below ends with the exact
commands to finish the live deployment when that authorization is given.

---

## Bonus A — GitHub Actions CI/CD Pipeline

**Goal (README):** pushing to `main` triggers lint → test → deploy, gated so deploy
only runs after lint+test pass and only on `main`.

**File:** `.github/workflows/deploy.yml`

### Step 1 — Split into two jobs, not one linear job

The workflow has exactly two jobs: `lint-and-test` and `deploy`, with
`deploy: needs: lint-and-test`. This isn't just organizational — GitHub Actions
treats `needs:` as a hard gate: if `lint-and-test` fails (or is skipped), `deploy`
never starts, regardless of what triggered the workflow. A single linear job with
lint/test/deploy as sequential steps would achieve the same ordering, but splitting
into jobs makes the gate explicit and lets `lint-and-test` run on every trigger
(including PRs) while `deploy` runs on a strict subset.

### Step 2 — Trigger on push, PR, and manual dispatch — but gate deploy separately

```yaml
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
```

`lint-and-test` runs on all three triggers (so PRs get lint/test feedback before
merge). `deploy` adds its own `if:` condition:

```yaml
if: github.ref == 'refs/heads/main' && github.event_name != 'pull_request'
```

This is the literal implementation of the assignment's own analysis question ("why
should deploy only run on main"): `github.ref` is the branch being built, and the
explicit `github.event_name != 'pull_request'` clause additionally protects against
a `pull_request` trigger whose `github.ref` briefly aliases to something
merge-commit-like — belt-and-suspenders against accidentally deploying from
untested, unmerged work.

### Step 3 — Lint and test, offline only

```yaml
- name: Install dependencies
  run: uv sync --extra dev
- name: Lint
  run: uv run ruff check agent client
- name: Test (offline smoke test — no Databricks credentials needed)
  run: uv run pytest -q
```

Deliberately no Databricks credentials are available to this job at all — `ruff
check agent client` and `pytest -q` (which now runs both `tests/test_smoke.py` and
`tests/test_client_sdk.py`, both fully mocked/offline) never touch a live workspace,
so a PR from a fork with no secrets access still gets full lint+test feedback.

### Step 4 — Deploy, with the real workspace credentials

```yaml
deploy:
  needs: lint-and-test
  if: github.ref == 'refs/heads/main' && github.event_name != 'pull_request'
  steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v3
    - run: uv sync
    - name: Log, register, and deploy the model
      env:
        DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
        DATABRICKS_TOKEN: ${{ secrets.DATABRICKS_TOKEN }}
        DATABRICKS_MODEL: ${{ secrets.DATABRICKS_MODEL }}
        EMBEDDINGS_ENDPOINT: ${{ vars.EMBEDDINGS_ENDPOINT }}
        UC_CATALOG: ${{ vars.UC_CATALOG }}
        UC_SCHEMA: ${{ vars.UC_SCHEMA }}
        VECTOR_SEARCH_ENDPOINT: ${{ vars.VECTOR_SEARCH_ENDPOINT }}
        VECTOR_SEARCH_INDEX: ${{ vars.VECTOR_SEARCH_INDEX }}
        SERVING_ENDPOINT_NAME: ${{ vars.SERVING_ENDPOINT_NAME }}
        SECRET_SCOPE: ${{ vars.SECRET_SCOPE }}
      run: uv run python deployment/deploy.py
```

Deliberate split between GitHub **Secrets** (`DATABRICKS_HOST`, `DATABRICKS_TOKEN`,
`DATABRICKS_MODEL` — genuine credentials, masked in logs) and GitHub **Variables**
(everything else — index names, catalog/schema, endpoint name — not sensitive, and
easier to inspect/change without re-entering a secret). This mirrors the exact same
secret-vs-plaintext split `deployment/deploy.py`'s `_secret_env_vars()` makes for the
*serving endpoint's* environment variables (Task 2.3) — the same principle applied
one layer up, at the CI level.

The `run: uv run python deployment/deploy.py` step is the entire deploy — no
duplicated logic in the workflow file. This means every bug fix made to
`deployment/deploy.py` throughout this engagement (the ten bugs documented in
`STUDENT_ANALYSIS.md`'s Deployment section, including the Windows-path
`model_code_path` fix) automatically applies to the CI deploy path too, since it's
the literal same script, not a re-implementation.

### Step 5 — Manual trigger + status print

`workflow_dispatch:` in the `on:` block lets anyone with write access run the whole
pipeline (including the deploy job, if on `main`) from the Actions tab without a
push — useful for redeploying after a secret rotation or a Vector Search index
rebuild with no code change. The final step prints the served entity/version:

```yaml
- name: Print deployed endpoint status
  run: |
    uv run python -c "
    ...
    print(f'Endpoint: {ep.name}')
    print(f'State: {ep.state}')
    for e in ep.config.served_entities or []:
        print(f'Served entity: {e.entity_name} version {e.entity_version}')
    "
```

### Current status

**Live and green.** `origin` (`alikhawaja/cs4603-pa4`) is the read-only instructor
repo, so activating this meant forking to `TalhaHassanUlHaq/cs4603-pa4`, pushing
this work there, and configuring the secrets/variables above via `gh secret
set`/`gh variable set` on the fork. The first run surfaced two real bugs — a
missing `SERVING_ENDPOINT_NAME` env var on the status-print step, and a readiness
check that declared success mid-rollout (both documented and fixed in
`STUDENT_ANALYSIS.md`) — and the second run went fully green in 10m21s:
[https://github.com/TalhaHassanUlHaq/cs4603-pa4/actions/runs/29598229429](https://github.com/TalhaHassanUlHaq/cs4603-pa4/actions/runs/29598229429).

---

## Bonus B — Deployment via `databricks-agents` SDK

**Goal (README):** deploy the same agent using `agents.deploy()` instead of the
manual `WorkspaceClient` endpoint config, and demonstrate the auto-provisioned
Review App.

**File:** `deployment/deploy_agents.py`

### Step 1 — Reuse the entire logging/registration pipeline unchanged

```python
from deployment.deploy import log_and_register

uc_name, version = log_and_register()
```

This is the central design decision: `deploy_agents.py` does **not** re-implement
`mlflow.langchain.log_model()` + `mlflow.register_model()`. It imports and calls the
exact same `log_and_register()` from `deployment/deploy.py` — the one already
proven (this session) to produce a correctly-loading model artifact (including the
Windows-path `model_code_path` fix, the full dependency lock, and the Python 3.12
`conda_env` pin). Bonus B only replaces the *serving* step, matching the
assignment's own framing ("you only swap the final `WorkspaceClient` deploy step for
a single `agents.deploy()` call").

The alternative — writing a second, parallel `log_and_register()` for Bonus B —
was deliberately rejected: it would double the surface area for the packaging bugs
already fixed once, and any future fix to the logging step would need to be applied
in two places instead of one.

### Step 2 — One call replaces the manual endpoint config

```python
from databricks import agents

deployment = agents.deploy(
    model_name=uc_name,
    model_version=version,
    scale_to_zero=True,
)
print(f"Endpoint: {deployment.endpoint_name}")
print(f"Review app: {deployment.review_app_url}")
```

Compare this to `deployment/deploy.py`'s `create_or_update_endpoint()`, which
manually builds `ServedEntityInput` + `EndpointCoreConfigInput`, wires
`environment_vars` (including the secret references for `DATABRICKS_HOST` /
`DATABRICKS_TOKEN` / `DATABRICKS_MODEL`), calls `serving_endpoints.create()` /
`update_config()`, and polls for `READY` — roughly 60 lines of manual SDK plumbing.
`agents.deploy()` collapses all of that into one call, and additionally
auto-provisions a Review App (a hosted UI for human reviewers to submit queries and
rate the responses) with zero extra code — something the manual path has no
equivalent for at all.

### Step 3 — What `agents.deploy()` does that isn't visible in this one call

Per the Databricks docs and the README's own framing, `agents.deploy()`:
- Registers/uses the given Unity Catalog model version (already done by
  `log_and_register()` above — `agents.deploy()` just needs the name+version).
- Creates a serving endpoint under an auto-derived name, with authentication handled
  automatically (no manual secret-scope wiring for the endpoint's own outbound
  credentials, unlike the manual path's explicit `{{secrets/...}}` references).
- Provisions a Review App: a separate hosted web UI, tied to the same MLflow
  experiment, where a human can type queries, see the agent's real responses, and
  attach a feedback rating — which lands as logged feedback in the MLflow
  experiment, retrievable via the tracking API.

### Current status

**Live and verified.** `agents.deploy()` provisioned a new, separate serving
endpoint (`agents_cs4603-default-27100306_document_analyst`, distinct from
`27100306-document-analyst`) plus a Review App. Getting it actually working
required finding and fixing five real bugs — Agent-Framework schema
incompatibility (the raw graph's output doesn't match
`ChatCompletionResponse`/`StringResponse`), a missing Unity Catalog signature, an
unconditional inference-table requirement this Free Edition workspace doesn't
support, an input-shape mismatch specific to how MLflow's langchain flavor treats
a plain `Runnable` versus the raw `CompiledStateGraph`, and a missing
`environment_vars` argument that left the container with no
`DATABRICKS_HOST`/`TOKEN`/`MODEL` at all — each documented in full in
`STUDENT_ANALYSIS.md`'s Bonus B section. The endpoint is `READY` and answers
correctly via the OpenAI SDK. Since no interactive browser session is available
here, the Review App's "submit 3 queries with feedback ratings" requirement was
satisfied via MLflow's traces/assessments API instead of the web UI: the 3
canonical queries were sent through the live endpoint, and
`mlflow.log_feedback(trace_id=..., name="user_rating", value=..., rationale=...)`
attached a rating + rationale to each resulting trace — the same mechanism a human
clicking thumbs-up/down in the Review App produces, landing on the same MLflow
experiment the Review App itself reads from.

---

## Bonus C — Standalone MCP Server as a Databricks App

**Goal (README):** stop bundling `tools/mcp_server.py` inside the model container;
run it as its own long-lived HTTP service, and have the agent connect to it
remotely instead of spawning it as a stdio subprocess.

**Files:** `deployment/mcp_app/app.py`, `deployment/mcp_app/app.yaml`,
`deployment/mcp_app/requirements.txt`, plus wiring in `agent/graph.py`,
`.env.example`, and `deployment/deploy.py`.

### Step 1 — Reuse the GIVEN tool definitions, switch only the transport

`tools/mcp_server.py` is a **given** file (must not be modified) and uses the stdio
transport (`mcp.run()`). Rather than duplicating the five tool definitions
(`calculate`, `percentage_change`, `growth_rate`, `compare_values`, `unit_convert`)
into a second file, `deployment/mcp_app/app.py` **imports the same `mcp` object**:

```python
from tools.mcp_server import mcp   # reuse the GIVEN tool definitions
```

and serves it over `streamable-http` instead of stdio:

```python
def build_app():
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    http_app = mcp.streamable_http_app()
    http_app.add_middleware(_BearerAuthMiddleware)
    return http_app
```

Two details that took actual debugging to get right, not just reading the docs:

- **Port comes from `$DATABRICKS_APP_PORT`, not a hardcoded value.** Databricks Apps
  assigns the port at runtime and injects it via this env var (default `8000` used
  only for local testing).
- **DNS-rebinding protection has to be explicitly disabled.** FastMCP's stdio-derived
  HTTP transport defaults to an allowlist of `Host:` headers (`localhost`,
  `127.0.0.1`, ...) as a defense against DNS-rebinding attacks from a browser. A
  Databricks App is reached through the platform's own proxy domain, never
  `localhost` — with the allowlist on, *every* real request through the Databricks
  proxy gets rejected outright. This is safe to disable here specifically because
  the bearer-token middleware (next step) is the actual access control, not the
  Host-header check.

### Step 2 — Add authentication, since this is now a public network service

A bundled stdio subprocess (Part 1) has no network exposure at all — it's a local
child process. The moment the tool server is reachable over HTTPS, it needs its own
access control, or anyone who finds the URL can call the calculation tools. Bonus C
requirement: only the serving endpoint (which holds the shared secret) should be
able to call it.

```python
class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not _SHARED_SECRET:
            return JSONResponse({"error": "server misconfigured..."}, status_code=500)
        if request.headers.get("authorization") != f"Bearer {_SHARED_SECRET}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
```

`_SHARED_SECRET` is read from `MCP_SHARED_SECRET` (set via `app.yaml`'s
`valueFrom: "mcp-shared-secret"`, a Databricks secret reference) — the same shared
secret is injected into **both** this App's environment and the serving endpoint's
`environment_vars` (see Step 4), so only a caller holding that one secret can
authenticate.

**Verified locally** (without needing a live Databricks App): running
`deployment/mcp_app/app.py` on `localhost` and sending requests with `httpx`
confirmed a missing/wrong `Authorization` header gets a clean `401`, and the correct
`Bearer <secret>` header gets a real `200` from a genuine MCP `initialize` call —
proving the auth middleware and the underlying MCP protocol both work correctly,
independent of the Databricks Apps platform itself.

### Step 3 — A Databricks App needs its own `requirements.txt`

Unlike this repo's own `pyproject.toml`/`uv`-managed environment, a Databricks App's
runtime starts **minimal** and does not inherit anything from the repo it's deployed
from — it only installs what `deployment/mcp_app/requirements.txt` lists. This was
missing initially (an easy thing to overlook, since everything works fine locally
where the full `uv` environment is already present) and would have made even an
authorized deploy attempt fail at container start with `ModuleNotFoundError`. Fixed
by adding:

```
mcp>=1.0.0
starlette>=0.37.0
uvicorn>=0.30.0
```

— the three packages `app.py` needs directly or transitively (`tools/mcp_server.py`
only needs `mcp` itself; `starlette`/`uvicorn` are for the HTTP layer this file adds
on top).

### Step 4 — Wire the URL (and secret) through to both local runs and the deployed model

`agent/graph.py::load_mcp_tools()` already branches on `MCP_SERVER_URL`:

```python
mcp_url = os.environ.get("MCP_SERVER_URL")
if mcp_url:
    connections = {
        "analyst": {
            "url": f"{mcp_url.rstrip('/')}/mcp",
            "transport": "streamable_http",
            "headers": {"Authorization": f"Bearer {os.environ.get('MCP_SHARED_SECRET', '')}"},
        }
    }
else:
    # ... Part 1 stdio subprocess fallback
```

`.env.example` documents both variables, unset by default (falls back to stdio):

```
# ─── Bonus C — standalone MCP server as a Databricks App ────────────────────
# Leave unset to use the Part 1 stdio subprocess. Set once the app is deployed.
MCP_SERVER_URL=
MCP_SHARED_SECRET=
```

**The gap found and fixed during this pass:** `deployment/deploy.py`'s
`_secret_env_vars()` — the function that builds the *deployed endpoint's*
`environment_vars` — never forwarded `MCP_SERVER_URL`/`MCP_SHARED_SECRET` at all.
That meant even with a fully working Databricks App and a local `.env` pointing at
it, the **deployed** container would still silently fall back to the stdio
subprocess, since the container never receives those two variables — a real,
previously-unnoticed gap between "works when I run it locally" and "works once
deployed" (precisely the class of bug the README's whole Task 2.1/2.3 section warns
about). Fixed by extending `_secret_env_vars()`:

```python
mcp_url = os.environ.get("MCP_SERVER_URL", "")
if mcp_url:
    env_vars["MCP_SERVER_URL"] = mcp_url
    env_vars["MCP_SHARED_SECRET"] = f"{{{{secrets/{scope}/MCP_SHARED_SECRET}}}}"
```

Conditional on `MCP_SERVER_URL` actually being set — unconditionally requiring an
`MCP_SHARED_SECRET` secret to exist in the scope would break the *baseline* Part 2
deploy for anyone who hasn't attempted Bonus C at all (whose scope has no such
secret). `MCP_SERVER_URL` itself is plaintext (just a URL, not a credential,
consistent with how `VECTOR_SEARCH_ENDPOINT` etc. are handled); `MCP_SHARED_SECRET`
is passed as a `{{secrets/...}}` reference, consistent with how `DATABRICKS_TOKEN`
is handled — never plaintext, since it's a genuine bearer credential.

### Current status

**Live and verified**, all four README requirements met. Getting there required
finding and fixing six more real bugs beyond the ones anticipated in this doc's
earlier drafts (all documented in full in `STUDENT_ANALYSIS.md`'s Bonus C
section):

1. `app.yaml` has to sit at the *root* of the deployed source-code-path, not
   nested at `deployment/mcp_app/app.yaml` as the repo's own layout keeps it.
2. Same for `requirements.txt` — the Apps runtime only installs from a root-level
   file.
3. A bare `env: valueFrom: "mcp-shared-secret"` doesn't self-resolve; the App
   object itself needs a matching `resources` entry (set via `databricks apps
   update --json`).
4. Databricks Apps enforce their own platform-level OAuth gate in front of every
   app — a valid workspace PAT with full permissions still gets a platform 401
   before the app's own code runs at all. Fixed with a dedicated service principal
   (`cs4603-mcp-caller`), granted `CAN_USE` on the app, whose OAuth
   client-credentials token (via `agent/graph.py::_fetch_mcp_oauth_token()`)
   satisfies that gate.
5. With the OAuth token now occupying `Authorization`, the app-level shared secret
   moved to its own header, `X-MCP-Shared-Secret`.
6. `deploy.py::_secret_env_vars()` referenced the secret as
   `MCP_SHARED_SECRET` (uppercase) when the actual key — per this very doc's own
   step 1 command — is `mcp-shared-secret` (lowercase, hyphenated), breaking the
   main endpoint's redeploy with `InvalidParameterValue`.

**Confirmed live, exactly per the README's four Bonus C requirements:**
`databricks apps list` shows `cs4603-mcp-tools` running with a real HTTPS URL; the
main deployed endpoint answers a calculation query correctly through it; stopping
the app makes the same kind of query fail cleanly (not a silent stdio fallback);
and the bundled model still ships `tools/mcp_server.py` for the stdio-fallback
path, but calculation traffic genuinely goes over HTTP once `MCP_SERVER_URL` is
set.
