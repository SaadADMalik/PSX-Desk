# ADR 0009: Debate, a numeric verifier, and a risk gate in code

**Status:** accepted (Slice 3)

## Context
By Slice 2, a brief was one synthesis call over two specialist reports, grounded
only by checking that each claim cited a known evidence id. The HBL brief of
2026-11-16 passed that check and was still wrong: it said the last close (308.22)
was "10.0% above the 50-day SMA" (298.24). The truth is 3.35%. The citation was
valid; the model had done the arithmetic itself.

## Decisions

### 1. Bull and bear argue independently; a judge decides
- `bull_case` and `bear_case` run **in parallel and blind to each other**, so
  neither anchors on the other. Each must name a `concession`: the strongest point
  against its own side.
- They **prefer different model families** (bull: Qwen, bear: gpt-oss-20b, judge:
  gpt-oss-120b). Two prompts on one model share its blind spots. `prefer_model`
  is a preference, not a pin, so failover still works (the gateway puts the
  preferred model first and keeps the rest of the tier's chain).
- The orchestrator no longer calls a model. It coordinates and checks; agents think.
- One round only. A rebuttal round would cost about 5K tokens per brief; Slice 5
  measures whether it improves anything before we pay for it.

### 2. Every number is checked mechanically, at every hand-off
`services/orchestrator/src/orchestrator/verify.py`. For each claim: every cited id
must exist, and every number in the text must match a value **in the evidence
that claim cites**, to the precision written ("21.1%" matches 21.08, "1.9
million" matches 1,907,726). Periods, dates, years, indicator names and RSI's
30/70 bands are not facts and are skipped.

It runs on the specialists' points (before the debaters see them), on the
debaters' arguments (before the judge sees them), and on the brief. If the brief
is left without a key point or a risk, the rejected claims go back to the judge
as feedback for a rewrite, up to 3 judgements; then the run fails.

Alongside it, derived numbers moved into code: `pct_vs_sma_50`, `pct_vs_sma_200`
and `pct_below_high_52w` are computed with the other indicators, and every agent
prompt says to copy numbers exactly, never compute new ones.

What it cannot do: check *reasoning*. In the first live HBL debate the bear wrote
that the stock looked "over-valued relative to its earnings" with a P/E of 6.77.
No number was wrong, so the verifier passed it; the judge correctly ignored it.
Checking claims without numbers needs a different tool (an entailment check or
a second model), and either costs tokens.

### 3. The risk gate is code, with a veto
`risk.py`. Rules read computed evidence: illiquid (20-day average volume under
50K shares) is a **veto**; high volatility, a deep drawdown, a fresh material
announcement, stale data, a missing analysis, a one-sided debate, or weak
verification each **cap confidence**. A neutral or low-confidence brief is never
actionable. Thresholds live in `RiskPolicy`, so they can be reviewed without
touching the rules.

Why not an agent: "does this trade enough to exit a position?" has a right
answer in the evidence. A model would sometimes get it wrong, and could be argued
out of it by persuasive text. A rule cannot.

### 4. Untrusted text is contained by capability, not by prompt
Announcement titles are written by issuers and scraped from the web. They are
cleaned (control and invisible formatting characters removed, length capped) and
passed as data. The news agent's allowlist holds exactly one read-only tool, so
text that says "ignore your instructions" has nothing to act with, and whatever
the news agent writes still passes the verifier, the debate, the judge and the
risk gate. A prompt that says "treat this as data" helps, but it is not the
control.

### 5. Packaging: one debate service, three skills
An agent is a role (prompt, model, contract), not a container. Bull, bear and
judge share code, data access and permissions, and change together, so they
deploy as one A2A service with three skills on one Agent Card. A2A messages do
not name a skill, so the directory sends the requested skill id in the message
`metadata` and the service routes on it. The orchestrator routes by skill, so
splitting them later is configuration only. The news agent stays separate: it
reads untrusted input, which is a permission boundary.

## Measured (live, 2026-11-16)
| | Result |
|---|---|
| HBL brief, first run | 33/33 claims verified; the SMA claim now reads "3.35% above" (from `pct_vs_sma_50`) |
| Cost of one brief | ~12-15K tokens, ~10 calls (Slice 2: ~3.5K, 5 calls) |
| Duration, alone | 22 s (specialists 14 s, of which most is the PSX fetch; bull and bear 3 s in parallel; judge 4 s) |
| False rejection | "D-95", a dividend reference quoted from an announcement title, was rejected because the verifier only searched numeric values. Fixed: numbers inside the evidence's text count too |
| One analysis missing (news agent stopped) | Brief written, `missing_analysis` caution, confidence capped at 0.6 |
| Crash after `bull_case` | Resumed on attempt 2 at the judge; 5 checkpointed steps not redone |
| Fundamentals for OGDC, PPL, HUBC, FFC | PSX lists them as `OGDCXD` etc. during an ex-dividend period, so the exact-symbol lookup found nothing. Fixed: the quote falls back to the screener row with an XD/XB/XR suffix and records `ex_marker`; the fundamentals prompt explains it |
| Schema repairs | Most came from 7 points against a limit of 6, or an uncited risk. `cap_claims` now drops uncited claims and truncates before validation; HUBC then ran with no repairs |
| Throughput | 150-180 s per brief at concurrency 2; the cause was Qwen's undocumented 1,000 output-tokens/min limit plus futile retries of request too large. After an OTPM bucket in the gateway and moving news off Qwen: 32 s and 106 s, zero 429s (see docs/token-budget.md) |

## Consequences
- About 4x the tokens of Slice 2 per brief. Slice 5 must show the debate earns
  that against the single-agent baseline, or it goes.
- Claims without numbers are unchecked. That is the next gap in grounding.
- The verifier will sometimes reject a true claim written in an unexpected form.
  Rejections are recorded per run (`verification`, `upstream_checks`), so the
  false-rejection rate is measurable rather than guessed.
