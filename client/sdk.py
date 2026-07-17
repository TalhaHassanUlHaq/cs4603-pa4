"""Python client SDK for the deployed Document Analyst (Part 3)."""

from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Iterator

import httpx

_RETRYABLE_STATUS = (429, 503)
_BASE_DELAY = 1.0
_MAX_DELAY = 20.0
# Upper bound on how long a server-supplied Retry-After can push a single retry
# wait to. Without this, `max(delay, float(retry_after))` below lets an upstream
# response (buggy or malicious) force an effectively unbounded `time.sleep()` --
# confirmed: Retry-After: 9999 produces a 9999s sleep, bypassing _MAX_DELAY
# entirely. Retry-After is still honored (a real scale-up can legitimately take
# longer than the exponential-backoff cap), just not without limit.
_MAX_RETRY_AFTER_DELAY = 60.0


class AnalystClientError(Exception):
    def __init__(self, message: str, status_code=None, request_id=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts)


class DocumentAnalystClient:
    def __init__(
        self,
        endpoint_name: str,
        host: str | None = None,
        token: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
    ) -> None:
        self.endpoint_name = endpoint_name
        self.host = (host or os.environ.get("DATABRICKS_HOST", "")).rstrip("/")
        self.token = token or os.environ.get("DATABRICKS_TOKEN", "")
        if not self.host or not self.token:
            raise AnalystClientError(
                "DocumentAnalystClient requires a Databricks host and token. Pass "
                "them explicitly or set DATABRICKS_HOST / DATABRICKS_TOKEN."
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )

    @property
    def _invocations_url(self) -> str:
        return f"{self.host}/serving-endpoints/{self.endpoint_name}/invocations"

    def _payload(self, question: str, stream: bool = False) -> dict:
        payload = {"messages": [{"role": "user", "content": question}]}
        if stream:
            payload["stream"] = True
        return payload

    def _sleep_for_retry(self, attempt: int, response: httpx.Response) -> None:
        delay = min(_MAX_DELAY, _BASE_DELAY * (2**attempt)) + random.uniform(0, 0.5)
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                requested = float(retry_after)
            except ValueError:
                requested = None
            if requested is not None and requested >= 0:
                delay = max(delay, min(requested, _MAX_RETRY_AFTER_DELAY))
        time.sleep(delay)

    def _post(self, payload: dict, stream: bool = False):
        attempt = 0
        while True:
            start = time.monotonic()
            try:
                if stream:
                    request = self._client.build_request(
                        "POST", self._invocations_url, json=payload
                    )
                    response = self._client.send(request, stream=True)
                else:
                    response = self._client.post(self._invocations_url, json=payload)
            except httpx.TimeoutException as exc:
                elapsed = time.monotonic() - start
                raise TimeoutError(
                    f"Request to '{self.endpoint_name}' timed out after {elapsed:.1f}s"
                ) from exc
            except httpx.HTTPError as exc:
                raise AnalystClientError(f"Request to '{self.endpoint_name}' failed: {exc}") from exc

            if response.status_code < 400:
                return response

            if response.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                if stream:
                    response.close()
                self._sleep_for_retry(attempt, response)
                attempt += 1
                continue

            request_id = response.headers.get("x-request-id")
            if stream:
                # A streamed response's body isn't read yet -- .json()/.text raise
                # httpx.ResponseNotRead unless we read() it first, which would
                # otherwise mask the real error (e.g. a 400 "does not support
                # streaming") behind an unrelated crash.
                response.read()
            try:
                detail = response.json().get("message") or response.text
            except Exception:
                detail = response.text
            if stream:
                response.close()
            raise AnalystClientError(
                f"Endpoint '{self.endpoint_name}' returned {response.status_code}: {detail}",
                status_code=response.status_code,
                request_id=request_id,
            )

    def ask(self, question: str) -> str:
        response = self._post(self._payload(question))
        data = response.json()
        # The live endpoint returns a bare top-level JSON list (one AnalystState
        # dict per input row) rather than a {"predictions": [...]} envelope --
        # confirmed against the real deployed model, whose graph output MLflow
        # serializes without wrapping. Unwrap that before the shape checks below.
        record = data[0] if isinstance(data, list) and data else data
        if isinstance(record, dict):
            if "choices" in record:
                return record["choices"][0]["message"]["content"]
            if "predictions" in record:  # some MLflow serving shapes
                pred = record["predictions"]
                if isinstance(pred, list):
                    pred = pred[0]
                if isinstance(pred, dict) and "messages" in pred:
                    return pred["messages"][-1]["content"]
                return str(pred)
            if "messages" in record:
                return record["messages"][-1]["content"]
        raise AnalystClientError(
            f"Unrecognized response shape from '{self.endpoint_name}': {data!r}"
        )

    def ask_streaming(self, question: str) -> Iterator[str]:
        response = self._post(self._payload(question, stream=True), stream=True)
        # httpx's read timeout applies per-socket-read, not to the request as a
        # whole -- a server that keeps sending small SSE chunks (or blank
        # keep-alive lines) without ever emitting "data: [DONE]" or closing the
        # connection would otherwise make this generator iterate forever, since
        # every individual read keeps succeeding within the timeout window.
        # Confirmed by reproduction: an endless mock SSE stream made this loop
        # never return on its own. An explicit wall-clock deadline over the whole
        # streaming operation, reusing self.timeout, bounds the total regardless
        # of how quickly individual chunks keep arriving.
        deadline = time.monotonic() + self.timeout
        try:
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                # Server ignored stream=True (or isn't stream-capable) -- treat a
                # single, non-incremental completion as a valid outcome.
                data = response.json()
                text = self._extract_text(data)
                if text:
                    yield text
                return

            emitted = False
            last_full_text = None
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Streaming response from '{self.endpoint_name}' did not "
                        f"complete within {self.timeout:.1f}s"
                    )
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="ignore")
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or [{}]
                choice = choices[0]
                delta_content = (choice.get("delta") or {}).get("content")
                if delta_content:
                    emitted = True
                    yield delta_content
                    continue
                message_content = (choice.get("message") or {}).get("content")
                if message_content:
                    last_full_text = message_content

            if not emitted and last_full_text:
                yield last_full_text
        finally:
            response.close()

    @staticmethod
    def _extract_text(data: dict) -> str:
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            if "messages" in data:
                return data["messages"][-1]["content"]
        return ""

    def health_check(self) -> bool:
        try:
            from databricks.sdk import WorkspaceClient
            from databricks.sdk.service.serving import EndpointStateReady

            w = WorkspaceClient(host=self.host, token=self.token)
            endpoint = w.serving_endpoints.get(self.endpoint_name)
            return bool(endpoint.state and endpoint.state.ready == EndpointStateReady.READY)
        except Exception:
            pass

        try:
            response = self._client.post(
                self._invocations_url,
                json=self._payload("ping"),
                timeout=min(self.timeout, 10.0),
            )
            return response.status_code < 500
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DocumentAnalystClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
