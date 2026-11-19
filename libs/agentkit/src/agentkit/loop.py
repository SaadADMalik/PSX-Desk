"""The agent loop, written by hand because this is the part worth understanding.

An "agent" is just this loop:

    send messages + tool specs to the model
    if the model asks for tools: run them, append results, go again
    otherwise: the model's text is the answer

Everything else is guard rails:

  * max_steps         stops a model that keeps calling tools forever
  * token_budget      stops a run that is burning quota without converging
  * tool errors       are returned to the model as tool output, not raised, so it
                      can recover (try another symbol, report a data gap)
  * unknown tools     same: the model is told, the process does not crash
  * bad JSON args     same
  * invalid turns     (the model calls a tool that does not exist, which the provider
                      rejects outright) get one corrective message, then fail

Live progress (`on_event`, optional): every model call streams, and the loop reports
what is happening as it happens:
  {"type": "delta", "text"}   answer text, as the model writes it
  {"type": "tool", "name", "args"}  about to run a tool
  {"type": "discard"}         text streamed this step was a preamble to tool calls,
                              not the answer (a UI drops it)
Without on_event nothing changes: one plain request per step.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from contracts import ChatMessage, Tier, Usage
from opentelemetry import trace

from agentkit.client import GatewayClient, GatewayError
from agentkit.tools import Tool, to_tool_content

log = logging.getLogger(__name__)
_tracer = trace.get_tracer("agentkit.loop")


# The gateway rejects any single call estimated above this (llm_gateway max_tokens_per_call).
GATEWAY_CALL_CAP = 7_000

OnEvent = Callable[[dict[str, Any]], Awaitable[None]]


class AgentLimitExceeded(Exception):
    pass


@dataclass
class AgentResult:
    text: str
    messages: list[ChatMessage]
    steps: int
    tool_calls: int
    usage: Usage = field(default_factory=Usage)
    models: list[str] = field(default_factory=list)
    cached_calls: int = 0


async def run_agent(
    client: GatewayClient,
    *,
    system: str,
    user: str,
    tools: list[Tool],
    tier: Tier = Tier.small,
    max_steps: int = 6,
    token_budget: int = 30_000,
    max_tokens: int = 1500,
    prefer_model: str | None = None,
    history: list[ChatMessage] | None = None,
    reasoning_effort: str | None = "low",
    on_event: OnEvent | None = None,
    context_cap: int | None = GATEWAY_CALL_CAP,
) -> AgentResult:
    """`history`: earlier turns of a conversation (user/assistant text only), placed
    between the system prompt and the new user message.

    `context_cap`: the gateway's per-call token cap. Tool results pile up step after
    step; before each call the context is shrunk to fit (see `fit_context`) instead of
    letting the gateway reject the whole run."""
    by_name = {t.name: t for t in tools}
    specs = [t.spec() for t in tools] or None
    messages = [ChatMessage(role="system", content=system), *(history or []),
                ChatMessage(role="user", content=user)]
    n_history = len(history or [])
    specs_chars = len(json.dumps(specs or []))
    usage, models, n_tool_calls, cached_calls = Usage(), [], 0, 0
    corrections_left = 1

    with _tracer.start_as_current_span("agent.run") as run_span:
        run_span.set_attribute("agent.caller", client.caller)
        for step in range(1, max_steps + 1):
            with _tracer.start_as_current_span("agent.step") as span:
                span.set_attribute("agent.step", step)
                streamed = False

                async def delta(text: str) -> None:
                    nonlocal streamed
                    streamed = True
                    await on_event({"type": "delta", "text": text})

                if context_cap:
                    n_history = fit_context(messages, n_history, context_cap,
                                            specs_chars, max_tokens)
                kw = {"tier": tier, "tools": specs, "max_tokens": max_tokens,
                      "prefer_model": prefer_model, "reasoning_effort": reasoning_effort}
                try:
                    resp = await (client.chat_stream(messages, delta, **kw) if on_event
                                  else client.chat(messages, **kw))
                except GatewayError as e:
                    if streamed:
                        await on_event({"type": "discard"})
                    if not (_is_bad_tool_use(e) and corrections_left):
                        raise
                    # The model produced an invalid turn (e.g. called a tool that does not
                    # exist). Tell it exactly what went wrong and let it try once more.
                    corrections_left -= 1
                    span.set_attribute("agent.corrected", True)
                    messages.append(ChatMessage(role="user", content=(
                        f"Your last reply was rejected: {e.detail.get('detail')}. "
                        f"The only tools are: {', '.join(sorted(by_name))}. If you are "
                        "finished, reply in plain text with the final answer and call no tool.")))
                    continue
            if resp.cached:
                cached_calls += 1  # served from cache: no quota was spent
            else:
                usage.prompt_tokens += resp.usage.prompt_tokens
                usage.completion_tokens += resp.usage.completion_tokens
            models.append(resp.model)
            messages.append(resp.message)

            if usage.total_tokens > token_budget:
                raise AgentLimitExceeded(f"token budget {token_budget} exceeded at step {step}")

            calls = resp.message.tool_calls or []
            if calls and streamed:
                await on_event({"type": "discard"})
            if not calls:
                run_span.set_attribute("agent.steps", step)
                return AgentResult(resp.message.content or "", messages, step,
                                   n_tool_calls, usage, models, cached_calls)

            for call in calls:
                n_tool_calls += 1
                name = call.function.get("name", "")
                if on_event:
                    await on_event({"type": "tool", "name": name,
                                    "args": _brief_args(call.function.get("arguments"))})
                content = await _run_tool(by_name, name, call.function.get("arguments") or "{}")
                messages.append(ChatMessage(role="tool", tool_call_id=call.id, name=name,
                                            content=content))

        raise AgentLimitExceeded(f"no final answer after {max_steps} steps")


TRIMMED = "[trimmed to save space; call the tool again if you need this] "


def estimate(messages: list[ChatMessage], specs_chars: int, max_tokens: int) -> int:
    """Same pessimistic estimate the gateway uses: ~4 chars per token plus the full
    completion allowance."""
    size = len(json.dumps([m.model_dump(mode="json") for m in messages])) + specs_chars
    return size // 4 + max_tokens


def fit_context(messages: list[ChatMessage], n_history: int, cap: int,
                specs_chars: int, max_tokens: int, margin: int = 300) -> int:
    """Shrink `messages` in place until the call fits under `cap`. Returns the new
    number of history messages. In order of what is least missed:
      1. older tool results (not the latest step's) cut to a short head
      2. earlier conversation turns dropped, oldest first
      3. any tool result, the largest first, cut as far as needed
    Tool messages are shortened, never removed: each must answer its tool call."""
    target = cap - margin

    def over() -> int:
        return estimate(messages, specs_chars, max_tokens) - target

    if over() <= 0:
        return n_history

    tools = [i for i, m in enumerate(messages) if m.role == "tool"]
    last_assistant = max((i for i, m in enumerate(messages) if m.role == "assistant"),
                         default=-1)
    for i in (i for i in tools if i < last_assistant):
        m = messages[i]
        if m.content and len(m.content) > 400 and not m.content.startswith(TRIMMED):
            messages[i] = m.model_copy(update={"content": TRIMMED + m.content[:300]})
            if over() <= 0:
                return n_history

    while n_history and over() > 0:
        del messages[1]  # the oldest history message (0 is the system prompt)
        n_history -= 1
    if over() <= 0:
        return n_history

    while (excess := over()) > 0:
        # Indices moved if history was dropped, so look again each round.
        idx = [i for i, m in enumerate(messages)
               if m.role == "tool" and len(m.content or "") > len(TRIMMED) + 200]
        if not idx:
            break  # nothing left to cut; the gateway will say so
        i = max(idx, key=lambda i: len(messages[i].content or ""))
        text = messages[i].content or ""
        body = text.removeprefix(TRIMMED)
        keep = max(200, len(body) - excess * 4 - 100)
        messages[i] = messages[i].model_copy(update={"content": TRIMMED + body[:keep]})
    return n_history


def _brief_args(raw: str | None) -> dict:
    """Tool arguments for display: short strings and numbers only."""
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(args, dict):
        return {}
    return {k: (v[:80] if isinstance(v, str) else v) for k, v in list(args.items())[:4]
            if isinstance(v, (str, int, float, bool, list))}


def _is_bad_tool_use(e: GatewayError) -> bool:
    return e.status == 422 and isinstance(e.detail, dict) and \
        e.detail.get("code") in {"tool_use_failed", "json_validate_failed"}


async def _run_tool(by_name: dict[str, Tool], name: str, raw_args: str) -> str:
    with _tracer.start_as_current_span("tool.call") as span:
        span.set_attribute("tool.name", name)
        tool = by_name.get(name)
        if tool is None:
            return to_tool_content({"error": f"unknown tool {name!r}",
                                    "available": sorted(by_name)})
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                raise TypeError("arguments must be a JSON object")
        except (ValueError, TypeError) as e:
            return to_tool_content({"error": f"invalid arguments: {e}"})
        try:
            return to_tool_content(await tool.fn(**args))
        except Exception as e:  # noqa: BLE001  the model should see any failure and adapt
            log.warning("tool %s failed: %s", name, e)
            span.record_exception(e)
            return to_tool_content({"error": f"{type(e).__name__}: {e}"})
