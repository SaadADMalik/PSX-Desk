# ADR 0003: Rate limiting with Redis token buckets and lane shares

**Status:** accepted

## Context
Several callers share each model's quota. A check in one round trip and a deduction
in another lets two callers spend the same last unit. The provider's minute windows are
rolling; daily counters reset at midnight UTC.

## Decision
- One Lua script in Redis does check-and-deduct atomically for four limits: requests
  per minute and tokens per minute (token buckets, continuous refill), requests and
  tokens per day (UTC-dated counters).
- Token cost is estimated pessimistically before the call (chars/4 + max completion)
  and reconciled with real usage after it.
- The provider's `x-ratelimit-*` headers can raise our counters, never lower them. On a 429
  we refund the estimate, sync from headers and set a cooldown for `retry-after`.
- **Lanes:** `interactive` may use 100% of a model's daily budget, `pipeline` 85%,
  `backtest` 50%. Each lane also has a maximum wait for minute capacity.
- A request estimated above 7,000 tokens is rejected (413) before any call: it could
  never fit an 8K/min window, and rejecting forces callers to manage context.

## Consequences
- Correct under concurrency and across gateway replicas.
- Lanes are reserves, not a true priority queue: a waiting pipeline request is not
  pre-empted by a new interactive one. Good enough at this scale; a Redis sorted-set
  queue would be the next step.
- **What would change our mind:** sustained contention between lanes, visible in the
  ledger as interactive requests waiting.
