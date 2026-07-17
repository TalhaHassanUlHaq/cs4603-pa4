"""Bonus B — deploy via the databricks-agents SDK (deployment/deploy_agents.py).

Reuses the exact same models-from-code definition + log_and_register() pipeline as
Part 2 (deployment/deploy.py); the only thing that changes is the final deploy step:
one `agents.deploy()` call replaces the manual WorkspaceClient endpoint config, and
also auto-provisions a Review App for human feedback collection.

Run:  uv run python deployment/deploy_agents.py
"""

from __future__ import annotations

from deployment.deploy import log_and_register


def main() -> None:
    from databricks import agents

    uc_name, version = log_and_register()

    deployment = agents.deploy(
        model_name=uc_name,
        model_version=version,
        scale_to_zero=True,
    )
    print(f"Endpoint: {deployment.endpoint_name}")
    print(f"Review app: {deployment.review_app_url}")


if __name__ == "__main__":
    main()
