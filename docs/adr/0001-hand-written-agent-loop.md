# ADR 0001: Hand-written agent loop, no agent framework

**Status:** accepted (Slice 0)

## Context
Frameworks (LangGraph, CrewAI, OpenClaw, ADK) give an agent loop, tool calling,
memory and multi-agent patterns out of the box. The goal of this project is to
understand those mechanics well enough to explain and debug them.

## Decision
`libs/agentkit/loop.py` is our own ~130-line loop: call the model, run requested
tools, append results, repeat, with guard rails (step cap, token budget, tool
errors returned to the model, one correction for invalid turns).

## Consequences
- Every behaviour is visible and testable (`tests/test_agentkit.py`).
- We write things a framework would give us for free (checkpointing, graph
  routing). Those are exactly the parts we want to understand.
- **What would change our mind:** if the project became a product rather than a
  learning vehicle, a framework's ecosystem would be worth its opacity.
