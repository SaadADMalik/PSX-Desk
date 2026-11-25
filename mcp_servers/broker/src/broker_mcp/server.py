"""The paper broker as an MCP server: the only service that can change trading state.

Tools and who may call them (config/access.toml):

    get_portfolio, list_proposals, get_proposal   broker:read     portfolio agent, dashboard
    propose_order                                 broker:propose  portfolio agent, advisor
    decide                                        broker:decide   dashboard (a human) only
    open_account                                  accounts:open   dashboard (at sign-up)
    interested_accounts                           accounts:list   portfolio agent
    audit_log, verify_audit, activity             audit:read      dashboard, operator
    get_watchlist                                 watchlist:read  dashboard, advisor
    watch, unwatch, init_watchlist                watchlist:write dashboard
    watched_symbols (everyone's, once)            watchlist:all   scheduler
    list_alerts, list_alert_events                alerts:read     dashboard, advisor
    create_alert, cancel_alert                    alerts:write    dashboard
    check_alerts                                  alerts:check    scheduler

It is also the account service. Every user has their own demo account: cash,
positions, proposals, watchlist and price alerts, all keyed by `account`. Each tool
that reads or changes one takes the account the calling service vouches for (the
dashboard for the signed-in user, the advisor for the user it is talking to), and a
row of another account is reported as not found.

Separation of duties is the point. The agent that proposes a trade cannot approve
it; the dashboard that approves cannot propose. Even a fully compromised agent
can, at worst, add a pending proposal for a human to reject.

The broker trusts no one's numbers. It fetches its own quote from market-data
(with its own identity, scope market:read), computes its own portfolio, and runs
its own limits at proposal time and again at fill time.

Paper only: there is no connection to a real brokerage anywhere in this system.
"""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol

import uvicorn
from agentkit.auth import McpGuard
from agentkit.mcp_tools import McpToolset
from agentkit.tracing import setup_tracing
from contracts import AlertEvent, AlertRule, Decision, Fill, OrderSide, Portfolio, Proposal
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from broker_mcp.store import BrokerStore

log = logging.getLogger("broker_mcp")

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,12}$")
KEY_RE = re.compile(r"^[A-Za-z0-9:_.@\-]{3,160}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_QUANTITY = 1_000_000

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                       open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                        open_world_hint=False)
ACCOUNT_RE = re.compile(r"^[a-z0-9_.@\-]{1,40}$")


def _account(account: str) -> str:
    account = account.lower()
    if not ACCOUNT_RE.match(account):
        raise ToolError("invalid account")
    return account


def _symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not SYMBOL_RE.match(symbol):
        raise ToolError("invalid symbol")
    return symbol


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


class Activity(BaseModel):
    cursor: int
    items: list[dict]


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

    async def prices(self, symbols: list[str]) -> dict[str, float | None]:
        """Marks for many holdings in ONE request (the market-wide quote board, which
        market-data caches). Valuing a portfolio one live PSX request per holding took
        6-9 s per page, 38 s cold. Not used for fills: the traded symbol's price still
        comes from `price()`, which refuses snapshot data."""
        try:
            async with McpToolset(self.url, allow={"get_quotes"}) as ts:
                board = await ts.call("get_quotes", symbols=symbols)
        except Exception as e:  # noqa: BLE001
            log.warning("no batch quotes (%s); pricing one by one", e)
            return {s: await self.price(s) for s in symbols}
        age = (datetime.now(UTC) - datetime.fromisoformat(board["fetched_at"])).total_seconds()
        if age > MAX_MARK_AGE_S:
            return dict.fromkeys(symbols)  # too old to value against: shown as stale
        return {s: (float(q["price"]) if (q := board["quotes"].get(s)) and q.get("price")
                    else None) for s in symbols}


MAX_MARK_AGE_S = 15 * 60
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000000"))


async def marks(store: BrokerStore, quotes: Quotes, account: str, also: str | None = None
                ) -> dict[str, float | None]:
    symbols = sorted(set(await store.held_symbols(account)) | ({also} if also else set()))
    if not symbols:
        return {}
    batch = getattr(quotes, "prices", None)
    if batch:
        return await batch(symbols)
    return dict(zip(symbols, await asyncio.gather(*(quotes.price(s) for s in symbols)),
                    strict=True))


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

    async def acting_for(account: str, grant: str | None, *,
                         follows: str | None = None) -> str:
        """The account this call may act for. A service saying "account X" is not
        enough: it must present X's user grant, signed by the dashboard when X signed
        in, meant for the broker and for this very service (agentkit.auth). The one
        exception is research fan-out, which runs with nobody signed in: a caller
        holding accounts:followers may act for an account only if that account follows
        or holds `follows`, the stock being researched, and only through the tools
        that pass it (read the portfolio, create a pending proposal)."""
        account = _account(account)
        if not guard.enabled:
            return account  # in-process: tests and local tools
        if grant:
            if guard.granted_account(grant) != account:
                raise ToolError("forbidden: the grant is for another account")
            return account
        if follows and guard.has("accounts:followers") \
                and await state["store"].follows(account, _symbol(follows)):
            return account
        raise ToolError("forbidden: acting for an account needs that user's grant")

    server = MCPServer(
        "psx-paper-broker", version="0.1.0", lifespan=lifespan,
        instructions=("Paper trading only. Proposals need a human decision; the broker "
                      "re-checks its limits at fill time."),
        **guard.server_kwargs())

    @server.tool(annotations=WRITE)
    async def open_account(account: str, grant: str | None = None) -> bool:
        """Open a demo account with its starting cash. False if it was already open."""
        caller = guard.require("accounts:open") or "unauthenticated"
        return await state["store"].open_account(await acting_for(account, grant),
                                                 INITIAL_CAPITAL, caller)

    @server.tool(annotations=READ)
    async def interested_accounts(symbol: str) -> list[str]:
        """Accounts that follow or hold this stock: who a research brief on it concerns."""
        guard.require("accounts:list")
        return await state["store"].interested_accounts(_symbol(symbol))

    @server.tool(annotations=READ)
    async def get_portfolio(account: str, grant: str | None = None,
                            symbol: str | None = None) -> Portfolio:
        """This account's cash, positions (average cost, latest mark, weight) and equity,
        in PKR. `symbol`: for research fan-out, the stock that makes this account
        relevant (see acting_for)."""
        guard.require("broker:read")
        account = await acting_for(account, grant, follows=symbol)
        return await state["store"].portfolio(
            account, await marks(state["store"], state["quotes"], account))

    @server.tool(annotations=READ)
    async def list_proposals(account: str, status: str | None = None, limit: int = 50,
                             run_id: str | None = None,
                             grant: str | None = None) -> list[Proposal]:
        """This account's proposals, newest first; optionally one status (pending,
        filled, ...) or the ones from one research run."""
        guard.require("broker:read")
        return await state["store"].proposals(await acting_for(account, grant), status,
                                              max(1, min(limit, 200)), run_id)

    @server.tool(annotations=READ)
    async def get_proposal(account: str, proposal_id: str,
                           grant: str | None = None) -> Proposal:
        """One of this account's proposals with its checks and the brief it came from."""
        guard.require("broker:read")
        if not UUID_RE.match(proposal_id):
            raise ToolError("invalid proposal id")
        p = await state["store"].proposal(await acting_for(account, grant), proposal_id)
        if p is None:
            raise ToolError("no such proposal")
        return p

    @server.tool(annotations=READ)
    async def list_fills(account: str, limit: int = 50,
                         grant: str | None = None) -> list[Fill]:
        """This account's executed demo fills, newest first."""
        guard.require("broker:read")
        return await state["store"].fills(await acting_for(account, grant),
                                          max(1, min(limit, 200)))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False))
    async def propose_order(account: str, client_order_id: str, symbol: str, side: str,
                            quantity: int, rationale: str, brief: dict,
                            run_id: str | None = None,
                            evidence_ids: list[str] | None = None,
                            grant: str | None = None) -> Proposal:
        """Propose a paper trade for a human to approve. Executes nothing. The same
        client_order_id always returns the same proposal. The broker prices it itself."""
        caller = guard.require("broker:propose") or "unauthenticated"
        if not KEY_RE.match(client_order_id):
            raise ToolError("invalid client_order_id")
        if not SYMBOL_RE.match(symbol):
            raise ToolError("invalid symbol")
        account = await acting_for(account, grant, follows=symbol)
        if side not in ("buy", "sell") or not 1 <= quantity <= MAX_QUANTITY:
            raise ToolError("side must be buy or sell; quantity 1..1,000,000")
        if not 10 <= len(rationale) <= 1000 or len(str(brief)) > 8000:
            raise ToolError("rationale must be 10-1000 characters; brief under 8,000")
        ref = await state["quotes"].price(symbol)
        if ref is None:
            raise ToolError(f"no live quote for {symbol}; cannot price a proposal")
        m = await marks(state["store"], state["quotes"], account, also=symbol)
        try:
            return await state["store"].propose(
                account=account, client_order_id=client_order_id, run_id=run_id,
                proposed_by=caller, symbol=symbol, side=OrderSide(side), quantity=quantity,
                ref_price=ref, rationale=rationale, brief=brief,
                evidence_ids=(evidence_ids or [])[:50], marks=m)
        except (ValueError, LookupError) as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                             idempotent_hint=True, open_world_hint=False))
    async def decide(account: str, proposal_id: str, approve: bool, decided_by: str,
                     note: str = "", grant: str | None = None) -> Decision:
        """A human's decision on a pending proposal. Approval fills it at the current quote
        if the broker's limits still pass; otherwise it fails with the reason."""
        caller = guard.require("broker:decide") or "unauthenticated"
        if not UUID_RE.match(proposal_id) or not re.match(r"^[a-z0-9_.\-]{1,40}$", decided_by):
            raise ToolError("invalid proposal id or decided_by")
        account = await acting_for(account, grant)
        p = await state["store"].proposal(account, proposal_id)
        if p is None:
            raise ToolError("no such proposal")
        quote = await state["quotes"].price(p.symbol) if approve else None
        m = (await marks(state["store"], state["quotes"], account, also=p.symbol)
             if approve else {})
        # The proven service and the human it vouches for, e.g. "dashboard:saad".
        return await state["store"].decide(account, proposal_id, approve=approve,
                                           actor=f"{caller}:{decided_by}", note=note[:500],
                                           quote=quote, marks=m)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                             idempotent_hint=True, open_world_hint=False))
    async def place_order(account: str, client_order_id: str, symbol: str, side: str,
                          quantity: int, placed_by: str, note: str = "",
                          grant: str | None = None) -> Decision:
        """A demo trade the human places themself: recorded as a proposal and decided in
        one go, at the current quote, under the same limits. Idempotent on
        client_order_id (a double click fills once)."""
        caller = guard.require("broker:trade") or "unauthenticated"
        account = await acting_for(account, grant)
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
        m = await marks(state["store"], state["quotes"], account, also=symbol)
        try:
            p = await state["store"].propose(
                account=account, client_order_id=f"manual:{account}:{client_order_id}",
                run_id=None, proposed_by=actor, symbol=symbol, side=OrderSide(side),
                quantity=quantity, ref_price=quote,
                rationale=(note or "your own demo trade")[:1000], brief={"manual": True},
                evidence_ids=[], marks=m)
        except (ValueError, LookupError) as e:
            raise ToolError(str(e)) from e
        if p.status.value != "pending":
            return Decision(proposal=p, reason="; ".join(
                f"{c.rule}: {c.detail}" for c in p.checks if not c.ok) or p.status.value)
        return await state["store"].decide(account, p.id, approve=True, actor=actor,
                                           note=note[:500], quote=quote, marks=m)

    # ------------------------------------------------------------ watchlists
    #
    # Every per-account tool takes the user's `grant` (see acting_for).

    @server.tool(annotations=READ)
    async def get_watchlist(account: str, grant: str | None = None) -> list[str]:
        """The stocks this account follows, in the order they were added."""
        guard.require("watchlist:read")
        return await state["store"].watchlist(await acting_for(account, grant))

    @server.tool(annotations=READ)
    async def watched_symbols() -> list[str]:
        """Every stock on anyone's watchlist, once. The daily research covers these."""
        guard.require("watchlist:all")
        return await state["store"].watched_symbols()

    @server.tool(annotations=WRITE)
    async def init_watchlist(account: str, symbols: list[str],
                             grant: str | None = None) -> bool:
        """Give a new account its starting watchlist. Does nothing if the account has
        had a watchlist before (an emptied list stays empty)."""
        guard.require("watchlist:write")
        return await state["store"].init_watchlist(
            await acting_for(account, grant), [_symbol(s) for s in symbols[:60]])

    @server.tool(annotations=WRITE)
    async def watch(account: str, symbol: str, grant: str | None = None) -> list[str]:
        """Add a stock to this account's watchlist. Returns the new list."""
        guard.require("watchlist:write")
        account = await acting_for(account, grant)
        try:
            return await state["store"].watch(account, _symbol(symbol))
        except ValueError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=WRITE)
    async def unwatch(account: str, symbol: str, grant: str | None = None) -> list[str]:
        """Remove a stock from this account's watchlist. Returns the new list."""
        guard.require("watchlist:write")
        return await state["store"].unwatch(await acting_for(account, grant),
                                            _symbol(symbol))

    # ------------------------------------------------------------ price alerts

    @server.tool(annotations=READ)
    async def list_alerts(account: str, active_only: bool = False,
                          grant: str | None = None) -> list[AlertRule]:
        """This account's price alerts, newest first."""
        guard.require("alerts:read")
        return await state["store"].alerts(await acting_for(account, grant), active_only)

    @server.tool(annotations=READ)
    async def list_alert_events(account: str, limit: int = 50,
                                grant: str | None = None) -> list[AlertEvent]:
        """When this account's alerts fired or re-armed, newest first."""
        guard.require("alerts:read")
        return await state["store"].alert_events(await acting_for(account, grant),
                                                 max(1, min(limit, 200)))

    @server.tool(annotations=WRITE)
    async def create_alert(account: str, symbol: str, kind: str, threshold: float,
                           created_by: str, days: int | None = None,
                           grant: str | None = None) -> AlertRule:
        """Alert when a stock falls. kind "pct": the price falls `threshold` percent below
        the reference. kind "pkr": the position loses `threshold` rupees (needs shares
        held). The reference is your average cost if you hold the stock, else today's
        price. `days`: watch for this many days from now; omit to watch until cancelled.
        It fires once per fall and re-arms after the price recovers past 80% of it."""
        caller = guard.require("alerts:write") or "unauthenticated"
        account, symbol = await acting_for(account, grant), _symbol(symbol)
        if not re.match(r"^[a-z0-9_.\-:]{1,60}$", created_by):
            raise ToolError("invalid created_by")
        if kind == "pct" and not 0.1 <= threshold <= 90:
            raise ToolError("a percentage alert needs a threshold from 0.1 to 90")
        if kind == "pkr" and not 1 <= threshold <= 1_000_000_000:
            raise ToolError("a rupee alert needs a threshold from 1 to 1,000,000,000")
        if kind not in ("pct", "pkr"):
            raise ToolError('kind must be "pct" or "pkr"')
        if days is not None and not 1 <= days <= 3650:
            raise ToolError("days must be 1 to 3650, or omitted for no end")
        pf = await state["store"].portfolio(account, {})
        pos = next((p for p in pf.positions if p.symbol == symbol), None)
        if kind == "pkr" and pos is None:
            raise ToolError(f"a rupee alert needs a position: you don't hold {symbol}")
        if pos is not None:
            ref, basis = pos.avg_cost, "avg_cost"
        else:
            ref, basis = await state["quotes"].price(symbol), "price_when_set"
            if ref is None:
                raise ToolError(f"no live quote for {symbol}; cannot set a reference price")
        ends = datetime.now(UTC) + timedelta(days=days) if days else None
        try:
            return await state["store"].create_alert(
                account=account, symbol=symbol, kind=kind, threshold=threshold, ref_price=ref,
                ref_basis=basis, ends_at=ends,
                created_by=f"{caller}:{created_by}")
        except ValueError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=WRITE)
    async def cancel_alert(account: str, alert_id: str, cancelled_by: str,
                           grant: str | None = None) -> AlertRule:
        """Stop one of this account's alerts."""
        caller = guard.require("alerts:write") or "unauthenticated"
        if not UUID_RE.match(alert_id) or not re.match(r"^[a-z0-9_.\-]{1,40}$", cancelled_by):
            raise ToolError("invalid alert id or cancelled_by")
        rule = await state["store"].cancel_alert(
            await acting_for(account, grant), alert_id, f"{caller}:{cancelled_by}")
        if rule is None:
            raise ToolError("no such alert")
        return rule

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=False, open_world_hint=False))
    async def check_alerts() -> list[AlertEvent]:
        """Check every active alert against live prices once. Returns the alerts that
        fired or re-armed in this pass. Stale prices are skipped, never judged."""
        caller = guard.require("alerts:check") or "unauthenticated"
        store = state["store"]
        symbols = await store.active_alert_symbols()
        if not symbols:
            return []
        quotes = state["quotes"]
        batch = getattr(quotes, "prices", None)
        prices = await batch(symbols) if batch else {s: await quotes.price(s) for s in symbols}
        return await store.check_alerts(prices, await store.holdings(symbols), actor=caller)

    @server.tool(annotations=READ)
    async def audit_log(limit: int = 50, account: str | None = None,
                        grant: str | None = None) -> list[AuditEntry]:
        """The append-only audit trail, newest first; with `account`, only its entries.
        Without it, everyone's: that needs audit:all (the operator only)."""
        guard.require("audit:read")
        if account is None:
            guard.require("audit:all")
        rows = await state["store"].audit_log(
            max(1, min(limit, 500)), await acting_for(account, grant) if account else None)
        return [AuditEntry(**r) for r in rows]

    @server.tool(annotations=READ)
    async def activity(after_id: int | None = None, limit: int = 200) -> Activity:
        """Account changes since audit entry `after_id` (proposals, fills, alerts), oldest
        first, each tagged with its account; and the cursor to pass next time. Without
        `after_id`: just the current cursor."""
        guard.require("audit:read")
        return Activity(**await state["store"].activity(after_id, max(1, min(limit, 500))))

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
