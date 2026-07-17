"""Bonus C — standalone MCP server as a Databricks App.

Reuses the GIVEN tool definitions from tools/mcp_server.py but serves them over the
streamable-HTTP transport instead of stdio, so the tool server can be deployed,
scaled, and monitored independently of the model (see README Bonus C). The agent
connects to it via `agent/graph.py::load_mcp_tools()` when `MCP_SERVER_URL` is set.
"""

from __future__ import annotations

import os
import sys

# Make the repo root importable whether this is run directly or launched by a
# Databricks App from an arbitrary working directory.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from tools.mcp_server import mcp  # noqa: E402  (reuse the GIVEN tool definitions)

_SHARED_SECRET = os.environ.get("MCP_SHARED_SECRET", "")


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request without a matching `Authorization: Bearer <secret>` header.

    A shared secret (stored as a Databricks secret, injected as MCP_SHARED_SECRET
    into both this App's and the serving endpoint's environment_vars) restricts
    callers to whoever holds the token -- in practice, only the serving endpoint --
    instead of leaving the calculation tools open to the public internet.
    """

    async def dispatch(self, request: Request, call_next):
        if not _SHARED_SECRET:
            return JSONResponse(
                {"error": "server misconfigured: MCP_SHARED_SECRET not set"},
                status_code=500,
            )
        if request.headers.get("authorization") != f"Bearer {_SHARED_SECRET}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    # Databricks Apps proxies requests through its own domain, not localhost, so the
    # default DNS-rebinding Host-header allowlist would reject every real request
    # here. The bearer-token middleware above is the actual access control.
    mcp.settings.transport_security.enable_dns_rebinding_protection = False

    http_app = mcp.streamable_http_app()
    http_app.add_middleware(_BearerAuthMiddleware)
    return http_app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)
