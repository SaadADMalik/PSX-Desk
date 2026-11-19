# ADR 0007: Central orchestration with graceful partial failure

**Status:** accepted

## Context
A brief needs several specialist analyses. Either one coordinator calls them
(orchestration) or agents react to each other's events (choreography).

## Decision
- **Orchestration** (`services/orchestrator/desk.py`): one place owns the flow:
  fan out to specialists in parallel, then a single synthesis model call.
- Synthesis is one model call, not an agent loop: it needs no tools.
- **Partial failure is a normal outcome.** A failed or unreachable specialist is
  recorded, the synthesis model is told what is missing, and the gap is written
  into `data_gaps`. Only when every specialist fails is the request an error (503).
- **Every hop checks what it receives:** specialists drop points citing evidence
  they never received; the orchestrator rejects reports for a different request or
  symbol, and drops synthesis claims citing evidence no specialist produced.
- **Priority travels with the work:** `lane` is part of `AnalysisRequest`, so a
  human-triggered brief makes every downstream model call `interactive`.

## Measured (LUCK, 2026-11-16)
| | Baseline (1 agent) | Desk (2 specialists + synthesis) |
|---|---|---|
| Tokens | 2,652 | 4,529 (1.7×) |
| Wall time, cold data | 18.9 s (ENGRO; not measured for LUCK) | 16 s |
| Wall time, warm caches | – | 5.8 s, 0 tokens |
| Invented company name | yes ("Lucknow Cement") | no |
| Peer/sector claims without data | yes | no |
| Disagreement visible | n/a | yes (technical bullish, fundamentals neutral) |

The quality gains came mostly from *narrower context and narrower prompts per
specialist*, which is why each analysis is its own agent with its own tools.

## Observed bottleneck
The two specialists run in parallel but share one MCP server whose polite PSX
limiter allows one fetch at a time, so `get_valuation` queued 12.5 s behind a cold
price-history fetch. Upstream parallelism is capped by the narrowest shared
resource. Accepted: the limit protects PSX; warm caches remove it.

## Consequences
- The flow is easy to read and to trace (one trace, five services).
- The orchestrator is a single point of coordination; each step is durable and
  idempotent (ADR 0008), so a crash resumes rather than repeats.
