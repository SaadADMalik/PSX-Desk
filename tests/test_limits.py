from llm_gateway.config import ModelLimits
from llm_gateway.limits import Admission, RateLimiter, estimate_tokens

M = "m"
LIMITS = {M: ModelLimits(rpm=3, tpm=1000, rpd=10, tpd=5000)}


async def test_grants_until_minute_bucket_empty_then_waits(redis, clock):
    rl = RateLimiter(redis, LIMITS)
    for _ in range(3):
        assert (await rl.acquire(M, 100, 1.0, clock())).admission is Admission.granted
    d = await rl.acquire(M, 100, 1.0, clock())
    assert d.admission is Admission.wait
    assert 19 < d.wait_s <= 20.5  # 3 rpm -> one request refills every 20s


async def test_bucket_refills_over_time(redis, clock):
    rl = RateLimiter(redis, LIMITS)
    for _ in range(3):
        await rl.acquire(M, 100, 1.0, clock())
    clock.now += 21
    assert (await rl.acquire(M, 100, 1.0, clock())).admission is Admission.granted


async def test_token_bucket_limits_large_requests(redis, clock):
    rl = RateLimiter(redis, LIMITS)
    assert (await rl.acquire(M, 900, 1.0, clock())).admission is Admission.granted
    d = await rl.acquire(M, 900, 1.0, clock())
    assert d.admission is Admission.wait
    assert d.wait_s > 40  # needs 800 more tokens at 1000/60 per second


async def test_lane_share_caps_daily_budget(redis, clock):
    rl = RateLimiter(redis, LIMITS)
    granted = 0
    for _ in range(10):
        d = await rl.acquire(M, 10, 0.5, clock())
        if d.admission is Admission.exhausted:
            break
        assert d.admission is Admission.granted
        granted += 1
        clock.now += 30  # stay clear of the minute bucket
    assert granted == 5  # backtest lane gets half of rpd=10
    # A higher-priority lane can still use the remainder.
    assert (await rl.acquire(M, 10, 1.0, clock())).admission is Admission.granted


async def test_cooldown_after_429_forces_wait(redis, clock):
    rl = RateLimiter(redis, LIMITS)
    await rl.cooldown(M, 12, clock())
    d = await rl.acquire(M, 10, 1.0, clock())
    assert d.admission is Admission.wait and abs(d.wait_s - 12) < 0.01


async def test_reconcile_corrects_daily_tokens(redis, clock):
    rl = RateLimiter(redis, LIMITS)
    await rl.acquire(M, 500, 1.0, clock())
    await rl.reconcile(M, estimated=500, actual=200)
    assert (await rl.usage(M))["tokens_today"] == 200


async def test_headers_raise_but_never_lower_usage(redis, clock):
    rl = RateLimiter(redis, LIMITS)
    await rl.sync_from_headers(M, {"x-ratelimit-remaining-requests": "4"})
    assert (await rl.usage(M))["requests_today"] == 6
    await rl.sync_from_headers(M, {"x-ratelimit-remaining-requests": "9"})
    assert (await rl.usage(M))["requests_today"] == 6


def test_estimate_is_pessimistic():
    assert estimate_tokens(4000, 500) == 1500



async def test_output_token_bucket_is_charged_with_the_requested_max_tokens(redis, clock):
    from llm_gateway.config import ModelLimits

    rl = RateLimiter(redis, {M: ModelLimits(rpm=30, tpm=8000, rpd=1000, tpd=200_000,
                                            otpm=1000)})
    first = await rl.acquire(M, 100, 1.0, clock(), max_output=900)
    second = await rl.acquire(M, 100, 1.0, clock(), max_output=900)
    assert first.admission is Admission.granted
    # Only 100 output tokens left; 800 more refill at 1000/60 per second: ~48 s.
    assert second.admission is Admission.wait and 47 < second.wait_s < 49
