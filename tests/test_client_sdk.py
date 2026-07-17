"""Offline tests for client/sdk.py using httpx.MockTransport -- no real endpoint,
no Databricks credentials. Verifies retry/backoff, timeout handling, error wrapping,
health_check, and both streaming code paths (SSE deltas and non-SSE single chunk).
"""

from __future__ import annotations

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client.sdk import (  # noqa: E402
    _MAX_RETRY_AFTER_DELAY,
    AnalystClientError,
    DocumentAnalystClient,
)


def _client(transport: httpx.MockTransport, **kwargs) -> DocumentAnalystClient:
    c = DocumentAnalystClient(
        endpoint_name="test-endpoint",
        host="https://example.databricks.com",
        token="dapi-fake",
        **kwargs,
    )
    c._client.close()
    c._client = httpx.Client(
        transport=transport,
        timeout=c.timeout,
        headers={"Authorization": "Bearer dapi-fake", "Content-Type": "application/json"},
    )
    return c


def test_ask_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    c = _client(httpx.MockTransport(handler))
    assert c.ask("hi") == "hello"


def test_ask_handles_bare_list_response_from_real_endpoint():
    """The live serving endpoint returns a top-level JSON list of AnalystState
    dicts (not wrapped in {"predictions": [...]}) -- confirmed against the real
    deployed model. ask() must unwrap this, not just the dict-shaped forms."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "messages": [
                        {"type": "human", "content": "hi"},
                        {"type": "ai", "content": "the answer"},
                    ],
                    "final_answer": "the answer",
                }
            ],
        )

    c = _client(httpx.MockTransport(handler))
    assert c.ask("hi") == "the answer"


def test_ask_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"message": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok after retry"}}]})

    c = _client(httpx.MockTransport(handler), max_retries=3)
    c._sleep_for_retry = lambda attempt, response: None  # skip real sleeping in tests
    assert c.ask("hi") == "ok after retry"
    assert calls["n"] == 3


def test_ask_raises_analyst_client_error_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "scaling up"}, headers={"x-request-id": "abc123"})

    c = _client(httpx.MockTransport(handler), max_retries=2)
    c._sleep_for_retry = lambda attempt, response: None
    with pytest.raises(AnalystClientError) as exc_info:
        c.ask("hi")
    assert exc_info.value.status_code == 503
    assert exc_info.value.request_id == "abc123"


def test_ask_wraps_non_retryable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad request"})

    c = _client(httpx.MockTransport(handler))
    with pytest.raises(AnalystClientError) as exc_info:
        c.ask("hi")
    assert exc_info.value.status_code == 400


def test_ask_raises_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    c = _client(httpx.MockTransport(handler), timeout=0.001)
    with pytest.raises(TimeoutError):
        c.ask("hi")


def test_ask_streaming_sse_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"choices": [{"delta": {"content": "Hello "}}]},
            {"choices": [{"delta": {"content": "world"}}]},
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    c = _client(httpx.MockTransport(handler))
    pieces = list(c.ask_streaming("hi"))
    assert pieces == ["Hello ", "world"]


def test_ask_streaming_raises_timeout_on_a_stream_that_never_completes():
    """A server that keeps sending SSE chunks without ever emitting [DONE] or
    closing the connection must not make ask_streaming() iterate forever: every
    individual read succeeds (so httpx's own read timeout never fires), so the
    generator needs its own wall-clock deadline over the whole operation."""

    def handler(request: httpx.Request) -> httpx.Response:
        def endless_chunks():
            while True:
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'

        return httpx.Response(
            200, content=endless_chunks(), headers={"content-type": "text/event-stream"}
        )

    c = _client(httpx.MockTransport(handler), timeout=0.2)
    with pytest.raises(TimeoutError):
        list(c.ask_streaming("hi"))


def test_ask_streaming_wraps_non_retryable_error_instead_of_crashing():
    """A server that rejects stream=True outright (e.g. a real MLflow-langchain
    deployment: 400 "This endpoint does not support streaming") must surface as a
    clean AnalystClientError, not an unrelated httpx.ResponseNotRead crash from
    reading .json()/.text on an un-read streamed response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "This endpoint does not support streaming."})

    c = _client(httpx.MockTransport(handler))
    with pytest.raises(AnalystClientError) as exc_info:
        list(c.ask_streaming("hi"))
    assert exc_info.value.status_code == 400
    assert "does not support streaming" in str(exc_info.value)


def test_ask_streaming_single_chunk_fallback_non_sse():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "full answer"}}]})

    c = _client(httpx.MockTransport(handler))
    pieces = list(c.ask_streaming("hi"))
    assert pieces == ["full answer"]


def test_ask_streaming_full_message_frames_yield_once():
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"choices": [{"message": {"content": "partial"}}]},
            {"choices": [{"message": {"content": "partial full"}}]},
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    c = _client(httpx.MockTransport(handler))
    pieces = list(c.ask_streaming("hi"))
    assert pieces == ["partial full"]  # only the last full frame, not both


def test_health_check_returns_false_on_error_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    c = _client(httpx.MockTransport(handler))
    assert c.health_check() is False


def test_constructor_requires_host_and_token():
    with pytest.raises(AnalystClientError):
        DocumentAnalystClient(endpoint_name="x", host="", token="")


def _sleep_for_retry_with_captured_delay(monkeypatch, retry_after: str) -> float:
    import client.sdk as sdk_mod

    slept = []
    monkeypatch.setattr(sdk_mod.time, "sleep", lambda d: slept.append(d))
    c = DocumentAnalystClient(endpoint_name="x", host="https://example.com", token="t")
    c._sleep_for_retry(0, httpx.Response(429, headers={"Retry-After": retry_after}))
    return slept[-1]


def test_sleep_for_retry_caps_a_huge_retry_after_instead_of_hanging(monkeypatch):
    """A buggy or malicious upstream Retry-After must not force an effectively
    unbounded time.sleep() -- it should still be honored, just capped."""
    delay = _sleep_for_retry_with_captured_delay(monkeypatch, "9999")
    assert delay <= _MAX_RETRY_AFTER_DELAY


def test_sleep_for_retry_ignores_negative_retry_after_without_crashing(monkeypatch):
    delay = _sleep_for_retry_with_captured_delay(monkeypatch, "-5")
    assert delay >= 0  # fell back to the exponential-backoff delay, no ValueError


def test_sleep_for_retry_still_honors_a_reasonable_retry_after(monkeypatch):
    delay = _sleep_for_retry_with_captured_delay(monkeypatch, "3")
    assert delay >= 3
