"""Bonus B — deploy via the databricks-agents SDK (deployment/deploy_agents.py).

Reuses `log_and_register()`'s packaging pipeline from Part 2
(deployment/deploy.py) -- same code_paths, same conda_env/dependency lock, same UC
registration -- but points it at `deployment/agent_model_agents.py` instead of
`deployment/agent_model.py`.

That swap is required, not optional: `agents.deploy()` calls
`_check_model_is_rag_compatible()` on the logged model and raises
`ValueError: The model's schema is not compatible with Agent Framework` if the
model's output schema isn't `ChatCompletionResponse`/`StringResponse`/a bare
string -- confirmed by actually running this against `deployment/agent_model.py`,
whose graph.invoke() output is the full custom AnalystState dict
(messages/plan/step_results/next_agent/final_answer), none of which match.
`agent_model_agents.py` wraps the same graph to return just the final answer text,
which satisfies the schema check. Everything else -- the deploy call itself,
Review App provisioning -- is unchanged from the assignment's description.

Run:  uv run python deployment/deploy_agents.py
"""

from __future__ import annotations

import os

from mlflow.models import infer_signature

from deployment.deploy import INPUT_EXAMPLE, ROOT, _secret_env_vars, log_and_register

AGENTS_MODEL_PATH = os.path.join(ROOT, "deployment", "agent_model_agents.py")

# Unity Catalog requires explicit signature metadata on every registered model
# version. `mlflow.langchain.log_model()` auto-infers one for the raw compiled
# StateGraph used by Part 2 (its models-from-code auto-invocation captures the
# AnalystState-shaped output directly), but does not for the plain
# `RunnableLambda` wrapper this file logs -- confirmed live: without this,
# `mlflow.register_model()` raised "Model passed for registration did not contain
# any signature metadata." `infer_signature` only needs representative input/output
# *values*, not an actual model invocation, so this costs nothing at deploy time.
AGENTS_SIGNATURE = infer_signature(INPUT_EXAMPLE, "Example answer text.")


def _disable_inference_tables() -> None:
    """Work around `agents.deploy()` unconditionally requesting inference tables.

    `databricks.agents.deployments._create_ai_gateway_config()` always returns an
    `AiGatewayConfig` with `inference_table_config.enabled=True` and has no
    parameter to opt out -- confirmed live: `agents.deploy()` raised
    `NotFound: Inference table is not currently supported for this endpoint type
    in this workspace` (a Databricks Free Edition limitation, not a code bug).
    Inference tables are an optional auto-logging add-on, not required for the
    Review App / serving functionality Bonus B actually asks for, so patch the
    endpoint-creation call to omit the AI Gateway config entirely rather than
    fail the whole deploy over an unsupported observability feature.
    """
    from databricks.agents import deployments

    deployments._create_ai_gateway_config = lambda model_name: None


def main() -> None:
    from databricks import agents

    _disable_inference_tables()

    uc_name, version = log_and_register(
        lc_model_path=AGENTS_MODEL_PATH, signature=AGENTS_SIGNATURE
    )

    # Without this, the container has no DATABRICKS_HOST/TOKEN/MODEL or Vector
    # Search config at all -- confirmed live: `agents.deploy()` with no
    # `environment_vars` produced a container that failed with "An error occurred
    # in model loading code" on every version, because `agent_model_agents.py`'s
    # `get_settings()` call raises OSError at import time the moment a required
    # env var is missing (the exact env-validation behavior Task 2.1 asks for --
    # it did its job, just not the job I expected here). Reusing the same
    # `_secret_env_vars()` Part 2's `create_or_update_endpoint()` already builds
    # keeps both deploy paths consistent instead of duplicating the secret-vs-
    # plaintext wiring a second time.
    deployment = agents.deploy(
        model_name=uc_name,
        model_version=version,
        scale_to_zero=True,
        environment_vars=_secret_env_vars(),
    )
    print(f"Endpoint: {deployment.endpoint_name}")
    print(f"Review app: {deployment.review_app_url}")


if __name__ == "__main__":
    main()
