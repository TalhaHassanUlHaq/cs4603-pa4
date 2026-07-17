"""Log, register, and serve the Document Analyst (Tasks 2.2 + 2.3).

Run:  uv run python deployment/deploy.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import mlflow
import mlflow.langchain.utils.logging as _mlflow_lc_logging
from dotenv import load_dotenv

load_dotenv()

# mlflow prints Unicode decorations (e.g. a running-person emoji on `start_run`) that
# crash with UnicodeEncodeError on Windows' default cp1252 console encoding. Force
# UTF-8 on stdout/stderr so logging never takes down an otherwise-successful deploy.
# Guarded with hasattr because sys.stdout/sys.stderr are replaced with a
# zmq-backed OutStream (no .reconfigure()) when this module is imported inside a
# Jupyter/IPython kernel (e.g. from pa4.ipynb) rather than run as a plain script --
# that stream already handles Unicode correctly, so there is nothing to fix there.
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# MLflow's models-from-code embeds the *resolved, OS-native* `lc_model` path string
# into the model's flavor metadata (`model_code_path`) at log time, then reconstructs
# it at serving-load time with `os.path.basename(...)` (see
# mlflow/langchain/model.py::_load_pyfunc). `os.path.basename` only splits on "/", so
# a path resolved on Windows (backslash-only, e.g. "F:\\...\\agent_model.py") comes
# back *unsplit* on the Linux serving container, which then does
# `os.path.join("/model", "F:\\...\\agent_model.py")` -- producing a literal, broken
# path. This is exactly the "No such file or directory: '/model/F:\\...'" failure seen
# in service_logs.txt for v10 (UPDATE_FAILED, "error occurred in model loading code").
# Wrapping MLflow's validator to return `.as_posix()` keeps its validation /
# Databricks-notebook-export behavior identical but guarantees the stored path is
# forward-slash-only, so the container's POSIX `basename()` correctly extracts just
# "agent_model.py" regardless of which OS ran `deploy.py`.
_orig_validate_and_get_model_code_path = _mlflow_lc_logging._validate_and_get_model_code_path


def _posix_validate_and_get_model_code_path(model_code_path: str, temp_dir: str) -> str:
    result = _orig_validate_and_get_model_code_path(model_code_path, temp_dir)
    return Path(result).as_posix()


_mlflow_lc_logging._validate_and_get_model_code_path = _posix_validate_and_get_model_code_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_MODEL_PATH = os.path.join(ROOT, "deployment", "agent_model.py")

# Full transitive dependency lock + explicit Python 3.12, delivered via conda_env.
#
# Two things the real build logs (see logs.txt / STUDENT_ANALYSIS.md) forced:
#
# 1. FULL LOCK, not just direct pins. The container's modern pip (26.x) aborts with
#    `resolution-too-deep` when the graph is too deep. Pinning only the ~14 DIRECT deps
#    was not enough -- pip backtracked through `databricks-connect` (pulled transitively
#    via databricks-langchain -> unitycatalog-*[databricks], 40+ releases) and blew past
#    its depth limit across 5 retries. Pinning the ENTIRE transitive closure to one
#    version each (deployment/requirements-lock.txt) gives pip a single candidate per
#    package and zero backtracking.
#
# 2. PYTHON 3.12, not the 3.13 MLflow infers from this machine. databricks-connect 17.x
#    (the only in-range version that is also numpy-2 compatible, which mlflow/langchain
#    require) declares Requires-Python ==3.12.*; the newest 3.13 build (16.1.7) caps
#    numpy<2 and conflicts. So on Python 3.13 NO databricks-connect satisfies both, and
#    the build fails with "No matching distribution found for databricks-connect". We
#    therefore pin the container to Python 3.12 (which the Databricks ecosystem targets)
#    by handing MLflow a full `conda_env` -- the serving image is conda-built, so its
#    conda.yaml `python=` line is what governs the runtime version.
LOCK_FILE = os.path.join(ROOT, "deployment", "requirements-lock.txt")
PYTHON_VERSION = "3.12"


def _load_pip_requirements() -> list[str]:
    """Read the fully-pinned lock, dropping comment and blank lines."""
    with open(LOCK_FILE, encoding="utf-8") as fh:
        reqs = [
            line.strip()
            for line in fh
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not reqs:
        raise RuntimeError(f"No requirements found in {LOCK_FILE}")
    return reqs


def _conda_env() -> dict:
    """Full conda environment pinning Python 3.12 + the exact pip lock (see above)."""
    return {
        "name": "mlflow-env",
        "channels": ["conda-forge"],
        "dependencies": [
            f"python={PYTHON_VERSION}",
            "pip",
            {"pip": _load_pip_requirements()},
        ],
    }


INPUT_EXAMPLE = {"messages": [{"role": "user", "content": "What was the revenue?"}]}


def _model_name() -> str:
    return os.environ.get("SERVING_MODEL_NAME") or os.environ.get(
        "SERVING_ENDPOINT_NAME", "document-analyst"
    ).replace("-", "_")


def log_and_register() -> tuple[str, str]:
    """Log the model via MLflow models-from-code and register it in Unity Catalog.

    Returns (uc_full_name, version).
    """
    catalog = os.environ["UC_CATALOG"]
    schema = os.environ["UC_SCHEMA"]
    model_name = _model_name()
    uc_full_name = f"{catalog}.{schema}.{model_name}"

    # Track against the Databricks-hosted tracking server, not local disk. Locally,
    # mlflow's default file:./mlruns store produces a malformed file: URI on Windows
    # whenever the repo path contains a space (this repo's does), which makes
    # mlflow.register_model() fail against Unity Catalog with a cryptic
    # "Illegal character in opaque part" 400 -- pointing tracking at the workspace
    # sidesteps local-URI issues entirely and is also the correct target for a
    # Unity Catalog registration regardless of platform.
    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")
    mlflow.set_experiment(f"/Shared/cs4603-pa4-{model_name}")

    with mlflow.start_run(run_name="document-analyst") as run:
        model_info = mlflow.langchain.log_model(
            lc_model=AGENT_MODEL_PATH,
            name="agent",
            code_paths=[
                os.path.join(ROOT, "agent"),
                os.path.join(ROOT, "rag"),
                os.path.join(ROOT, "tools"),
                os.path.join(ROOT, "config.py"),
            ],
            conda_env=_conda_env(),
            input_example=INPUT_EXAMPLE,
        )
        print(f"Logged model in run {run.info.run_id} -> {model_info.model_uri}")

    registered = mlflow.register_model(model_info.model_uri, uc_full_name)
    print(f"Registered {uc_full_name} version {registered.version}")
    return uc_full_name, registered.version


def _secret_env_vars() -> dict[str, str]:
    scope = os.environ.get("SECRET_SCOPE", "cs4603-deploy")
    env_vars = {
        "DATABRICKS_HOST": f"{{{{secrets/{scope}/DATABRICKS_HOST}}}}",
        "DATABRICKS_TOKEN": f"{{{{secrets/{scope}/DATABRICKS_TOKEN}}}}",
        "DATABRICKS_MODEL": f"{{{{secrets/{scope}/DATABRICKS_MODEL}}}}",
        "VECTOR_SEARCH_ENDPOINT": os.environ.get("VECTOR_SEARCH_ENDPOINT", ""),
        "VECTOR_SEARCH_INDEX": os.environ.get("VECTOR_SEARCH_INDEX", ""),
        "EMBEDDINGS_ENDPOINT": os.environ.get("EMBEDDINGS_ENDPOINT", "databricks-gte-large-en"),
    }
    # Bonus C: only wire the remote MCP server through when actually configured.
    # agent/graph.py::load_mcp_tools() falls back to the Part 1 stdio subprocess
    # whenever MCP_SERVER_URL is unset, so requiring an MCP_SHARED_SECRET secret to
    # exist on every deploy -- even for students who never attempted Bonus C --
    # would break the baseline Part 2 path for everyone else.
    mcp_url = os.environ.get("MCP_SERVER_URL", "")
    if mcp_url:
        env_vars["MCP_SERVER_URL"] = mcp_url
        env_vars["MCP_SHARED_SECRET"] = f"{{{{secrets/{scope}/MCP_SHARED_SECRET}}}}"
    return env_vars


def create_or_update_endpoint(uc_name: str, version: str) -> str:
    """Create or update a Model Serving endpoint for the registered model version."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import (
        EndpointCoreConfigInput,
        EndpointStateConfigUpdate,
        EndpointStateReady,
        ServedEntityInput,
    )

    _FAILED_STATES = (
        EndpointStateConfigUpdate.UPDATE_FAILED,
        EndpointStateConfigUpdate.UPDATE_CANCELED,
    )

    endpoint_name = os.environ.get("SERVING_ENDPOINT_NAME", "document-analyst")
    w = WorkspaceClient()

    served_entities = [
        ServedEntityInput(
            entity_name=uc_name,
            entity_version=version,
            workload_size="Small",
            scale_to_zero_enabled=True,
            environment_vars=_secret_env_vars(),
        )
    ]

    existing = None
    try:
        existing = w.serving_endpoints.get(endpoint_name)
    except Exception:
        existing = None

    if existing is None:
        print(f"Creating endpoint {endpoint_name}...")
        w.serving_endpoints.create(
            name=endpoint_name,
            config=EndpointCoreConfigInput(name=endpoint_name, served_entities=served_entities),
        )
    else:
        print(f"Updating endpoint {endpoint_name} to version {version}...")
        w.serving_endpoints.update_config(name=endpoint_name, served_entities=served_entities)

    deadline = time.monotonic() + 40 * 60  # first-time container builds can take 20-30+ min
    while time.monotonic() < deadline:
        status = w.serving_endpoints.get(endpoint_name)
        state = status.state
        # `state.ready` alone is not enough: during a rolling update the endpoint
        # reports READY the whole time because the *previous* served-entity version
        # keeps answering traffic (confirmed live -- see STUDENT_ANALYSIS.md/
        # COMPLETION_GUIDE.md bug log), while `state.config_update` is what actually
        # tracks whether the new version has finished rolling out. Both must hold
        # before this function can truthfully report the new version is serving.
        if (
            state
            and state.ready == EndpointStateReady.READY
            and state.config_update == EndpointStateConfigUpdate.NOT_UPDATING
        ):
            break
        if state and state.config_update in _FAILED_STATES:
            raise RuntimeError(
                f"Endpoint {endpoint_name} update failed ({state.config_update}). "
                f"Check Serving -> {endpoint_name} -> Logs in the workspace UI for "
                "the container build error."
            )
        print(f"  ...state={state}, waiting")
        time.sleep(20)
    else:
        print(f"WARNING: endpoint {endpoint_name} did not reach READY within 40 minutes")

    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    url = f"{host}/serving-endpoints/{endpoint_name}/invocations"
    print(f"Endpoint URL: {url}")
    return url


if __name__ == "__main__":
    name, ver = log_and_register()
    create_or_update_endpoint(name, ver)
