"""Client for the LLM gateway. Agents hold this, never a provider API key."""

import json
from collections.abc import Awaitable, Callable

import httpx
from contracts import ChatMessage, ChatRequest, ChatResponse, Lane, Tier

from agentkit.auth import bearer_for


class GatewayError(Exception):
    def __init__(self, status: int, detail: object) -> None:
        super().__init__(f"gateway HTTP {status}: {detail}")
        self.status, self.detail = status, detail


class GatewayClient:
    def __init__(self, base_url: str, caller: str, lane: Lane = Lane.pipeline,
                 timeout_s: float = 600.0) -> None:
        # Long timeout on purpose: the gateway may legitimately queue a request while
        # it waits for quota. Waiting is its job; callers should not give up first.
        # Signed per request with this process's identity; the gateway records the
        # proven identity in the ledger, not the self-declared `caller`.
        self.http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, auth=bearer_for())
        self.caller, self.lane = caller, lane

    async def chat(self, messages: list[ChatMessage], *, tier: Tier = Tier.small,
                   tools: list[dict] | None = None, max_tokens: int = 1024,
                   reasoning_effort: str | None = "low", **kw) -> ChatResponse:
        req = ChatRequest(messages=messages, tools=tools, tier=tier, lane=self.lane,
                          caller=self.caller, max_tokens=max_tokens,
                          reasoning_effort=reasoning_effort, **kw)
        r = await self.http.post("/v1/chat", json=req.model_dump(mode="json", exclude_none=True))
        if r.status_code != 200:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text
            raise GatewayError(r.status_code, detail)
        return ChatResponse.model_validate(r.json())

    async def chat_stream(self, messages: list[ChatMessage],
                          on_delta: Callable[[str], Awaitable[None]], *,
                          tier: Tier = Tier.small, tools: list[dict] | None = None,
                          max_tokens: int = 1024, reasoning_effort: str | None = "low",
                          **kw) -> ChatResponse:
        """Like `chat`, but the answer text is handed to `on_delta` as it is written.
        Returns the same ChatResponse at the end; raises the same GatewayError."""
        req = ChatRequest(messages=messages, tools=tools, tier=tier, lane=self.lane,
                          caller=self.caller, max_tokens=max_tokens,
                          reasoning_effort=reasoning_effort, **kw)
        async with self.http.stream("POST", "/v1/chat/stream",
                                    json=req.model_dump(mode="json", exclude_none=True)) as r:
            if r.status_code != 200:
                raw = await r.aread()
                try:
                    detail = json.loads(raw)
                except ValueError:
                    detail = raw.decode("utf-8", "replace")
                raise GatewayError(r.status_code, detail)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:])
                if event["type"] == "delta":
                    await on_delta(event["text"])
                elif event["type"] == "done":
                    return ChatResponse.model_validate(event["response"])
                elif event["type"] == "error":
                    raise GatewayError(event["status"], event["body"])
        raise GatewayError(502, "stream ended without a result")

    async def aclose(self) -> None:
        await self.http.aclose()
