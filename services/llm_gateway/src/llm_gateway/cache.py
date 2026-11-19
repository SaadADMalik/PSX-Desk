"""Response cache. Same request -> same answer, at zero quota cost.

Only deterministic requests are cached (temperature 0). The key covers everything
that changes the answer: requested tier or model, messages, tools, and sampling
settings. Tool results are inside the messages, so new market data is a new key.
"""

import hashlib
import json

from contracts import ChatRequest, ChatResponse
from redis.asyncio import Redis

# prefer_model is part of the key: two agents asking different models the same
# question (bull and bear) must not share one cached answer.
CACHE_FIELDS = {"messages", "tools", "tier", "model", "prefer_model", "max_tokens",
                "temperature", "reasoning_effort", "response_format"}


def cache_key(req: ChatRequest) -> str:
    payload = req.model_dump(mode="json", include=CACHE_FIELDS, exclude_none=True)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"llmcache:{digest}"


def cacheable(req: ChatRequest) -> bool:
    return req.cache and req.temperature == 0.0


class ResponseCache:
    def __init__(self, redis: Redis, ttl_s: int) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    async def get(self, key: str) -> ChatResponse | None:
        raw = await self.redis.get(key)
        if raw is None:
            return None
        resp = ChatResponse.model_validate_json(raw)
        return resp.model_copy(update={"cached": True})

    async def set(self, key: str, resp: ChatResponse) -> None:
        await self.redis.set(key, resp.model_dump_json(), ex=self.ttl_s)
