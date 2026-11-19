"""Gateway configuration. Limits are data, not code, so they can follow Groq's changes."""

from dataclasses import dataclass

from contracts import Lane, Tier
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class ModelLimits:
    rpm: int  # requests per minute
    tpm: int  # tokens per minute
    rpd: int  # requests per day
    tpd: int  # tokens per day
    # Output tokens per minute. Not in Groq's rate-limit headers; found from a 429 body.
    # A request whose max_tokens exceeds it is rejected outright, so never send one.
    otpm: int | None = None


# Groq free tier, checked 2026-11-16 (console.groq.com/docs/rate-limits).
# Confirm against the limits page in your own account; it is authoritative.
DEFAULT_LIMITS: dict[str, ModelLimits] = {
    "openai/gpt-oss-120b": ModelLimits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000),
    "openai/gpt-oss-20b": ModelLimits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000),
    "qwen/qwen3.8-27b": ModelLimits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000, otpm=1_000),
}

# Models differ in which optional parameters they accept; sending an unsupported
# one is a 400. The gateway translates the generic request per model.
REASONING_EFFORT_MODELS = {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}
EXTRA_PARAMS: dict[str, dict] = {
    "qwen/qwen3.8-27b": {"reasoning_format": "hidden"},  # keep <think> out of content
}

# Fallback order per tier. Each model has its own quota, so falling back also
# spreads load across separate budgets.
DEFAULT_TIERS: dict[Tier, list[str]] = {
    Tier.large: ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
    Tier.small: ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
}

# Share of each model's DAILY budget a lane may consume. The pipeline can never
# eat the last 15% a human might need; backtests stop at half.
LANE_SHARE: dict[Lane, float] = {
    Lane.interactive: 1.00,
    Lane.pipeline: 0.85,
    Lane.backtest: 0.50,
}

# How long a request in each lane is willing to wait for minute-window capacity.
LANE_MAX_WAIT_S: dict[Lane, float] = {
    Lane.interactive: 45.0,  # the chat shows live progress, so a short wait beats a 503
    Lane.pipeline: 90.0,
    Lane.backtest: 300.0,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = ""  # empty -> ledger disabled (logged only)

    request_timeout_s: float = 60.0
    max_attempts_per_model: int = 3
    # A single call's estimated size must fit well inside the 8K tokens/min window,
    # or it can never be admitted. Rejecting early forces callers to manage context.
    max_tokens_per_call: int = 7_000
    cache_ttl_s: int = 7 * 24 * 3600

    breaker_failure_threshold: int = 3
    breaker_cooldown_s: float = 60.0
