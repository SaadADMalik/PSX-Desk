import pytest
from contracts import ChatMessage, ChatRequest, Lane, Tier
from llm_gateway.breaker import CircuitBreaker
from llm_gateway.cache import ResponseCache
from llm_gateway.config import ModelLimits, Settings
from llm_gateway.ledger import Ledger
from llm_gateway.limits import RateLimiter
from llm_gateway.provider import ModelOutputError, PermanentError, RateLimited, TransientError
from llm_gateway.router import AllModelsFailed, ContextTooLarge, Gateway

A, B = "model-a", "model-b"
LIMITS = {m: ModelLimits(rpm=30, tpm=8000, rpd=1000, tpd=200_000) for m in (A, B)}
TIERS = {Tier.small: [A, B], Tier.large: [A, B]}


def ok(content="hi", prompt=10, completion=5):
    return ({"choices": [{"message": {"role": "assistant", "content": content},
                          "finish_reason": "stop"}],
             "usage": {"prompt_tokens": prompt, "completion_tokens": completion}},
            {"x-ratelimit-remaining-requests": "999"})


class ScriptedProvider:
    """Plays back a script of outcomes per model."""

    def __init__(self, script: dict[str, list]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    async def chat(self, payload):
        model = payload["model"]
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Rows(Ledger):
    def __init__(self):
        super().__init__(None)
        self.rows = []

    async def record(self, row):
        self.rows.append(row)


def make(redis, clock, script, **settings):
    provider = ScriptedProvider(script)
    ledger = Rows()
    gw = Gateway(
        provider=provider,
        limiter=RateLimiter(redis, LIMITS),
        breaker=CircuitBreaker(failure_threshold=1, cooldown_s=60, clock=clock),
        cache=ResponseCache(redis, 3600),
        ledger=ledger,
        settings=Settings(_env_file=None, **settings),
        limits=LIMITS, tiers=TIERS, clock=clock, sleep=clock.sleep,
    )
    return gw, provider, ledger


def req(**kw):
    return ChatRequest(messages=[ChatMessage(role="user", content="hello")], **kw)


async def test_happy_path_records_ledger(redis, clock):
    gw, _provider, ledger = make(redis, clock, {A: [ok()]})
    resp = await gw.complete(req())
    assert resp.model == A and resp.message.content == "hi"
    assert ledger.rows[-1].outcome == "ok" and ledger.rows[-1].prompt_tokens == 10


async def test_cache_hit_costs_no_call(redis, clock):
    gw, provider, ledger = make(redis, clock, {A: [ok()]})
    await gw.complete(req())
    again = await gw.complete(req())
    assert again.cached and provider.calls == [A]
    assert ledger.rows[-1].outcome == "cached"


async def test_nonzero_temperature_is_not_cached(redis, clock):
    gw, provider, _ = make(redis, clock, {A: [ok(), ok()]})
    await gw.complete(req(temperature=0.7))
    await gw.complete(req(temperature=0.7))
    assert provider.calls == [A, A]


async def test_short_429_waits_and_retries_same_model(redis, clock):
    gw, provider, _ = make(redis, clock, {A: [RateLimited(3, {}), ok()]})
    resp = await gw.complete(req(lane=Lane.interactive))
    assert resp.model == A and provider.calls == [A, A]
    assert sum(clock.sleeps) >= 3


async def test_long_429_falls_back_to_next_model(redis, clock):
    gw, provider, _ = make(redis, clock, {A: [RateLimited(600, {})], B: [ok()]})
    resp = await gw.complete(req(lane=Lane.interactive))
    assert resp.model == B and provider.calls == [A, B]


async def test_transient_errors_retry_then_open_breaker_and_fall_back(redis, clock):
    boom = TransientError("503")
    gw, provider, _ = make(redis, clock, {A: [boom, boom, boom], B: [ok(), ok()]})
    assert (await gw.complete(req(cache=False))).model == B
    assert provider.calls == [A, A, A, B]
    # Breaker is now open for A: the next request goes straight to B.
    await gw.complete(req(cache=False))
    assert provider.calls[-1] == B and provider.calls.count(A) == 3


async def test_permanent_error_is_not_retried(redis, clock):
    gw, provider, _ = make(redis, clock, {A: [PermanentError(400, "bad tool schema")]})
    with pytest.raises(PermanentError):
        await gw.complete(req())
    assert provider.calls == [A]


async def test_bad_tool_use_goes_back_to_caller_without_fallback_or_breaker(redis, clock):
    bad = ModelOutputError("tool_use_failed", "called tool 'JSON'", "{...}")
    gw, provider, _ = make(redis, clock, {A: [bad], B: [ok()]})
    with pytest.raises(ModelOutputError):
        await gw.complete(req())
    assert provider.calls == [A]
    assert gw.breaker.state(A).value == "closed"


async def test_all_models_failing_reports_every_reason(redis, clock):
    gw, _, ledger = make(redis, clock, {A: [RateLimited(600, {})], B: [RateLimited(600, {})]})
    with pytest.raises(AllModelsFailed) as e:
        await gw.complete(req(lane=Lane.interactive))
    assert len(e.value.reasons) == 2 and ledger.rows[-1].outcome == "error"


async def test_oversized_request_rejected_before_any_call(redis, clock):
    gw, provider, _ = make(redis, clock, {A: [ok()]}, max_tokens_per_call=600)
    with pytest.raises(ContextTooLarge):
        await gw.complete(req(max_tokens=1000))
    assert provider.calls == []


async def test_pinned_unknown_model_is_rejected(redis, clock):
    gw, _, _ = make(redis, clock, {})
    with pytest.raises(ValueError):
        await gw.complete(req(model="nope"))


def test_payload_only_sends_params_the_model_supports(redis, clock):
    gw, _, _ = make(redis, clock, {})
    r = req(reasoning_effort="low")
    assert "reasoning_effort" in gw.payload(r, "openai/gpt-oss-20b")
    qwen = gw.payload(r, "qwen/qwen3.8-27b")
    assert "reasoning_effort" not in qwen and qwen["reasoning_format"] == "hidden"


async def test_preferred_model_goes_first_and_failover_still_works(redis, clock):
    gw, provider, _ = make(redis, clock, {B: [TransientError("503")] * 3, A: [ok()]})
    resp = await gw.complete(req(prefer_model=B))
    assert provider.calls == [B, B, B, A] and resp.model == A


async def test_different_preferred_models_do_not_share_a_cache_entry(redis, clock):
    gw, provider, _ = make(redis, clock, {A: [ok("from a")], B: [ok("from b")]})
    a = await gw.complete(req(prefer_model=A))
    b = await gw.complete(req(prefer_model=B))
    assert (a.message.content, b.message.content) == ("from a", "from b")
    assert provider.calls == [A, B]


async def test_request_too_large_for_a_model_moves_on_without_retrying(redis, clock):
    too_large = RateLimited(5.0, {}, "Request too large for model on output tokens per minute")
    gw, provider, _ = make(redis, clock, {A: [too_large], B: [ok()]})
    resp = await gw.complete(req())
    assert provider.calls == [A, B] and resp.model == B  # no retry of a request that can't fit


async def test_model_with_an_output_limit_is_skipped_when_max_tokens_exceeds_it(redis, clock):
    from llm_gateway.config import ModelLimits as ML

    gw, provider, _ = make(redis, clock, {A: [], B: [ok()]})
    gw.limits = {**gw.limits, A: ML(rpm=30, tpm=8000, rpd=1000, tpd=200_000, otpm=1000)}
    resp = await gw.complete(req(max_tokens=1500))
    assert provider.calls == [B] and resp.model == B
