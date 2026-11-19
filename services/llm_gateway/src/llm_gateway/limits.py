"""Admission control against Groq's per-model limits, shared through Redis.

Why Redis and a Lua script: several agents (and later several gateway replicas)
draw on the same quota. Check-then-deduct in two round trips would let two
callers both see "1 left" and both spend it. A Lua script runs atomically
inside Redis, so the check and the deduction are one step.

Per model we track:
  * two token buckets for the minute windows (requests, tokens), refilled
    continuously, which matches Groq's rolling windows;
  * for models that have one, a third bucket for output tokens per minute. Groq
    charges it with the *requested* max_tokens, not the tokens generated (its 429
    says "Used 897, Requested 900"), so we charge it the same way and never refund;
  * two daily counters keyed by UTC date (Groq resets at midnight UTC);
  * a cooldown set when Groq itself returns 429, because our view of the quota
    can drift from theirs.

Token cost is estimated before the call and reconciled with real usage after.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum

from redis.asyncio import Redis

from llm_gateway.config import ModelLimits

ACQUIRE_LUA = """
-- KEYS: req_bucket, tok_bucket, day_req, day_tok, cooldown, out_bucket
-- ARGV: now, cost, rpm, tpm, rpd, tpd, share, otpm (0 = none), out_cost
local now   = tonumber(ARGV[1])
local cost  = tonumber(ARGV[2])
local rpm   = tonumber(ARGV[3])
local tpm   = tonumber(ARGV[4])
local rpd   = tonumber(ARGV[5])
local tpd   = tonumber(ARGV[6])
local share = tonumber(ARGV[7])
local otpm  = tonumber(ARGV[8])
local out_cost = tonumber(ARGV[9])

local until_ts = tonumber(redis.call('GET', KEYS[5]) or '0')
if until_ts > now then
  return {1, tostring(until_ts - now)}
end

local day_req = tonumber(redis.call('GET', KEYS[3]) or '0')
local day_tok = tonumber(redis.call('GET', KEYS[4]) or '0')
if day_req + 1 > rpd * share or day_tok + cost > tpd * share then
  return {2, '0'}
end

local function refill(key, cap, rate)
  local b = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(b[1]) or cap
  local ts = tonumber(b[2]) or now
  tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
  return tokens
end

local r_rate, t_rate = rpm / 60.0, tpm / 60.0
local r_tokens = refill(KEYS[1], rpm, r_rate)
local t_tokens = refill(KEYS[2], tpm, t_rate)

local wait = 0
if r_tokens < 1 then wait = math.max(wait, (1 - r_tokens) / r_rate) end
if t_tokens < cost then wait = math.max(wait, (cost - t_tokens) / t_rate) end
local o_tokens, o_rate = 0, 0
if otpm > 0 then
  o_rate = otpm / 60.0
  o_tokens = refill(KEYS[6], otpm, o_rate)
  if o_tokens < out_cost then wait = math.max(wait, (out_cost - o_tokens) / o_rate) end
end

if wait > 0 then
  redis.call('HSET', KEYS[1], 'tokens', r_tokens, 'ts', now)
  redis.call('HSET', KEYS[2], 'tokens', t_tokens, 'ts', now)
  if otpm > 0 then redis.call('HSET', KEYS[6], 'tokens', o_tokens, 'ts', now) end
  return {1, tostring(wait)}
end

redis.call('HSET', KEYS[1], 'tokens', r_tokens - 1, 'ts', now)
redis.call('HSET', KEYS[2], 'tokens', t_tokens - cost, 'ts', now)
if otpm > 0 then redis.call('HSET', KEYS[6], 'tokens', o_tokens - out_cost, 'ts', now) end
redis.call('INCRBY', KEYS[3], 1)
redis.call('INCRBY', KEYS[4], cost)
redis.call('EXPIRE', KEYS[3], 172800)
redis.call('EXPIRE', KEYS[4], 172800)
return {0, '0'}
"""

# Raise our counters to match what Groq reports, never lower them.
SYNC_LUA = """
-- KEYS: day_req, tok_bucket   ARGV: used_requests_today, remaining_minute_tokens
local used = tonumber(ARGV[1])
if used >= 0 then
  local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
  if used > cur then redis.call('SET', KEYS[1], used, 'EX', 172800) end
end
local rem = tonumber(ARGV[2])
if rem >= 0 then
  local cur = tonumber(redis.call('HGET', KEYS[2], 'tokens') or '-1')
  if cur < 0 or rem < cur then redis.call('HSET', KEYS[2], 'tokens', rem) end
end
return 1
"""


class Admission(IntEnum):
    granted = 0
    wait = 1
    exhausted = 2  # this lane's share of the daily budget is spent: try another model


@dataclass(frozen=True)
class Decision:
    admission: Admission
    wait_s: float = 0.0


def estimate_tokens(text_chars: int, max_tokens: int) -> int:
    """~4 characters per token, plus the full completion allowance. Deliberately pessimistic."""
    return text_chars // 4 + max_tokens


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class RateLimiter:
    def __init__(self, redis: Redis, limits: dict[str, ModelLimits]) -> None:
        self.redis = redis
        self.limits = limits
        self._acquire = redis.register_script(ACQUIRE_LUA)
        self._sync = redis.register_script(SYNC_LUA)

    @staticmethod
    def _keys(model: str, day: str | None = None) -> list[str]:
        d = day or _today()
        return [
            f"rl:{model}:req",
            f"rl:{model}:tok",
            f"rl:{model}:day:{d}:req",
            f"rl:{model}:day:{d}:tok",
            f"rl:{model}:cooldown",
            f"rl:{model}:out",
        ]

    async def acquire(self, model: str, cost: int, share: float, now: float,
                      max_output: int = 0) -> Decision:
        lim = self.limits[model]
        code, wait = await self._acquire(
            keys=self._keys(model),
            args=[now, cost, lim.rpm, lim.tpm, lim.rpd, lim.tpd, share, lim.otpm or 0,
                  max_output],
        )
        return Decision(Admission(int(code)), float(wait))

    async def reconcile(self, model: str, estimated: int, actual: int) -> None:
        """Correct the token buckets once real usage is known. Can go negative: that is debt."""
        delta = actual - estimated
        if delta == 0:
            return
        k = self._keys(model)
        async with self.redis.pipeline(transaction=True) as p:
            p.hincrbyfloat(k[1], "tokens", -delta)
            p.incrby(k[3], delta)
            await p.execute()

    async def sync_from_headers(self, model: str, headers: dict[str, str]) -> None:
        """Groq's own view wins when it is stricter than ours."""
        lim = self.limits[model]
        try:
            rem_req = int(headers.get("x-ratelimit-remaining-requests", "-1"))
            rem_tok = int(float(headers.get("x-ratelimit-remaining-tokens", "-1")))
        except ValueError:
            return
        used = lim.rpd - rem_req if rem_req >= 0 else -1
        k = self._keys(model)
        await self._sync(keys=[k[2], k[1]], args=[used, rem_tok])

    async def cooldown(self, model: str, seconds: float, now: float) -> None:
        await self.redis.set(self._keys(model)[4], now + seconds, ex=max(1, int(seconds) + 1))

    async def usage(self, model: str) -> dict:
        k = self._keys(model)
        req, tok = await self.redis.mget(k[2], k[3])
        lim = self.limits[model]
        return {
            "model": model,
            "requests_today": int(req or 0),
            "requests_per_day": lim.rpd,
            "tokens_today": int(tok or 0),
            "tokens_per_day": lim.tpd,
        }
