# ADR 0010: Evidence by reference, and long-term memory

**Status:** accepted

## Context
Reports carried their evidence inline. Every hop copied the facts, A2A turned
integers into floats on the way, and the orchestrator verified numbers against
whatever copy an agent sent it. The desk also forgot each brief as soon as it
was written.

## Decisions

### Evidence is stored at its source and passed by id
- The market-data MCP server writes every record it computes to an `evidence`
  table **before** returning it, so nothing can be cited that cannot be looked up.
  Ids are content hashes, so writes are idempotent.
- `AnalysisReport` v2 carries `evidence_ids` only. Anyone who needs the values
  calls the `get_evidence` MCP tool, and gets them exactly as computed.
- The verifier checks the brief against the source's copy, not an agent's. A
  buggy or compromised agent cannot change a fact on its way to the check.

### One owner per table
`market_data` (role) owns `evidence`; `memory` owns `memories`. Each role can
SELECT and INSERT its own table and nothing else; there is no UPDATE or DELETE.
Every other service goes through the owner's MCP tools. That keeps each service
replaceable, and the grants show at a glance who can write what.

### Long-term memory: an MCP server over pgvector
- `recall(query, symbol?, k)` and `remember(key, ...)`, append-only and
  idempotent by key (`brief:<run_id>`).
- Embeddings run locally (fastembed, `BAAI/bge-small-en-v1.5`, 384 dims, CPU),
  because the LLM provider has no embeddings endpoint and a local model costs no quota. The
  model is downloaded when the image is built, never at runtime.
- pgvector inside the existing Postgres rather than a separate vector database:
  one less system, and memories sit next to the runs they came from. No ANN index
  yet; an exact scan is fast (and exact) at this size.
- Only the **judge** recalls, top 3 for the symbol. Bull and bear argue from
  today's evidence only.

### Memory is a poisoning risk, so it is fenced
A wrong conclusion stored today becomes "context" tomorrow. So:
- only the orchestrator writes, and only after the verifier and the risk gate;
- prior views are labelled context, not evidence: they have no evidence id, so
  the verifier would reject any claim that cited one;
- memory is best effort: if it is down, the brief is still written and the
  `remember` step is recorded as failed.

Memory calls are authenticated like every other service (ADR 0011): only the
orchestrator may `remember`; agents may `recall`.

## Measured (live, 2026-11-16)
- Second HBL brief: the judge received the first brief as a prior view. Every other
  call was a cache hit; only the judge made a model call (4.4K tokens), **because
  the prior view changed its prompt**. Memory and caching pull against each other.
- Memory stopped mid-run (ENGRO): brief succeeded, `remember` failed. The first
  version recorded the error as "ExceptionGroup: unhandled errors in a TaskGroup";
  step errors now unwrap exception groups to the real cause.

## Consequences
- A brief whose `remember` step failed is never written to memory later. Writing
  memory from a `brief.completed` event consumer (at-least-once, deduped by key)
  would fix that; deferred until it matters.
- Evidence rows accumulate (about 3 per brief); retention is a later decision.
