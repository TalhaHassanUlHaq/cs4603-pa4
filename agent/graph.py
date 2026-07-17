"""Full Document Analyst graph (Tasks 1.5 + 1.7)."""

from __future__ import annotations

import asyncio
import atexit
import os
import sys
import threading

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agent.planner import make_planner
from agent.prompts import MCP_STEP_PROMPT
from agent.rag_agent import make_rag_agent
from agent.state import AnalystState
from agent.supervisor import make_supervisor, route_from_supervisor
from agent.synthesizer import make_synthesizer

DEFAULT_MCP_SERVER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "mcp_server.py"
)


class _MCPLoopThread:
    """Owns a dedicated background event loop for all MCP client traffic.

    LangGraph nodes are synchronous, but langchain-mcp-adapters is async. Bridging
    with a bare `asyncio.run()` inside a node breaks the moment the caller already
    has a loop running (e.g. inside pa4.ipynb / Jupyter). On Windows the stdio
    transport additionally needs `asyncio.create_subprocess_exec`, which requires the
    Proactor event loop -- Jupyter's kernel loop is not guaranteed to be one. Running
    a single dedicated loop in its own thread, with Proactor forced on win32 before
    the loop is created, sidesteps both problems regardless of what loop (if any) the
    calling context is running.
    """

    def __init__(self) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        # Bounded, not indefinite: if the background thread ever died before
        # signalling ready (e.g. asyncio.set_event_loop raising in some exotic
        # environment), an unconditional ready.wait() would hang the constructor
        # forever with no diagnostic. A bounded wait plus an is_alive() check turns
        # that into a clear error instead of a silent hang.
        if not ready.wait(timeout=10) or not self._thread.is_alive():
            raise RuntimeError("MCP event-loop thread failed to start within 10s")

    def _run(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        ready.set()
        self._loop.run_forever()

    def run(self, coro, timeout: float = 30.0):
        """Run `coro` on the bridge loop and block for up to `timeout` seconds.

        `concurrent.futures.Future.result(timeout=...)` only stops *waiting* -- on
        its own it does not stop the coroutine, which would otherwise keep running
        on the shared background loop indefinitely (confirmed: a coroutine that
        outlives its timeout keeps executing and its result is silently discarded).
        Explicitly cancelling the future on timeout propagates a CancelledError into
        the coroutine at its next await point, so a slow/hung MCP call is actually
        torn down instead of leaking a runaway task on the one shared event loop
        every subsequent call also depends on.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(f"MCP call did not complete within {timeout}s") from None

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


_bridge: _MCPLoopThread | None = None
_mcp_client = None  # keep a strong reference alive for the process lifetime
_mcp_session_cm = None  # entered `client.session(...)` context manager; kept open
_mcp_session = None  # the persistent ClientSession every tool call reuses


def _get_bridge() -> _MCPLoopThread:
    global _bridge
    if _bridge is None:
        _bridge = _MCPLoopThread()
        atexit.register(_cleanup_mcp_session)
    return _bridge


def _cleanup_mcp_session() -> None:
    """Close the persistent MCP session (if any), then stop the bridge loop.

    Registered with atexit instead of stopping the loop directly so the session's
    `__aexit__` -- which for the stdio transport terminates the subprocess -- still
    has a running loop to execute on.
    """
    global _mcp_session_cm, _mcp_session
    if _mcp_session_cm is not None and _bridge is not None:
        try:
            _bridge.run(_mcp_session_cm.__aexit__(None, None, None), timeout=10.0)
        except Exception:
            pass
        _mcp_session_cm = None
        _mcp_session = None
    if _bridge is not None:
        _bridge.stop()


def _ensure_valid_mcp_stdio_errlog() -> None:
    """Work around a Jupyter-specific MCP stdio crash on Windows.

    `mcp.client.stdio.stdio_client`'s `errlog` parameter defaults to `sys.stderr`,
    bound once when the function is defined (i.e. at first import of
    mcp.client.stdio), not re-read on every call. Under Jupyter, sys.stderr is
    ipykernel's OutStream, whose `.fileno()` raises `io.UnsupportedOperation` --
    which crashes Windows subprocess creation deep inside the mcp/anyio/asyncio
    stack (subprocess.Popen needs a real OS file handle for stderr). Since the
    bound default can't be fixed by patching sys.stderr at call time, rebind the
    default itself, once, to a real file if it's broken.
    """
    import mcp.client.stdio as _mcp_stdio

    target = getattr(_mcp_stdio.stdio_client, "__wrapped__", _mcp_stdio.stdio_client)
    current_errlog = target.__defaults__[0] if target.__defaults__ else None
    try:
        if current_errlog is not None:
            current_errlog.fileno()
        return  # already a real file descriptor, nothing to fix
    except Exception:
        pass
    target.__defaults__ = (open(os.devnull, "w"),)


def _fetch_mcp_oauth_token() -> str:
    """Exchange MCP_APP_CLIENT_ID/SECRET for a Databricks OAuth access token.

    Databricks Apps sit behind the platform's own access proxy, which requires a
    genuine Databricks OAuth bearer token in `Authorization` before a request ever
    reaches the app's own code -- confirmed live: a request carrying only the
    app-level MCP_SHARED_SECRET as `Authorization: Bearer <secret>` gets a
    platform-level 401 (`server: databricks`, no app code involved at all). A
    service principal (created for this purpose, granted CAN_USE on the App) can
    get a real token via the standard OAuth client-credentials flow -- this needs
    no browser/interactive login, unlike user-based OAuth, so it works from inside
    the serving container exactly like the existing PAT-based auth does.
    """
    import httpx

    host = os.environ["DATABRICKS_HOST"].rstrip("/")
    client_id = os.environ["MCP_APP_CLIENT_ID"]
    client_secret = os.environ["MCP_APP_CLIENT_SECRET"]
    response = httpx.post(
        f"{host}/oidc/v1/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def load_mcp_tools(server_path: str | None = None):
    """Connect to the MCP tool server and return LangChain tool objects.

    Uses the streamable-HTTP transport when MCP_SERVER_URL is set (Bonus C -- a
    standalone MCP server deployed as its own Databricks App), otherwise spawns the
    GIVEN tools/mcp_server.py as a stdio subprocess (Part 1 default, and what the
    serving container does too -- see DEPLOYMENT_GUIDE.md).

    Opens ONE persistent MCP session and returns tools bound to it, rather than
    calling `MultiServerMCPClient.get_tools()` directly. `get_tools()`'s tool
    closures hold no session of their own, so -- per its own docstring ("a new
    session will be created for each tool call") -- every single tool invocation
    would otherwise open a fresh session from scratch: for the stdio transport
    (Part 1's default) that means spawning a brand-new subprocess, re-running its
    Python interpreter startup and imports, and redoing the MCP handshake, on
    *every* calculation step. Measured directly: with a shared session, a
    `calculate` call is ~0.01s; with a fresh session per call (the default), the
    identical call takes ~0.75s -- almost entirely subprocess-spawn overhead, not
    computation -- which also eats directly into `_MCPLoopThread.run()`'s 30s
    per-call timeout budget on every single MCP node visit, not just the first.
    """
    global _mcp_client, _mcp_session_cm, _mcp_session
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools as _mcp_load_tools

    mcp_url = os.environ.get("MCP_SERVER_URL")
    if mcp_url:
        connections = {
            "analyst": {
                "url": f"{mcp_url.rstrip('/')}/mcp",
                "transport": "streamable_http",
                "headers": {
                    "Authorization": f"Bearer {_fetch_mcp_oauth_token()}",
                    "X-MCP-Shared-Secret": os.environ.get("MCP_SHARED_SECRET", ""),
                },
            }
        }
    else:
        _ensure_valid_mcp_stdio_errlog()
        resolved = os.path.abspath(server_path or DEFAULT_MCP_SERVER_PATH)
        connections = {
            "analyst": {
                "command": sys.executable,  # match whatever interpreter/venv is running
                "args": [resolved],
                "transport": "stdio",
            }
        }

    client = MultiServerMCPClient(connections)
    _mcp_client = client  # keep a strong reference alive for the process lifetime
    bridge = _get_bridge()

    # A previous call to load_mcp_tools() left a session open (e.g. a second graph
    # build in the same process) -- close it before opening a new one instead of
    # leaking the old subprocess/connection.
    if _mcp_session_cm is not None:
        try:
            bridge.run(_mcp_session_cm.__aexit__(None, None, None), timeout=10.0)
        except Exception:
            pass
        _mcp_session_cm = None
        _mcp_session = None

    async def _open_session():
        cm = client.session("analyst")
        session = await cm.__aenter__()
        return cm, session

    _mcp_session_cm, _mcp_session = bridge.run(_open_session(), timeout=30.0)

    async def _get_tools():
        return await _mcp_load_tools(_mcp_session)

    return bridge.run(_get_tools(), timeout=30.0)


def _stringify_tool_output(output) -> str:
    """Flatten an MCP tool result into a plain string.

    langchain-mcp-adapters returns tool output as a list of MCP content blocks
    (dicts like {"type": "text", "text": "...", "id": ...}) rather than a bare
    string, so extract and join the text parts.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif hasattr(item, "text"):
                parts.append(str(item.text))
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else str(output)
    return str(output)


def make_mcp_node(tools, llm):
    llm_with_tools = llm.bind_tools(tools)
    by_name = {t.name: t for t in tools}
    bridge = _get_bridge()

    def mcp_tools(state: AnalystState) -> dict:
        idx = state.get("current_step_index", 0)
        step = state["plan"][idx]
        prior_results = state.get("step_results", [])

        human_content = step
        if prior_results:
            context = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(prior_results))
            human_content = (
                f"Results of earlier steps (use these numbers if this step refers "
                f"to them):\n{context}\n\nThis step: {step}"
            )

        try:
            msg = llm_with_tools.invoke(
                [SystemMessage(content=MCP_STEP_PROMPT), HumanMessage(content=human_content)]
            )
        except Exception as exc:
            result = f"Error invoking tool-calling model: {exc}"
            step_results = state.get("step_results", []) + [result]
            return {"step_results": step_results, "current_step_index": idx + 1}

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            result = getattr(msg, "content", None) or "No tool call was produced for this step."
        else:
            call = tool_calls[0]
            tool = by_name.get(call["name"])
            if tool is None:
                result = f"Unknown tool requested: {call['name']!r}"
            else:
                try:
                    raw_output = bridge.run(tool.ainvoke(call["args"]), timeout=30.0)
                    output = _stringify_tool_output(raw_output)
                except Exception as exc:
                    output = f"Error calling {call['name']}: {exc}"
                result = f"[{call['name']}({call['args']})] -> {output}"

        step_results = state.get("step_results", []) + [result]
        return {"step_results": step_results, "current_step_index": idx + 1}

    return mcp_tools


def build_graph(llm=None, retriever=None, tools=None):
    """Assemble and compile the full Document Analyst graph.

    Dependencies default to the real Databricks-backed factories when not injected,
    so the same function serves production (deployment/agent_model.py), local
    notebook use, AND offline unit tests with fakes (tests/test_smoke.py).
    """
    if llm is None:
        from config import get_chat_llm

        llm = get_chat_llm()
    if retriever is None:
        from rag.store import get_retriever

        retriever = get_retriever()
    if tools is None:
        tools = load_mcp_tools()

    builder = StateGraph(AnalystState)
    builder.add_node("planner", make_planner(llm))
    builder.add_node("supervisor", make_supervisor(llm))
    builder.add_node("rag_agent", make_rag_agent(retriever, llm))
    builder.add_node("mcp_tools", make_mcp_node(tools, llm))
    builder.add_node("synthesizer", make_synthesizer(llm))

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "supervisor")
    builder.add_conditional_edges("supervisor", route_from_supervisor)
    builder.add_edge("rag_agent", "supervisor")
    builder.add_edge("mcp_tools", "supervisor")
    builder.add_edge("synthesizer", END)

    return builder.compile()
