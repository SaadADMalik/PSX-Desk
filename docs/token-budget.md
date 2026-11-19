# Daily token budget

Written before Slice 2 (the scheduled pipeline), from measured usage in the
`llm_calls` ledger on 2026-11-16. Re-measure whenever prompts or models change.

## Measured cost of one multi-agent brief

| Step | Model (tier) | Calls | Tokens |
|---|---|---|---|
| technical-agent | gpt-oss-20b (small) | 2 | ~1,600 |
| fundamentals-agent | gpt-oss-20b (small) | 2 | ~1,200 |
| schema repair (when needed) | gpt-oss-20b (small) | 0-1 | ~1,400 |
| synthesis | gpt-oss-120b (large) | 1 | ~1,600 |

Planning figure, assuming a repair in roughly one brief in three:
**small ≈ 3,200 tokens / 5 calls, large ≈ 1,600 tokens / 1 call.**

## What one day allows

Groq free tier per model: 200,000 tokens/day, 1,000 requests/day, 8,000 tokens/min.
The `pipeline` lane may use 85% of the daily budget (ADR 0003).

| Model | Pipeline share | Bound by | Briefs/day |
|---|---|---|---|
| gpt-oss-20b | 170K tokens, 850 req | tokens (170K / 3.2K) | **~53** |
| gpt-oss-120b | 170K tokens, 850 req | tokens (170K / 1.6K) | ~106 |

The small model is the binding constraint, because both specialists use it.

## The plan

- **Daily watchlist: 10 symbols.** About 32K small tokens (19% of the pipeline
  share) and 16K large. Leaves room for retries, interactive briefs and growth.
- **Throughput is set by tokens per minute, not per day.** The gateway admits on
  a pessimistic estimate (prompt plus the full `max_tokens` allowance), about 2.5K
  per specialist call, so two parallel specialist calls nearly fill one minute's
  8K window. Expect roughly one brief per minute; a 10-symbol run takes 10-15 minutes.
  The workflow engine therefore runs at most 2 briefs at a time.
- **Re-runs are nearly free**: identical prompts hit the gateway cache.

## Consequences for later slices

- **Backtests (Slice 5)** get 50% of the daily budget, ~31 replayed briefs a day on
  the small model, and replays of past dates are not cache hits. Backtests must
  sample dates, spread over several days, or move one specialist onto
  `qwen/qwen3.8-27b`, which has its own separate 200K budget.
- Slice 3 adds bull, bear and judge agents: see below.

## Slice 3: the debate desk (re-measured 2026-11-16)

Two scheduled briefs (LUCK, ENGRO) from the ledger, after the fixes below:

| Caller | Model | Calls per brief | Tokens per brief |
|---|---|---|---|
| technical-agent | gpt-oss-20b | 2 | ~1,600 (cache hit here: 0) |
| fundamentals-agent | gpt-oss-20b | 2 | ~1,400 |
| news-agent | gpt-oss-20b | 2 | ~1,700 |
| bull | qwen3.8-27b | 1 | ~2,700 |
| bear | gpt-oss-20b | 1 | ~2,300 |
| judge | gpt-oss-120b, fell back to 20b | 1 | ~4,400 |

**About 12-14K tokens and 9 calls per brief**, 4x Slice 2. Zero 429s, zero
verifier rejections, LUCK 32 s and ENGRO 106 s at engine concurrency 1.

At 85% pipeline share, gpt-oss-20b carries ~9K of that, so **~18 briefs/day** if
120b is unavailable, ~28 if the judge stays on 120b. The 10-symbol watchlist still
fits, with less headroom than before.

### Limits the documentation doesn't show

- **Qwen has an output-tokens-per-minute limit of 1,000**, found only in a 429 body
  ("Used 897, Requested 900"). Groq charges it with the *requested* `max_tokens`, not
  what is generated, so Qwen fits about one 900-token call a minute. The gateway now
  has an OTPM bucket charged the same way, and skips a model whose OTPM is below the
  request's `max_tokens`. Qwen serves only the bull; the news agent moved to 20b.
- **"Request too large" 429s are not rate limits to wait out.** Retrying the same
  model can never succeed, so the router moves on without cooling the model down.
- **The daily window is rolling, not UTC midnight.** 120b's 429 said "Used 196,600" of
  200,000 while the gateway's own daily counter showed ~38K, because ours resets at
  UTC midnight. The 429 still triggers a cooldown and failover, so this costs a
  fallback, not a failure. Reconciling the daily counter from 429 bodies is open.
- **Concurrency 2 did not help.** Briefs took 150-180 s each at 2, because parallel
  runs queued on the same per-minute buckets. The engine runs 1 at a time.
