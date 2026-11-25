"""Slice 4 security drills, run against the live stack.

    uv run python scripts/security_drills.py            (from the host, as `operator`)
    docker compose exec -T <service> python - < scripts/security_drills.py --as-service

Each drill states what must happen, does it, and prints PASS or FAIL. Nothing here
changes trading state: every write attempt is one that must be refused.
"""

import asyncio
import os
import sys

import httpx
from agentkit.auth import BearerAuth, default_identity
from agentkit.mcp_tools import McpToolError, McpToolset

LOCAL = {"market-data": "http://127.0.0.1:8300/mcp", "broker": "http://127.0.0.1:8600/mcp",
         "memory": "http://127.0.0.1:8500/mcp", "orchestrator": "http://127.0.0.1:8200",
         "gateway": "http://127.0.0.1:8100", "agent": "http://127.0.0.1:8401/"}
INSIDE = {"market-data": "http://market-data:8000/mcp", "broker": "http://broker:8000/mcp",
          "memory": "http://memory:8000/mcp", "orchestrator": "http://orchestrator:8000",
          "gateway": "http://llm-gateway:8000", "agent": "http://technical-agent:8000/"}
results: list[bool] = []


def report(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")


async def refused_tool(url: str, tool: str, args: dict, expect: str) -> tuple[bool, str]:
    try:
        async with McpToolset(url, allow={tool}) as ts:
            await ts.call(tool, **args)
    except Exception as e:  # noqa: BLE001
        # The MCP client's task group wraps errors in an ExceptionGroup: find the cause.
        while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
            e = e.exceptions[0]
        if isinstance(e, McpToolError):
            return expect in str(e), f"tool refused: {e}"
        return True, f"connection refused before any tool ran ({e!r:.90}...)"
    return False, "the call SUCCEEDED"


async def as_service(urls: dict) -> None:
    me = default_identity()
    print(f"identity: {me.name if me else 'NONE'}\n")
    if me is None:
        sys.exit("no identity: set SERVICE_NAME and a key")
    if me.name == "news-agent":
        ok, d = await refused_tool(urls["market-data"], "get_price_summary",
                                   {"symbol": "LUCK"}, "lacks scope 'market:read'")
        report("news agent cannot read prices (it holds news:read only)", ok, d)
        ok, d = await refused_tool(urls["broker"], "propose_order",
                                   {"account": "operator", "client_order_id": "drill:news", "symbol": "LUCK",
                                    "side": "buy", "quantity": 1, "rationale": "x" * 20,
                                    "brief": {}}, "forbidden")
        report("news agent cannot reach the broker at all (an injected 'buy' has no path)",
               ok, d)
    if me.name == "portfolio-agent":
        async with McpToolset(urls["broker"], allow={"get_portfolio"}) as ts:
            pf = await ts.call("get_portfolio", account="operator", symbol="LUCK")
        report("portfolio agent can read the portfolio of an account that follows the "
               "stock it is sizing", True, f"equity {pf['equity']:,.0f}")
        ok, d = await refused_tool(urls["broker"], "get_portfolio", {"account": "operator"},
                                   "needs that user's grant")
        report("portfolio agent cannot read an account without a reason (no grant, "
               "no followed stock)", ok, d)
        ok, d = await refused_tool(urls["broker"], "get_portfolio",
                                   {"account": "operator", "symbol": "ZZZQ"},
                                   "needs that user's grant")
        report("portfolio agent cannot read an account that does not follow the stock",
               ok, d)
        me_grant = me.mint_grant("operator", "broker", "portfolio-agent")
        ok, d = await refused_tool(urls["broker"], "get_portfolio",
                                   {"account": "operator", "grant": me_grant},
                                   "may not issue user grants")
        report("portfolio agent cannot forge a user grant (only the dashboard issues them)",
               ok, d)
        ok, d = await refused_tool(urls["broker"], "decide",
                                   {"account": "operator",
                                    "proposal_id": "00000000-0000-0000-0000-000000000000",
                                    "approve": True, "decided_by": "agent"},
                                   "lacks scope 'broker:decide'")
        report("portfolio agent cannot approve a trade (no broker:decide)", ok, d)
        ok, d = await refused_tool(urls["memory"], "remember",
                                   {"key": "drill", "symbol": "LUCK", "as_of": "x",
                                    "stance": "bullish", "confidence": 1, "text": "x"},
                                   "forbidden")
        report("portfolio agent cannot write memory", ok, d)
    if me.name == "operator":
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(urls["agent"], json={"jsonrpc": "2.0", "id": 1,
                                                  "method": "SendMessage", "params": {}})
            report("no token: an agent refuses work (401)", r.status_code == 401,
                   f"HTTP {r.status_code}")
            r = await c.post(urls["agent"], json={}, auth=BearerAuth(me))
            report("operator token: an agent still refuses (only the orchestrator assigns "
                   "work, 403)", r.status_code == 403, f"HTTP {r.status_code}: {r.text[:80]}")
            r = await c.post(f"{urls['gateway']}/v1/chat", json={})
            report("no token: the LLM gateway refuses (401)", r.status_code == 401,
                   f"HTTP {r.status_code}")
            r = await c.post(urls["market-data"], json={"jsonrpc": "2.0", "id": 1,
                                                        "method": "tools/list"},
                             headers={"accept": "application/json, text/event-stream"})
            report("no token: an MCP server refuses before listing tools (401)",
                   r.status_code == 401, f"HTTP {r.status_code}")
            other = BearerAuth(me, audience="memory")  # a valid token, minted for memory
            r = await c.post(urls["market-data"], json={"jsonrpc": "2.0", "id": 1,
                                                        "method": "tools/list"},
                             headers={"accept": "application/json, text/event-stream"},
                             auth=other)
            report("token for another audience is refused (no replay across services)",
                   r.status_code == 401, f"HTTP {r.status_code}")
            r = await c.get(f"{urls['orchestrator']}/v1/runs", params={"limit": 1},
                            auth=BearerAuth(me))
            report("operator can read runs (runs:read)", r.status_code == 200,
                   f"HTTP {r.status_code}")
        async with McpToolset(urls["broker"], allow={"verify_audit"}) as ts:
            v = await ts.call("verify_audit")
        report("audit hash chain verifies", v["ok"], str(v))
        ok, d = await refused_tool(urls["broker"], "decide",
                                   {"account": "operator",
                                    "proposal_id": "00000000-0000-0000-0000-000000000000",
                                    "approve": True, "decided_by": "me"},
                                   "lacks scope 'broker:decide'")
        report("operator CLI cannot approve trades either: only the dashboard", ok, d)
        ok, d = await refused_tool(urls["broker"], "get_portfolio", {"account": "operator"},
                                   "needs that user's grant")
        report("a service naming an account is not enough: reading it needs that user's "
               "grant", ok, d)
        forged = me.mint_grant("operator", "broker", "operator")
        ok, d = await refused_tool(urls["broker"], "get_portfolio",
                                   {"account": "operator", "grant": forged},
                                   "may not issue user grants")
        report("only the dashboard can issue a user grant (a self-signed one is refused)",
               ok, d)


def main() -> None:
    inside = "--as-service" in sys.argv or os.getenv("SERVICE_NAME")
    asyncio.run(as_service(INSIDE if inside else LOCAL))
    print(f"\n{sum(results)}/{len(results)} drills passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
