# ADR 0006: MCP between agents and tools, A2A between agents

**Status:** accepted

## Context
Agents need data (prices, valuations) and need to hand work to each other. Both
could be plain REST. The question is which interactions deserve which protocol.

## Decision
| Interaction | Protocol | Why |
|---|---|---|
| Agent → data/tools | **MCP** (`mcp_servers/market_data`) | The consumer is a model. MCP gives it a discoverable list of tools with JSON schemas, descriptions and read-only hints, in a format any MCP client understands. |
| Orchestrator → specialist | **A2A** (`agentkit/a2a.py`) | The consumer is another agent. A2A gives a self-describing Agent Card (skills, version, endpoint) and a task lifecycle (submitted/working/completed/failed/rejected) for work that takes seconds to minutes and can fail. |

Routing is by **skill id**, read from cards at startup (`AgentDirectory`), not by
hostname. The orchestrator is configured with addresses only.

## Details that matter (found while building)
- **Tracing travels differently.** A2A rides HTTP, so trace context is the
  `traceparent` header. MCP puts it in the JSON-RPC `_meta` field, because MCP also
  runs over stdio, which has no headers. The MCP SDK uses `httpx2`, which our httpx
  instrumentation does not touch, so header-based propagation would have silently
  broken the trace.
- **A2A data parts are protobuf `Value`s:** integers come back as floats. Evidence
  ids are therefore assigned once at the source and only ever carried.
- **MCP error masking:** unexpected exceptions reach the client only as
  "Error executing tool X". Anticipated failures raise `ToolError` so the model
  gets a useful message without internals leaking.
- **MCP DNS-rebinding guard:** the server only accepts known `Host` headers;
  others get 421. Inside Docker the allowed name is `market-data:8000`.
- **Stateless MCP over HTTP:** no session to lose on restart; any replica can answer.

## Consequences
- Each specialist sees only its allowlisted MCP tools (technical: price history;
  fundamentals: valuation).
- A2A task state is in memory per agent; a restarted agent forgets its tasks.
  Durability belongs to the orchestrator's workflow (ADR 0008).
- Every call between services is authenticated with signed, scoped tokens
  (ADR 0011); services are reachable only inside the Compose network and on 127.0.0.1.
- **What would change our mind:** if every agent were ours and in one language
  forever, plain function calls or REST would be simpler. A2A earns its keep when
  agents are owned, deployed or built separately.
