# ADR 0001: Hand-written agent loop, no agent framework

**Status:** accepted

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
  routing), and in exchange we control and can explain every one of them.
- **What would change our mind:** if the team grew and needed a framework's ecosystem
  (integrations, hosted tracing) more than full control, it would be worth its opacity.
