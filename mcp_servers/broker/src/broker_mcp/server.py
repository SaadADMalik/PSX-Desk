"""The paper broker as an MCP server: the only service that can change trading state.

Tools and who may call them (config/access.toml):

    get_portfolio, list_proposals, get_proposal   broker:read     portfolio agent, dashboard
    propose_order                                 broker:propose  portfolio agent
    decide                                        broker:decide   dashboard (a human) only
    audit_log, verify_audit                       audit:read      dashboard, operator

Separation of duties is the point. The agent that proposes a trade cannot approve
it; the dashboard that approves cannot propose. Even a fully compromised agent
can, at worst, add a pending proposal for a human to reject.

The broker trusts no one's numbers. It fetches its own quote from market-data
(with its own identity, scope market:read), computes its own portfolio, and runs
its own limits at proposal time and again at fill time.

Paper only: there is no connection to a real brokerage anywhere in this system.
"""

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import uvicorn
from agentkit.auth import McpGuard
from agentkit.mcp_tools import McpToolset
from agentkit.tracing import setup_tracing
from contracts import Decision, Fill, OrderSide, Portfolio, Proposal
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from broker_mcp.store import BrokerStore

log = logging.getLogger("broker_mcp")

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,12}$")
KEY_RE = re.compile(r"^[A-Za-z0-9:_\-]{3,120}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_QUANTITY = 1_000_000

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                       open_world_hint=False)


class AuditEntry(BaseModel):
    id: int
    at: str
    actor: str
    action: str
    subject: str
    details: dict
    hash: str


class AuditCheck(BaseModel):
    checked: int
    first_bad_id: int | None
    ok: bool


class Quotes(Protocol):
    async def price(self, symbol: str) -> float | None: ...


class McpQuotes:
    """Current price from the market-data server's valuation evidence."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def price(self, symbol: str) -> float | None:
        try:
            async with McpToolset(self.url, allow={"get_valuation"}) as ts:
                ev = await ts.call("get_valuation", symbol=symbol)
        except Exception as e:  # noqa: BLE001  no quote is an answer, not a crash
            log.warning("no quote for %s: %s", symbol, e)
            return None
        if ev.get("stale"):
            return None  # never fill at a snapshot price
        p = ev["values"].get("price")
        return float(p) if p else None


async def marks(store: BrokerStore, quotes: Quotes, also: str | None = None
                ) -> dict[str, float | None]:
    symbols = set(await store.held_symbols()) | ({also} if also else set())
    return {s: await quotes.price(s) for s in sorted(symbols)}


def build_server(store: BrokerStore | None = None, quotes: Quotes | None = None,
                 guard: McpGuard | None = None) -> MCPServer:
    state: dict = {}
    guard = guard or McpGuard("broker", None)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        own = None
        state["store"] = store or (own := await BrokerStore.connect(os.environ["DATABASE_URL"]))
        state["quotes"] = quotes or McpQuotes(os.getenv("MARKET_DATA_MCP_URL",
                                                        "http://127.0.0.1:8300/mcp"))
        try:
            yield
        finally:
            if own is not None:
                await own.close()

    server = MCPServer(
        "psx-paper-broker", version="0.1.0", lifespan=lifespan,
        instructions=("Paper trading only. Proposals need a human decision; the broker "
                      "re-checks its limits at fill time."),
        **guard.server_kwargs())

    @server.tool(annotations=READ)
    async def get_portfolio() -> Portfolio:
        """Cash, positions (average cost, latest mark, weight) and equity, in PKR."""
        guard.require("broker:read")
        return await state["store"].portfolio(await marks(state["store"], state["quotes"]))

    @server.tool(annotations=READ)
    async def list_proposals(status: str | None = None, limit: int = 50) -> list[Proposal]:
        """Proposals, newest first; optionally only one status (pending, filled, ...)."""
        guard.require("broker:read")
        return await state["store"].proposals(status, max(1, min(limit, 200)))

    @server.tool(annotations=READ)
    async def get_proposal(proposal_id: str) -> Proposal:
        """One proposal with its checks and the brief it came from."""
        guard.require("broker:read")
        if not UUID_RE.match(proposal_id):
            raise ToolError("invalid proposal id")
        p = await state["store"].proposal(proposal_id)
        if p is None:
            raise ToolError("no such proposal")
        return p

    @server.tool(annotations=READ)
    async def list_fills(limit: int = 50) -> list[Fill]:
        """Executed paper fills, newest first."""
        guard.require("broker:read")
        return await state["store"].fills(max(1, min(limit, 200)))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False))
    async def propose_order(client_order_id: str, symbol: str, side: str, quantity: int,
                            rationale: str, brief: dict, run_id: str | None = None,
                            evidence_ids: list[str] | None = None) -> Proposal:
        """Propose a paper trade for a human to approve. Executes nothing. The same
        client_order_id always returns the same proposal. The broker prices it itself."""
        caller = guard.require("broker:propose") or "unauthenticated"
        if not KEY_RE.match(client_order_id):
            raise ToolError("invalid client_order_id")
        if not SYMBOL_RE.match(symbol):
            raise ToolError("invalid symbol")
        if side not in ("buy", "sell") or not 1 <= quantity <= MAX_QUANTITY:
            raise ToolError("side must be buy or sell; quantity 1..1,000,000")
        if not 10 <= len(rationale) <= 1000 or len(str(brief)) > 8000:
            raise ToolError("rationale must be 10-1000 characters; brief under 8,000")
        ref = await state["quotes"].price(symbol)
        if ref is None:
            raise ToolError(f"no live quote for {symbol}; cannot price a proposal")
        m = await marks(state["store"], state["quotes"], also=symbol)
        return await state["store"].propose(
            client_order_id=client_order_id, run_id=run_id, proposed_by=caller,
            symbol=symbol, side=OrderSide(side), quantity=quantity, ref_price=ref,
            rationale=rationale, brief=brief, evidence_ids=(evidence_ids or [])[:50], marks=m)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                             idempotent_hint=True, open_world_hint=False))
    async def decide(proposal_id: str, approve: bool, decided_by: str,
                     note: str = "") -> Decision:
        """A human's decision on a pending proposal. Approval fills it at the current quote
        if the broker's limits still pass; otherwise it fails with the reason."""
        caller = guard.require("broker:decide") or "unauthenticated"
        if not UUID_RE.match(proposal_id) or not re.match(r"^[a-z0-9_.\-]{1,40}$", decided_by):
            raise ToolError("invalid proposal id or decided_by")
        p = await state["store"].proposal(proposal_id)
        if p is None:
            raise ToolError("no such proposal")
        quote = await state["quotes"].price(p.symbol) if approve else None
        m = await marks(state["store"], state["quotes"], also=p.symbol) if approve else {}
        # The proven service and the human it vouches for, e.g. "dashboard:saad".
        return await state["store"].decide(proposal_id, approve=approve,
                                           actor=f"{caller}:{decided_by}", note=note[:500],
                                           quote=quote, marks=m)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                             idempotent_hint=True, open_world_hint=False))
    async def place_order(client_order_id: str, symbol: str, side: str, quantity: int,
                          placed_by: str, note: str = "") -> Decision:
        """A demo trade the human places themself: recorded as a proposal and decided in
        one go, at the current quote, under the same limits. Idempotent on
        client_order_id (a double click fills once)."""
        caller = guard.require("broker:trade") or "unauthenticated"
        if not KEY_RE.match(client_order_id) or not SYMBOL_RE.match(symbol):
            raise ToolError("invalid client_order_id or symbol")
        if side not in ("buy", "sell") or not 1 <= quantity <= MAX_QUANTITY:
            raise ToolError("side must be buy or sell; quantity 1..1,000,000")
        if not re.match(r"^[a-z0-9_.\-]{1,40}$", placed_by):
            raise ToolError("invalid placed_by")
        quote = await state["quotes"].price(symbol)
        if quote is None:
            raise ToolError(f"no live quote for {symbol}")
        actor = f"{caller}:{placed_by}"
        m = await marks(state["store"], state["quotes"], also=symbol)
        p = await state["store"].propose(
            client_order_id=f"manual:{client_order_id}", run_id=None, proposed_by=actor,
            symbol=symbol, side=OrderSide(side), quantity=quantity, ref_price=quote,
            rationale=(note or "your own demo trade")[:1000], brief={"manual": True},
            evidence_ids=[], marks=m)
        if p.status.value != "pending":
            return Decision(proposal=p, reason="; ".join(
                f"{c.rule}: {c.detail}" for c in p.checks if not c.ok) or p.status.value)
        return await state["store"].decide(p.id, approve=True, actor=actor, note=note[:500],
                                           quote=quote, marks=m)

    @server.tool(annotations=READ)
    async def audit_log(limit: int = 50) -> list[AuditEntry]:
        """The append-only audit trail, newest first."""
        guard.require("audit:read")
        return [AuditEntry(**r) for r in await state["store"].audit_log(max(1, min(limit, 500)))]

    @server.tool(annotations=READ)
    async def verify_audit() -> AuditCheck:
        """Recompute the audit hash chain. ok=false names the first row that was altered."""
        guard.require("audit:read")
        return AuditCheck(**await state["store"].verify_audit())

    return server


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    setup_tracing("broker-mcp")
    allowed = os.getenv("MCP_ALLOWED_HOSTS", "broker:8000,127.0.0.1:*,localhost:*")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in allowed.split(",") if h.strip()],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"])
    app = build_server(guard=McpGuard.from_env("broker")).streamable_http_app(
        stateless_http=True, json_response=True, transport_security=security, host="0.0.0.0")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
