# PA4 Audit & Completion Guide

Supplementary doc (not part of the graded submission structure) summarizing a full
audit of this repo against `README.md` and everything verified live against the
real Databricks workspace, including the later pass that activated Bonus A, B, and
C end to end.

---

## 1. Audit result: what's implemented and verified

All 100 base points' worth of code matches the spec's required architecture, and
all three bonuses are now live-deployed and verified against the real workspace —
not just read as code.

| Check | Result |
|---|---|
| `uv run pytest -q` | 19/19 pass, offline, no credentials touched |
| `uv run ruff check agent client deployment` | clean |
| Vector Search index | `ready=True`, `ONLINE_NO_PENDING_UPDATE`, 7 indexed rows |
| Part 2 endpoint (`27100306-document-analyst`) | `READY`, `NOT_UPDATING`, answers correctly, now using the remote MCP server |
| Bonus A CI (`TalhaHassanUlHaq/cs4603-pa4`, fork) | Green run: [29598229429](https://github.com/TalhaHassanUlHaq/cs4603-pa4/actions/runs/29598229429) |
| Bonus B endpoint (`agents_cs4603-default-27100306_document_analyst`) | `READY`, answers correctly via OpenAI SDK, Review App feedback logged for all 3 canonical queries |
| Bonus C app (`cs4603-mcp-tools`) | `RUNNING`, reachable over HTTPS with OAuth + shared-secret auth, proven as the genuine calculation dependency (stop → failure, restart → recovery) |

### Part-by-part

- **Part 1 (40 pts)** — all match the spec's required shapes exactly; exceeds the
  minimum bar (persistent MCP session, cross-step context threading, 3-tier
  supervisor routing).
- **Part 2 (40 pts)** — env validation at import, full dependency lock + Python 3.12
  `conda_env` pin, a Windows-path MLflow packaging fix, and (this pass) a corrected
  rolling-update readiness check. Confirmed live: `READY`, correct answers, now via
  the remote MCP server.
- **Part 3 (20 pts)** — retry/backoff, timeout, streaming, `health_check()` — all
  implemented and covered by 16 offline tests, exercised live in the notebook.
- **Bonus A (15 pts)** — activated on a personal fork (`origin` is the read-only
  instructor repo). Found and fixed two real bugs on the first CI run (a missing
  `SERVING_ENDPOINT_NAME` env var in the status-print step, and a readiness check
  that declared success mid-rollout); the second run went fully green.
- **Bonus B (15 pts)** — `agents.deploy()` now fully live. Found and fixed five real
  bugs (Agent-Framework schema incompatibility, missing UC signature, an
  unconditional inference-table requirement unsupported on this workspace tier, an
  input-shape mismatch specific to plain `Runnable`s, and a missing
  `environment_vars` argument). Review App feedback for the 3 canonical queries
  logged via the MLflow traces API.
- **Bonus C (15 pts)** — MCP server deployed as its own Databricks App. Found and
  fixed six real bugs (`app.yaml`/`requirements.txt` needing to sit at the source
  root, a missing App `resources` declaration for the secret reference, the
  Databricks Apps platform OAuth gate requiring a service-principal
  client-credentials token, `Authorization` needing to be freed up for that token
  by moving the shared secret to its own header, and a secret-key naming
  mismatch). Proved the remote dependency live: stopping the app makes the
  deployed model's calculation step fail; a fresh model version restores it.

Full narrative for all eleven bonus-path bugs is in `STUDENT_ANALYSIS.md`'s "Bonus
A/B/C" sections — this file only summarizes.

---

## 2. Issues found (still relevant)

1. **Two `[GIVEN]` files were modified**: `config.py` (explicit `timeout=60.0` on
   `ChatOpenAI`) and `.env.example` (added `MCP_SERVER_URL`/SQL-warehouse/OAuth
   vars). Kept as-is per an explicit decision — small, well-documented, low risk
   unless a grader byte-diffs given files.
2. **Stale duplicate drafts**: `Analysis_1.md` and `pa4_1.ipynb` are earlier drafts,
   fully superseded by `STUDENT_ANALYSIS.md` and `pa4.ipynb` — exclude both from
   the submission zip.
3. **`build_logs.txt` / `service_logs.txt`** aren't in the prescribed directory
   structure — fine to keep locally, leave out of the submission zip.
4. **New cloud resources now exist** beyond the original Part 2 endpoint: a GitHub
   fork (`TalhaHassanUlHaq/cs4603-pa4`), a Bonus B serving endpoint + Review App, a
   Bonus C Databricks App, a dedicated service principal (`cs4603-mcp-caller`), and
   several new Databricks secrets. All are billable/quota-consuming — see the
   cleanup note below before final submission.

---

## 3. Cleanup before final submission

All three bonuses provisioned real, billable resources. Once you've captured
whatever evidence you need (screenshots, notebook outputs), consider tearing down
what you don't want to keep paying for:

```bash
# Part 2 + Bonus A/C endpoint (only if you're fully done with it)
databricks serving-endpoints delete 27100306-document-analyst

# Bonus B endpoint
databricks serving-endpoints delete agents_cs4603-default-27100306_document_analyst

# Bonus C app
databricks apps delete cs4603-mcp-tools

# Bonus C service principal (frees up the OAuth secret too)
databricks service-principals delete 75782457075382
```

Before zipping the submission:

- Exclude `Analysis_1.md`, `pa4_1.ipynb`, `build_logs.txt`, `service_logs.txt`.
- Exclude `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`.
- `uv.lock` is fine to include (small, no secrets) but not required.
