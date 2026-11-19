"""Streaming, hop by hop: Groq -> provider -> gateway router -> agent loop -> A2A -> caller.

The rule under test throughout: retries and fallback happen only before the first word
reaches the caller. After that a failure is an error, never a silent retry that would
show the user the same words twice."""

import json

import httpx
import pytest
from agentkit import Tool, run_agent
from agentkit.a2a import A2AService, AgentDirectory, AgentTaskFailed, Skill, build_a2a_app
from contracts import ChatMessage, ChatResponse, ToolCall, Usage
from llm_gateway.provider import GroqProvider, StreamInterrupted, TransientError
from test_router import A, B, make, ok, req


def sse(*chunks: dict | str) -> bytes:
    out = []
    for c in chunks:
        out.append(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n")
    return "".join(out).encode()


def delta(text=None, tool=None, finish=None) -> dict:
    d = {}
    if text is not None:
        d["content"] = text
    if tool is not None:
        d["tool_calls"] = [tool]
    return {"choices": [{"index": 0, "delta": d, "finish_reason": finish}]}


def provider(handler) -> GroqProvider:
    return GroqProvider(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        api_key="k", base_url="http://groq")


# ---------------------------------------------------------------- provider


async def test_provider_streams_text_and_rebuilds_a_normal_response():
    body = sse(delta("Hel"), delta("lo"), delta(finish="stop"),
               {"choices": [], "x_groq": {"usage": {"prompt_tokens": 7,
                                                     "completion_tokens": 2}}}, "[DONE]")
    p = provider(lambda r: httpx.Response(200, content=body,
                                          headers={"content-type": "text/event-stream"}))
    seen = []

    async def on_delta(t):
        seen.append(t)

    data, _ = await p.chat_stream({"model": "m", "messages": []}, on_delta)
    assert seen == ["Hel", "lo"]
    assert data["choices"][0]["message"]["content"] == "Hello"
    assert data["usage"] == {"prompt_tokens": 7, "completion_tokens": 2}


async def test_provider_assembles_tool_calls_from_fragments():
    body = sse(delta(tool={"index": 0, "id": "c1", "function": {"name": "get_",
                                                                "arguments": '{"sym'}}),
               delta(tool={"index": 0, "function": {"name": "quote",
                                                    "arguments": 'bol": "LUCK"}'}}),
               delta(finish="tool_calls"), "[DONE]")
    p = provider(lambda r: httpx.Response(200, content=body))

    async def on_delta(t):
        raise AssertionError("no answer text was sent")

    data, _ = await p.chat_stream({"model": "m", "messages": []}, on_delta)
    tc = data["choices"][0]["message"]["tool_calls"][0]
    assert tc["id"] == "c1" and tc["function"] == {"name": "get_quote",
                                                   "arguments": '{"symbol": "LUCK"}'}


async def test_errors_before_text_can_be_retried_but_not_after():
    p = provider(lambda r: httpx.Response(200, content=sse(
        {"error": {"code": "server_error", "message": "overloaded"}})))
    with pytest.raises(TransientError):  # nothing sent yet: the router may retry
        await p.chat_stream({"model": "m", "messages": []}, lambda t: None)

    p = provider(lambda r: httpx.Response(200, content=sse(
        delta("Half an ans"), {"error": {"code": "server_error", "message": "boom"}})))

    async def on_delta(t):
        pass

    with pytest.raises(StreamInterrupted):  # text already shown: retrying would repeat it
        await p.chat_stream({"model": "m", "messages": []}, on_delta)


# ---------------------------------------------------------------- router


class StreamingScript:
    """Router-facing fake: plays a script, and streams the text of successful outcomes."""

    def __init__(self, inner):
        self.inner = inner

    async def chat_stream(self, payload, on_delta):
        data, headers = await self.inner.chat(payload)
        for word in data["choices"][0]["message"]["content"].split(" "):
            await on_delta(word + " ")
        return data, headers


async def test_router_falls_back_before_the_first_word(redis, clock):
    gw, scripted, _ = make(redis, clock, {A: [TransientError("x")] * 3, B: [ok("from b")]})
    gw.provider = StreamingScript(scripted)
    words = []

    async def on_delta(t):
        words.append(t)

    resp = await gw.complete(req(), on_delta=on_delta)
    assert resp.model == B and "".join(words).strip() == "from b"


async def test_a_cache_hit_still_streams_its_text(redis, clock):
    gw, scripted, _ = make(redis, clock, {A: [ok("cached answer")]})
    gw.provider = StreamingScript(scripted)
    await gw.complete(req(), on_delta=lambda t: _noop())
    got = []

    async def on_delta(t):
        got.append(t)

    again = await gw.complete(req(), on_delta=on_delta)
    assert again.cached and got == ["cached answer"]


async def _noop():
    return None


# ---------------------------------------------------------------- loop


def reply(content=None, calls=None):
    return ChatResponse(message=ChatMessage(role="assistant", content=content, tool_calls=calls),
                        model="fake", usage=Usage(prompt_tokens=10, completion_tokens=5))


class StreamingClient:
    caller = "test"

    def __init__(self, steps):
        self.steps = list(steps)  # (text to stream, response)

    async def chat_stream(self, messages, on_delta, **kw):
        text, resp = self.steps.pop(0)
        for ch in text:
            await on_delta(ch)
        return resp


async def test_loop_reports_tools_drops_preambles_and_streams_the_answer():
    async def quote(symbol: str):
        return {"price": 1}

    tools = [Tool("quote", "q", {"type": "object", "properties": {}}, quote)]
    call = ToolCall(id="c1", function={"name": "quote", "arguments": '{"symbol": "LUCK"}'})
    client = StreamingClient([("Let me check.", reply("Let me check.", [call])),
                              ("LUCK is 1.", reply("LUCK is 1."))])
    events = []

    async def on_event(e):
        events.append(e)

    res = await run_agent(client, system="s", user="u", tools=tools, on_event=on_event)
    kinds = [e["type"] for e in events]
    assert res.text == "LUCK is 1."
    # the preamble streamed, was discarded, then the tool ran, then the real answer
    assert kinds.index("discard") < kinds.index("tool") < len(kinds) - 1
    assert {"type": "tool", "name": "quote", "args": {"symbol": "LUCK"}} in events
    after_tool = "".join(e["text"] for e in events[kinds.index("tool"):]
                         if e["type"] == "delta")
    assert after_tool == "LUCK is 1."


# ---------------------------------------------------------------- A2A


async def talker(payload, emit):
    await emit({"type": "tool", "name": "look"})
    for word in ("Hello", " there"):
        await emit({"type": "delta", "text": word})
    return {"reply": "Hello there"}


async def quiet(payload):
    return {"reply": "done"}


async def crashing(payload, emit):
    await emit({"type": "delta", "text": "partial"})
    raise RuntimeError("tool exploded")


def a2a(name, handler):
    return build_a2a_app(A2AService(name=name, description="d", version="1.0.0",
                                    url=f"http://{name}/", skills=[Skill(name, name, "d")],
                                    handler=handler))


@pytest.fixture
async def agents():
    http = httpx.AsyncClient(mounts={f"http://{n}": httpx.ASGITransport(a2a(n, h))
                                     for n, h in [("talker", talker), ("quiet", quiet),
                                                  ("crashing", crashing)]})
    d = AgentDirectory(["http://talker", "http://quiet", "http://crashing"], http=http)
    await d.refresh()
    yield d, http
    await d.aclose()


async def test_card_advertises_streaming_only_when_the_handler_can(agents):
    _, http = agents
    talk = (await http.get("http://talker/.well-known/agent-card.json")).json()
    calm = (await http.get("http://quiet/.well-known/agent-card.json")).json()
    assert talk["capabilities"].get("streaming") is True
    assert not calm["capabilities"].get("streaming")


async def test_a2a_stream_delivers_progress_in_order_then_the_result(agents):
    d, _ = agents
    events = [e async for e in d.stream("talker", {"q": 1})]
    assert events == [{"type": "tool", "name": "look"}, {"type": "delta", "text": "Hello"},
                      {"type": "delta", "text": " there"},
                      {"type": "result", "data": {"reply": "Hello there"}}]
    # the same agent still answers plain, non-streaming calls
    assert await d.call("talker", {"q": 1}) == {"reply": "Hello there"}


async def test_a_failure_mid_stream_is_an_error_not_a_result(agents):
    d, _ = agents
    seen = []
    with pytest.raises(AgentTaskFailed, match="tool exploded"):
        async for e in d.stream("crashing", {}):
            seen.append(e)
    assert seen == [{"type": "delta", "text": "partial"}]
