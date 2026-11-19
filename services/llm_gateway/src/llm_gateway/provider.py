"""Thin client for Groq's OpenAI-compatible chat API.

Its only job is to turn HTTP outcomes into three kinds of error the router
handles differently:

  RateLimited      429: wait `retry_after`, or move to another model
  TransientError   timeouts, 5xx: back off and retry the same model
  ModelOutputError 400 tool_use_failed: the model misbehaved; the caller can correct it
  PermanentError   other 4xx: retrying will not help, surface it to the caller
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

MODEL_OUTPUT_CODES = {"tool_use_failed", "json_validate_failed"}


def _error_body(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    def __init__(self, retry_after: float, headers: dict[str, str], detail: str = "") -> None:
        super().__init__(f"rate limited, retry after {retry_after}s: {detail}".rstrip(": "))
        self.retry_after = retry_after
        self.headers = headers
        self.detail = detail
        # "Request too large": this request can never fit this model's limits.
        # Waiting will not help; another model might.
        self.too_large = "request too large" in detail.lower()


class TransientError(ProviderError):
    pass


class ModelOutputError(ProviderError):
    """The request was fine; the model's output was not (e.g. it called a tool that does
    not exist). Groq reports this as a 400 with code `tool_use_failed`. Different from a
    bad request: the *caller* can fix it by telling the model what went wrong."""

    def __init__(self, code: str, message: str, failed_generation: str | None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.failed_generation = code, message, failed_generation


class PermanentError(ProviderError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status


class StreamInterrupted(PermanentError):
    """The stream broke after text reached the caller. Not retried: a retry would send
    the caller the same words twice. The caller sees an error and can ask again."""

    def __init__(self, detail: str) -> None:
        super().__init__(502, f"stream interrupted after output began: {detail}")


OnDelta = Callable[[str], Awaitable[None]]


class GroqProvider:
    def __init__(self, client: httpx.AsyncClient, api_key: str, base_url: str) -> None:
        self.client = client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def chat(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        if not self.api_key:
            raise PermanentError(500, "GROQ_API_KEY is not set on the gateway")
        try:
            r = await self.client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e

        headers = {k.lower(): v for k, v in r.headers.items()}
        _raise_for_status(r, headers)
        return r.json(), headers

    async def chat_stream(self, payload: dict[str, Any],
                          on_delta: OnDelta) -> tuple[dict[str, Any], dict[str, str]]:
        """Same contract as `chat`, but answer text is passed to `on_delta` as it is
        generated. Errors before the first word map exactly as in `chat`, so the router
        can still retry or fall back; after it, only StreamInterrupted.

        Returns a response in the non-streaming shape (tool calls and usage rebuilt from
        the deltas), so everything after the call (ledger, quota, cache) is unchanged."""
        if not self.api_key:
            raise PermanentError(500, "GROQ_API_KEY is not set on the gateway")
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        content: list[str] = []
        calls: dict[int, dict] = {}
        usage: dict = {}
        finish = None
        emitted = False
        try:
            async with self.client.stream(
                    "POST", f"{self.base_url}/chat/completions", json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"}) as r:
                headers = {k.lower(): v for k, v in r.headers.items()}
                if r.status_code >= 400:
                    await r.aread()
                    _raise_for_status(r, headers)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if err := chunk.get("error"):
                        _raise_stream_error(err, emitted)
                    groq_usage = (chunk.get("x_groq") or {}).get("usage")
                    usage = chunk.get("usage") or groq_usage or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if text := delta.get("content"):
                            content.append(text)
                            emitted = True
                            await on_delta(text)
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {
                                "id": "", "type": "function",
                                "function": {"name": "", "arguments": ""}})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["function"]["name"] += fn.get("name") or ""
                            slot["function"]["arguments"] += fn.get("arguments") or ""
                        finish = choice.get("finish_reason") or finish
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if emitted:
                raise StreamInterrupted(f"{type(e).__name__}") from e
            raise TransientError(f"{type(e).__name__}: {e}") from e
        message = {"role": "assistant", "content": "".join(content) or None}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {"choices": [{"message": message, "finish_reason": finish}],
                "usage": usage}, headers


def _raise_for_status(r: httpx.Response, headers: dict[str, str]) -> None:
    if r.status_code == 429:
        try:
            retry_after = float(headers.get("retry-after", "5"))
        except ValueError:
            retry_after = 5.0
        # Which limit was hit (tokens, requests, or the provider's own capacity) is
        # only in the body. Without it, a 429 cannot be told apart from our quota.
        detail = str(_error_body(r).get("message", ""))[:300]
        log.warning("429 from provider (retry-after %.1fs): %s", retry_after, detail)
        raise RateLimited(retry_after, headers, detail)
    if r.status_code >= 500:
        raise TransientError(f"HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400:
        err = _error_body(r)
        if err.get("code") in MODEL_OUTPUT_CODES:
            raise ModelOutputError(err["code"], err.get("message", ""),
                                   err.get("failed_generation"))
        raise PermanentError(r.status_code, r.text)


def _raise_stream_error(err: dict, emitted: bool) -> None:
    """An error sent inside the stream (Groq reports e.g. tool_use_failed this way)."""
    code, message = err.get("code"), str(err.get("message", ""))[:300]
    if code in MODEL_OUTPUT_CODES:
        raise ModelOutputError(code, message, err.get("failed_generation"))
    if emitted:
        raise StreamInterrupted(message or str(code))
    raise TransientError(f"stream error {code}: {message}")
