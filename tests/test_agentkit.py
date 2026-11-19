import json

import pytest
from agentkit import AgentLimitExceeded, GatewayError, Tool, parse_as, run_agent
from agentkit.structured import StructuredOutputError, parse_with_repair
from contracts import Brief, ChatMessage, ChatResponse, ToolCall, Usage


def answer(content=None, calls=None):
    return ChatResponse(message=ChatMessage(role="assistant", content=content, tool_calls=calls),
                        model="fake", usage=Usage(prompt_tokens=10, completion_tokens=5))


def call(name, args, id_="c1"):
    return ToolCall(id=id_, function={"name": name, "arguments": json.dumps(args)})


class FakeClient:
    caller = "test"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[ChatMessage]] = []

    async def chat(self, messages, **kw):
        self.seen.append(list(messages))
        return self.replies.pop(0)


async def echo(symbol: str):
    return {"symbol": symbol, "price": 1}


TOOLS = [Tool("echo", "echo", {"type": "object", "properties": {}}, echo)]


async def test_loop_runs_tool_then_returns_final_text():
    client = FakeClient([answer(calls=[call("echo", {"symbol": "X"})]), answer("done")])
    res = await run_agent(client, system="s", user="u", tools=TOOLS)
    assert res.text == "done" and res.steps == 2 and res.tool_calls == 1
    tool_msg = client.seen[1][-1]
    assert tool_msg.role == "tool" and json.loads(tool_msg.content)["symbol"] == "X"


async def test_unknown_tool_and_bad_args_are_reported_to_the_model():
    bad = ToolCall(id="c2", function={"name": "echo", "arguments": "not json"})
    client = FakeClient([answer(calls=[call("nope", {}), bad]), answer("ok")])
    await run_agent(client, system="s", user="u", tools=TOOLS)
    errors = [json.loads(m.content)["error"] for m in client.seen[1] if m.role == "tool"]
    assert "unknown tool" in errors[0] and "invalid arguments" in errors[1]


async def test_step_cap_stops_a_looping_model():
    loop_forever = [answer(calls=[call("echo", {"symbol": "X"})]) for _ in range(3)]
    with pytest.raises(AgentLimitExceeded):
        await run_agent(FakeClient(loop_forever), system="s", user="u", tools=TOOLS, max_steps=3)


class RejectingClient(FakeClient):
    """First call fails the way Groq does when the model invents a tool."""

    def __init__(self, replies, rejections=1):
        super().__init__(replies)
        self.rejections = rejections

    async def chat(self, messages, **kw):
        if self.rejections:
            self.rejections -= 1
            self.seen.append(list(messages))
            raise GatewayError(422, {"code": "tool_use_failed",
                                     "detail": "attempted to call tool 'JSON'"})
        return await super().chat(messages, **kw)


async def test_invalid_tool_use_gets_one_correction():
    client = RejectingClient([answer("final")])
    res = await run_agent(client, system="s", user="u", tools=TOOLS)
    assert res.text == "final"
    correction = client.seen[1][-1]
    assert correction.role == "user" and "echo" in correction.content


async def test_second_invalid_tool_use_is_raised():
    with pytest.raises(GatewayError):
        await run_agent(RejectingClient([], rejections=2), system="s", user="u", tools=TOOLS)


BRIEF = {"symbol": "LUCK", "stance": "neutral", "confidence": 0.5,
         "thesis": "Balanced picture overall.",
         "key_points": [{"text": "Trades above SMA200", "evidence_ids": ["ev_1"]}],
         "risks": [{"text": "High drawdown", "evidence_ids": ["ev_1"]}]}


def test_parse_accepts_fenced_json_and_strips_think_tags():
    text = f"<think>hmm</think>Here:\n```json\n{json.dumps(BRIEF)}\n```"
    assert parse_as(text, Brief).symbol == "LUCK"


def test_claims_without_citations_are_rejected():
    bad = {**BRIEF, "risks": [{"text": "Something bad", "evidence_ids": []}]}
    with pytest.raises(StructuredOutputError):
        parse_as(json.dumps(bad), Brief)


async def test_one_repair_attempt_then_give_up():
    fixed = FakeClient([answer(json.dumps(BRIEF))])
    brief, repaired = await parse_with_repair(fixed, "not json", Brief)
    assert repaired and brief.stance.value == "neutral"
    still_bad = FakeClient([answer("still not json")])
    with pytest.raises(StructuredOutputError):
        await parse_with_repair(still_bad, "not json", Brief)


def test_mechanical_output_faults_are_fixed_without_a_repair_call():
    from agentkit.structured import cap_claims

    claims = [{"text": f"c{i}", "evidence_ids": ["e"]} for i in range(8)]
    claims.insert(1, {"text": "uncited", "evidence_ids": []})
    fixed = cap_claims(claims, 6)
    assert len(fixed) == 6 and all(c["evidence_ids"] for c in fixed)


# --------------------------------------------------- context stays under the gateway cap

async def test_piled_up_tool_results_are_trimmed_to_fit_the_gateway_cap():
    """Regression: the advisor read several official pages in one turn, the context grew
    to ~10K tokens and the gateway refused the call (413). Every call must fit."""
    from agentkit.loop import TRIMMED, estimate

    async def read_page(url: str = ""):
        return "x" * 9000  # ~2.2K tokens per result

    tool = Tool("read_page", "Read a page", {"type": "object", "properties": {
        "url": {"type": "string"}}}, read_page)
    history = [ChatMessage(role="user", content="earlier question " * 200),
               ChatMessage(role="assistant", content="earlier answer " * 200)]
    replies = [answer(calls=[call("read_page", {"url": f"u{i}"}, id_=f"c{i}")]) for i in range(5)]
    client = FakeClient([*replies, answer("done")])
    out = await run_agent(client, system="s", user="q", tools=[tool], history=history,
                          max_steps=8, max_tokens=1000, context_cap=7000)
    assert out.text == "done"
    specs = len(json.dumps([tool.spec()]))
    for sent in client.seen:
        assert estimate(sent, specs, 1000) <= 7000
        # every tool call still has its answer, so the provider accepts the turn
        assert sum(m.role == "tool" for m in sent) == sum(
            len(m.tool_calls or []) for m in sent if m.role == "assistant")
    last = client.seen[-1]
    assert any((m.content or "").startswith(TRIMMED) for m in last if m.role == "tool")
    assert last[-1].role == "tool"  # the newest result is still there
