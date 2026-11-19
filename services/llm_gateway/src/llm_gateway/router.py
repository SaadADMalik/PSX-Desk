"""The gateway's decision logic, independent of HTTP so it can be tested directly.

For each request:

  1. cache hit?                       -> return it, zero quota spent
  2. estimated size over the per-call cap? -> reject (caller must shrink context)
  3. for each model in the tier's fallback chain:
       circuit open?                  -> skip
       admit against shared quota     -> wait if the lane allows, else next model
       call Groq
         429        -> cool the model down; retry after `retry-after` if short, else next model
         timeout/5xx -> exponential backoff with jitter, up to N attempts, then next model
         tool_use_failed -> return 422 to the caller, who can correct the model
         other 4xx  -> stop: retrying or switching will not fix a bad request
         ok         -> reconcile tokens, sync quota from headers, cache, ledger
  4. nothing worked                   -> 503 with every model's reason

Streaming (`on_delta`) uses the same loop. Retries and fallback still happen, but only
before the first word reaches the caller; once text has been sent the model is
committed, and a broken stream is an error rather than a silent retry.
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable

from contracts import ChatMessage, ChatRequest, ChatResponse, ToolCall, Usage

from llm_gateway.breaker import CircuitBreaker
from llm_gateway.cache import ResponseCache, cache_key, cacheable
from llm_gateway.config import (
    EXTRA_PARAMS,
    LANE_MAX_WAIT_S,
    LANE_SHARE,
    REASONING_EFFORT_MODELS,
    ModelLimits,
    Settings,
)
from llm_gateway.ledger import Ledger, LedgerRow
from llm_gateway.limits import Admission, RateLimiter, estimate_tokens
from llm_gateway.provider import (
    GroqProvider,
    ModelOutputError,
    OnDelta,
    PermanentError,
    RateLimited,
    TransientError,
)

log = logging.getLogger(__name__)


class ContextTooLarge(Exception):
    def __init__(self, estimated: int, cap: int) -> None:
        super().__init__(f"request estimated at {estimated} tokens; per-call cap is {cap}")
        self.estimated, self.cap = estimated, cap


class AllModelsFailed(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class Gateway:
    def __init__(
        self,
        provider: GroqProvider,
        limiter: RateLimiter,
        breaker: CircuitBreaker,
        cache: ResponseCache,
        ledger: Ledger,
        settings: Settings,
        limits: dict[str, ModelLimits],
        tiers: dict,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider, self.limiter, self.breaker = provider, limiter, breaker
        self.cache, self.ledger, self.settings = cache, ledger, settings
        self.limits, self.tiers = limits, tiers
        self.clock, self.sleep = clock, sleep

    def candidates(self, req: ChatRequest) -> list[str]:
        if req.model:
            if req.model not in self.limits:
                raise ValueError(f"unknown model {req.model!r}")
            return [req.model]
        chain = list(self.tiers[req.tier])
        if req.prefer_model:
            if req.prefer_model not in self.limits:
                raise ValueError(f"unknown model {req.prefer_model!r}")
            # Preference, not a pin: the preferred model goes first, failover still works.
            chain = [req.prefer_model, *(m for m in chain if m != req.prefer_model)]
        return chain

    def payload(self, req: ChatRequest, model: str) -> dict:
        p: dict = {
            "model": model,
            "messages": [m.model_dump(mode="json", exclude_none=True) for m in req.messages],
            "max_completion_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        if req.tools:
            p["tools"] = req.tools
        if req.response_format:
            p["response_format"] = req.response_format
        if req.reasoning_effort and model in REASONING_EFFORT_MODELS:
            p["reasoning_effort"] = req.reasoning_effort
        p.update(EXTRA_PARAMS.get(model, {}))
        return p

    def backoff(self, attempt: int) -> float:
        """Exponential with full jitter: spreads retries so callers do not stampede together."""
        return random.uniform(0, min(20.0, 1.0 * 2**attempt))

    async def _admit(self, model: str, cost: int, req: ChatRequest) -> str | None:
        """Returns None when admitted, otherwise why this model cannot take the request."""
        share, max_wait = LANE_SHARE[req.lane], LANE_MAX_WAIT_S[req.lane]
        waited = 0.0
        while True:
            d = await self.limiter.acquire(model, cost, share, self.clock(),
                                           max_output=req.max_tokens)
            if d.admission is Admission.granted:
                return None
            if d.admission is Admission.exhausted:
                return f"{model}: {req.lane.value} lane's daily share used up"
            if waited + d.wait_s > max_wait:
                return f"{model}: needs {d.wait_s:.1f}s more wait, lane allows {max_wait:.0f}s"
            await self.sleep(d.wait_s + 0.05)
            waited += d.wait_s

    async def complete(self, req: ChatRequest, trace_id: str | None = None,
                       on_delta: OnDelta | None = None) -> ChatResponse:
        started = time.monotonic()
        requested = req.model or req.tier.value

        def row(**kw) -> LedgerRow:
            base = {"trace_id": trace_id, "caller": req.caller, "lane": req.lane.value,
                        "requested": requested, "model": None, "outcome": "error", "attempts": 0,
                        "prompt_tokens": 0, "completion_tokens": 0, "cached": False,
                        "latency_ms": int((time.monotonic() - started) * 1000)}
            return LedgerRow(**{**base, **kw})

        key = cache_key(req) if cacheable(req) else None
        if key and (hit := await self.cache.get(key)):
            await self.ledger.record(row(model=hit.model, outcome="cached", cached=True))
            if on_delta and hit.message.content:
                await on_delta(hit.message.content)
            return hit

        size = len(json.dumps([m.model_dump(mode="json") for m in req.messages]))
        size += len(json.dumps(req.tools or []))
        est = estimate_tokens(size, req.max_tokens)
        if est > self.settings.max_tokens_per_call:
            await self.ledger.record(row(error="context too large"))
            raise ContextTooLarge(est, self.settings.max_tokens_per_call)

        reasons: list[str] = []
        attempts = 0
        for model in self.candidates(req):
            if not self.breaker.allow(model):
                reasons.append(f"{model}: circuit open")
                continue
            otpm = self.limits[model].otpm
            if otpm is not None and req.max_tokens > otpm:
                reasons.append(f"{model}: max_tokens {req.max_tokens} exceeds its "
                               f"{otpm} output tokens/min")
                continue
            for attempt in range(1, self.settings.max_attempts_per_model + 1):
                if (why := await self._admit(model, est, req)) is not None:
                    reasons.append(why)
                    break
                attempts += 1
                try:
                    payload = self.payload(req, model)
                    data, headers = await (self.provider.chat_stream(payload, on_delta)
                                           if on_delta else self.provider.chat(payload))
                except RateLimited as e:
                    await self.limiter.reconcile(model, est, 0)
                    if e.too_large:
                        # Not a full quota: this request can never fit this model. Do not
                        # cool the model down (smaller requests still fit); move on.
                        reasons.append(f"{model}: request too large ({e.detail[:120]})")
                        break
                    # Our quota view drifted from Groq's. Learn, cool down.
                    await self.limiter.sync_from_headers(model, e.headers)
                    await self.limiter.cooldown(model, e.retry_after, self.clock())
                    last = attempt == self.settings.max_attempts_per_model
                    if e.retry_after > LANE_MAX_WAIT_S[req.lane] or last:
                        reasons.append(f"{model}: 429 (retry-after {e.retry_after:.0f}s)")
                        break
                    continue  # _admit will sleep out the cooldown
                except TransientError as e:
                    await self.limiter.reconcile(model, est, 0)
                    if attempt < self.settings.max_attempts_per_model:
                        await self.sleep(self.backoff(attempt))
                        continue
                    self.breaker.failure(model)
                    reasons.append(f"{model}: {e}")
                    break
                except ModelOutputError as e:
                    # Not the model being down, so no breaker penalty and no fallback:
                    # the caller holds the context needed to correct it.
                    await self.ledger.record(row(model=model, attempts=attempts, error=str(e)))
                    raise
                except PermanentError as e:
                    await self.limiter.reconcile(model, est, 0)
                    await self.ledger.record(row(model=model, attempts=attempts, error=str(e)))
                    raise
                else:
                    resp = self._parse(data, model, attempts)
                    await self.limiter.reconcile(model, est, resp.usage.total_tokens)
                    await self.limiter.sync_from_headers(model, headers)
                    self.breaker.success(model)
                    if key and resp.finish_reason != "length":
                        await self.cache.set(key, resp)
                    await self.ledger.record(row(
                        model=model, outcome="ok", attempts=attempts,
                        prompt_tokens=resp.usage.prompt_tokens,
                        completion_tokens=resp.usage.completion_tokens,
                    ))
                    return resp

        await self.ledger.record(row(attempts=attempts, error="; ".join(reasons)))
        raise AllModelsFailed(reasons)

    @staticmethod
    def _parse(data: dict, model: str, attempts: int) -> ChatResponse:
        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = [ToolCall(**tc) for tc in msg.get("tool_calls") or []] or None
        u = data.get("usage") or {}
        return ChatResponse(
            message=ChatMessage(role="assistant", content=msg.get("content"), tool_calls=tool_calls),
            model=model,
            usage=Usage(prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0)),
            attempts=attempts,
            finish_reason=choice.get("finish_reason"),
        )
