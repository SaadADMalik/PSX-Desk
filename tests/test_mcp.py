"""MCP server and client, connected in-process through the real SDK."""

import pytest
from agentkit.mcp_tools import McpContractError, McpToolError, McpToolset
from helpers import FlakySource
from market_data_mcp.server import build_server
from marketdata import MarketData
from mcp import Client


@pytest.fixture
def server(tmp_path):
    return build_server(MarketData(FlakySource(), tmp_path))


async def test_tools_are_discovered_with_schemas_and_read_only_hints(server):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"get_price_summary", "get_valuation", "get_announcements",
                          "get_evidence", "get_price_history", "get_quotes",
                          "get_board"}
    t = tools["get_price_summary"]
    assert t.input_schema["required"] == ["symbol"]
    assert t.output_schema is not None
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint
               for t in tools.values())


async def test_allowlist_hides_tools_an_agent_does_not_need(server):
    async with McpToolset(server, allow={"get_valuation"}) as ts:
        assert [t.name for t in ts.tools] == ["get_valuation"]


async def test_missing_required_tool_fails_fast(server):
    with pytest.raises(McpContractError):
        async with McpToolset(server, allow={"get_price_summary", "place_order"}):
            pass


async def test_call_returns_structured_evidence_with_ints_intact(server):
    async with McpToolset(server, allow={"get_price_summary"}) as ts:
        data = await ts.tools[0].fn(symbol="test")
    assert data["symbol"] == "TEST" and data["id"].startswith("ev_")
    assert isinstance(data["values"]["trading_days_available"], int)


async def test_anticipated_errors_reach_the_caller_with_their_message(server):
    async with McpToolset(server, allow={"get_price_summary"}) as ts:
        with pytest.raises(McpToolError, match="invalid PSX symbol"):
            await ts.tools[0].fn(symbol="../etc")


async def test_on_result_hook_sees_every_result(server):
    seen = []
    async with McpToolset(server, allow={"get_valuation"},
                          on_result=lambda n, d: seen.append(n) or {"ok": 1}) as ts:
        assert await ts.tools[0].fn(symbol="TEST") == {"ok": 1}
    assert seen == ["get_valuation"]


async def test_resource_lists_index_members(server):
    async with Client(server) as c:
        r = await c.read_resource("psx://index/kse100")
    assert "AAA" in r.contents[0].text


async def test_price_history_and_live_quotes_for_the_app(server):
    async with McpToolset(server, allow={"get_price_history", "get_quotes"}) as ts:
        tools = {t.name: t for t in ts.tools}
        h = await tools["get_price_history"].fn(symbol="test", days=30)
        q = await tools["get_quotes"].fn(symbols=["test", "nope"])
    assert len(h["points"]) == 30 and h["points"][-1]["close"] == 159.0
    assert set(q["quotes"]) == {"TEST"} and q["quotes"]["TEST"]["change_pct"] == 1.25


async def test_board_lists_every_stock_with_names_and_maps_ex_dividend_symbols(server):
    async with McpToolset(server, allow={"get_board"}) as ts:
        b = await ts.tools[0].fn()
    by = {s["symbol"]: s for s in b["stocks"]}
    assert set(by) == {"TEST", "HUBC", "TESTNC"} and b["count"] == 3  # TFC (debt) left out
    assert by["TESTNC"]["name"] == "Test Cement Ltd (non-compliant segment)"
    assert by["TEST"]["name"] == "Test Cement Ltd" and by["TEST"]["sector"] == "Cement"
    assert by["TEST"]["pe_ratio"] == 8.5 and by["TEST"]["change_1y_pct"] == 12.0
    assert by["HUBC"]["ex"] == "XD" and by["HUBC"]["price"] == 120.0  # HUBCXD -> HUBC
