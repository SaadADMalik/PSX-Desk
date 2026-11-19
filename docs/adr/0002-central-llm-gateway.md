# ADR 0002: Every model call goes through one LLM gateway

**Status:** accepted

## Context
The LLM provider's limits are per organisation and per model (about 30 req/min,
8K tokens/min, 1K req/day, 200K tokens/day). With many agents calling directly,
each would need the API key, and none would know what the others had spent.

## Options
1. Each agent calls the provider directly. Simple; no shared view of quota; key everywhere.
2. LiteLLM proxy. Mature; does most of this; hides how it works.
3. Our own small gateway. More code; every mechanism is ours to explain.

## Decision
Option 3: `services/llm_gateway`. It owns the key, rate limits, priority lanes,
fallback, circuit breakers, response cache and the `llm_calls` ledger. Agents ask
for a *tier* (`small`/`large`), never a vendor model id.

## Consequences
- Swapping or adding providers touches one service.
- The gateway is a single point of failure. Acceptable locally; in production it
  would run as several replicas (limiter state is already in Redis for that reason).
- Failure classes are explicit: 429 → wait or fall back; 5xx/timeout → backoff then
  breaker; `tool_use_failed` → 422 back to the caller, who has the context to fix
  it; other 4xx → fail fast.
- **What would change our mind:** needing many providers or features like
  budgets per team: adopt LiteLLM and keep our contracts in front of it.
