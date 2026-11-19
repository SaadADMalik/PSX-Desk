# ADR 0005: Models cite evidence; code produces every number

**Status:** accepted; enforced by the claim verifier

## Context
Models state plausible numbers and facts from memory. In a financial system, a
wrong number in a confident sentence is the main failure mode.

## Decision
- Indicators and valuation figures are computed in code (`libs/marketdata`) and
  wrapped as `Evidence` with a content-derived id, source and as-of date.
- The model sees `Evidence.prompt_view()`, which leaves out fetch metadata so that
  identical facts give an identical prompt (and therefore a cache hit).
- Every claim in a `Brief` must cite at least one evidence id (enforced by the schema).
- Cited ids must exist, and a deterministic verifier checks each figure in a claim
  against the cited evidence (ADR 0009).

## Observed in the first live run
The baseline cited real evidence ids, yet:
- named LUCK "Lucknow Cement" (it is Lucky Cement; no tool supplied a name);
- said P/E was "modest for the sector" and yield "low relative to peers" with no
  peer data;
- said the price was "near its 50-day SMA" when it was 8% above.

Citation-existence checks catch none of these. That is the evidence for the
claim verifier and the bear agent (ADR 0009).
