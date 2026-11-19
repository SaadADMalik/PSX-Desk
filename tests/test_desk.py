"""The analyst routine, the evidence store behind MCP, and the orchestrator's desk client."""

import json

import pytest
from agentkit.analyst import AnalystConfig, UngroundedOutput, analyse
from agentkit.mcp_tools import McpContractError, McpToolError, McpToolset
from contracts import AnalysisReport, AnalysisRequest, Claim, Stance
from helpers import FakeClient, FlakySource, answer, call
from market_data_mcp.evidence_store import MemoryEvidenceStore
from market_data_mcp.server import build_server
from marketdata import MarketData
from orchestrator.desk import AgentFailed, Desk

CONFIG = AnalystConfig(agent="technical-agent", system="analyse",
                       tools=frozenset({"get_price_summary"}))


def draft(*evidence_ids):
    return json.dumps({"stance": "neutral", "confidence": 0.5, "summary": "Mixed picture here.",
                       "points": [{"text": f"point citing {e}", "evidence_ids": [e]}
                                  for e in evidence_ids]})


async def real_evidence_id(tmp_path):
    return (await MarketData(FlakySource(), tmp_path).price_summary("TEST")).id


async def test_analyst_drops_points_citing_evidence_it_never_received(tmp_path):
    ev_id = await real_evidence_id(tmp_path)
    client = FakeClient([answer(calls=[call("get_price_summary", {"symbol": "TEST"})]),
                         answer(draft(ev_id, "ev_madeup"))])
    server = build_server(MarketData(FlakySource(), tmp_path), MemoryEvidenceStore())
    req = AnalysisRequest(symbol="TEST", request_id="r1")
    report = await analyse(CONFIG, req, gateway_url="unused", mcp_server=server, gateway=client)
    assert [p.evidence_ids for p in report.points] == [[ev_id]]
    assert any("ev_madeup" in g for g in report.data_gaps)
    assert report.evidence_ids == [ev_id]  # by reference only
    assert "copy them exactly" in client.seen[0][0].content  # the numbers rule is in the prompt


async def test_analyst_with_no_grounded_points_fails(tmp_path):
    client = FakeClient([answer(calls=[call("get_price_summary", {"symbol": "TEST"})]),
                         answer(draft("ev_madeup"))])
    server = build_server(MarketData(FlakySource(), tmp_path), MemoryEvidenceStore())
    with pytest.raises(UngroundedOutput):
        await analyse(CONFIG, AnalysisRequest(symbol="TEST", request_id="r1"),
                      gateway_url="unused", mcp_server=server, gateway=client)


# ---------------------------------------------------------------- evidence store via MCP


async def test_every_evidence_record_is_stored_at_the_source_and_retrievable(tmp_path):
    store = MemoryEvidenceStore()
    server = build_server(MarketData(FlakySource(), tmp_path), store)
    async with McpToolset(server, allow={"get_price_summary", "get_evidence"}) as ts:
        ev = await ts.call("get_price_summary", symbol="TEST")
        again = await ts.call("get_evidence", ids=[ev["id"], "ev_000000000000"])
    assert list(store.rows) == [ev["id"]]
    assert [e["id"] for e in again] == [ev["id"]]  # unknown ids are simply absent
    assert again[0]["values"]["trading_days_available"] == 60  # int stays int at the source


async def test_get_evidence_rejects_malformed_ids(tmp_path):
    server = build_server(MarketData(FlakySource(), tmp_path), MemoryEvidenceStore())
    async with McpToolset(server, allow={"get_evidence"}) as ts:
        with pytest.raises(McpToolError, match="invalid evidence ids"):
            await ts.call("get_evidence", ids=["'; drop table evidence; --"])


async def test_code_calls_respect_the_allowlist(tmp_path):
    server = build_server(MarketData(FlakySource(), tmp_path), MemoryEvidenceStore())
    async with McpToolset(server, allow={"get_evidence"}) as ts:
        with pytest.raises(McpContractError):
            await ts.call("get_price_summary", symbol="TEST")


async def test_announcements_tool_returns_counted_news_evidence(tmp_path):
    server = build_server(MarketData(FlakySource(), tmp_path), MemoryEvidenceStore())
    async with McpToolset(server, allow={"get_announcements"}) as ts:
        ev = await ts.call("get_announcements", symbol="TEST")
    assert ev["kind"] == "news"
    assert ev["values"]["by_category_in_window"]["financial_results"] == 1


# ---------------------------------------------------------------- desk client


class FakeDirectory:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # skill -> callable(payload) or exception

    async def call(self, skill, payload):
        b = self.behaviour[skill]
        if isinstance(b, Exception):
            raise b
        return b(payload)


def report_for(agent, **override):
    def make(payload):
        return AnalysisReport(**{
            "request_id": payload["request_id"], "agent": agent, "symbol": "LUCK",
            "stance": Stance.neutral, "confidence": 0.6, "summary": "Looks balanced.",
            "points": [Claim(text="RSI is 65", evidence_ids=["ev_aaa"])],
            "evidence_ids": ["ev_aaa"], **override}).model_dump(mode="json")
    return make


def desk(behaviour):
    return Desk(FakeDirectory(behaviour), market_data_url="unused", memory_url="unused")


REQ = AnalysisRequest(symbol="LUCK", request_id="r1")


async def test_desk_validates_reports():
    report = await desk({"technical_analysis": report_for("tech")}).analyse(
        "technical_analysis", REQ)
    assert report.agent == "tech"


async def test_report_for_a_different_request_is_rejected():
    d = desk({"technical_analysis": report_for("tech", request_id="someone-else")})
    with pytest.raises(AgentFailed, match="does not match"):
        await d.analyse("technical_analysis", REQ)


async def test_contract_violations_and_outages_become_agent_failures():
    d = desk({"technical_analysis": lambda p: {"agent": "tech"},
              "fundamental_analysis": ConnectionError("refused")})
    with pytest.raises(AgentFailed, match="invalid AnalysisReport"):
        await d.analyse("technical_analysis", REQ)
    with pytest.raises(AgentFailed, match="ConnectionError"):
        await d.analyse("fundamental_analysis", REQ)


def test_step_errors_unwrap_exception_groups():
    from orchestrator.workflow import describe

    err = ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("memory:8000")])
    assert describe(err) == "ConnectionError: memory:8000"


async def test_analyst_without_any_evidence_says_which_tool_failed(tmp_path):
    from agentkit.analyst import NoEvidence

    source = FlakySource()
    source.up = False  # and no snapshot yet: the tool errors
    client = FakeClient([answer(calls=[call("get_price_summary", {"symbol": "TEST"})]),
                         answer(draft("ev_madeup"))])
    server = build_server(MarketData(source, tmp_path), MemoryEvidenceStore())
    with pytest.raises(NoEvidence, match="get_price_summary: .*data unavailable"):
        await analyse(CONFIG, AnalysisRequest(symbol="TEST", request_id="r1"),
                      gateway_url="unused", mcp_server=server, gateway=client)
    assert len(client.seen) == 2  # no repair call spent on a report with nothing to cite
