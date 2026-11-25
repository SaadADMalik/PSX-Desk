"""Portfolio manager: turns an actionable brief into paper-trade *proposals*, one for
each account the brief concerns.

    brief + risk ─► [model, once] intent: buy / sell / hold, target weight, rationale
                 ─► broker.interested_accounts: who follows or holds the stock
                 ─► per account, [code] sizing: shares from weight, price, its cash, limits
                 ─► broker.propose_order(account)  ─► pending, until that user decides

Split by what each part is good at. Whether a brief justifies a position is judgement:
a model. How many shares that is for one account is arithmetic: code. So the model
runs once per brief, however many users follow the stock, and its cost does not grow
with users. The model gets no tools and writes no numbers the broker uses except a
target weight, which code clamps.

What this agent can do, by construction:
  * read the portfolio and propose (broker:read, broker:propose);
  * never approve or execute: it holds no broker:decide scope, and `decide` is not
    on its tool allowlist either. Two independent locks.

Its proposals are idempotent: client_order_id = run:<run id>:<account>, so a workflow
step redone after a crash returns the same proposals instead of second ones.
"""

import asyncio
import logging
import math
import os

from agentkit import GatewayClient, parse_with_repair
from agentkit.a2a import A2AService, InvalidRequest, Skill, build_a2a_app
from agentkit.analyst import serve
from agentkit.mcp_tools import McpToolset
from contracts import (
    ChatMessage,
    Lane,
    Portfolio,
    ProposeTradeRequest,
    ProposeTradeResult,
    Tier,
    TradeIntent,
)

log = logging.getLogger("portfolio_agent")

GATEWAY = os.getenv("LLM_GATEWAY_URL", "http://127.0.0.1:8100")
MARKET_DATA = os.getenv("MARKET_DATA_MCP_URL", "http://127.0.0.1:8300/mcp")
BROKER = os.getenv("BROKER_MCP_URL", "http://127.0.0.1:8600/mcp")

# Sizing targets. The broker enforces its own limits independently; these keep
# proposals inside them so a human is not asked to approve something that will fail.
MAX_POSITION_PCT = 20.0
MAX_ORDER_PCT = 10.0
CASH_BUFFER = 0.99  # leave room for fees

SYSTEM = """You are the portfolio manager of a paper-trading research desk for the
Pakistan Stock Exchange (PSX). A research brief has passed the desk's verifier and
risk gate. Decide what weight in this stock the brief justifies, for any account.

- buy: hold target_weight_pct of equity in it (accounts below that are topped up).
- sell: cut to target_weight_pct (0 = exit); applies to accounts that hold it.
- hold: no change. Often right: a brief is one view, not a mandate.
- target_weight_pct is at most {max_pos} and should scale with confidence.
- Code sizes each account's order from its own cash and holdings; you set the weight.
- rationale: 1-3 sentences a human approver can check against the brief.

This is paper trading for research, not investment advice.

Reply with ONLY: {{"action": "buy"|"sell"|"hold", "target_weight_pct": number,
"rationale": str}}"""


def describe(req: ProposeTradeRequest, price: float) -> str:
    b, r = req.brief, req.risk
    lines = [
        f"Symbol: {req.symbol}. Current price: {price}.",
        f"Brief: {b['stance']} (confidence {b['confidence']:.2f}). {b['thesis']}",
        "Key points:", *[f"- {c['text']}" for c in b.get("key_points", [])],
        "Risks:", *[f"- {c['text']}" for c in b.get("risks", [])],
        "Risk gate: {}; flags: {}".format(
            r["verdict"],
            "; ".join(f"{f['rule']}: {f['detail']}" for f in r.get("flags", [])) or "none"),
    ]
    return "\n".join(lines)


def size(intent: TradeIntent, stance: str, confidence: float, pf: Portfolio, symbol: str,
         price: float) -> tuple[str, int, str]:
    """(action, quantity, reason). Code, not the model, decides the number of shares."""
    held = next((p.quantity for p in pf.positions if p.symbol == symbol), 0)
    if intent.action == "buy" and stance != "bullish":
        return "hold", 0, f"model said buy on a {stance} brief; overridden to hold"
    if intent.action == "sell" and held == 0:
        return "hold", 0, "nothing held to sell (no short selling)"
    if intent.action == "hold":
        return "hold", 0, intent.rationale

    cap = MAX_POSITION_PCT * confidence  # a 0.6-confidence brief may reach 12%
    target = min(intent.target_weight_pct, cap)
    delta_value = pf.equity * target / 100 - held * price
    if intent.action == "buy":
        qty = math.floor(max(delta_value, 0) / price)
        qty = min(qty, math.floor(pf.equity * MAX_ORDER_PCT / 100 / price),
                  math.floor(pf.cash * CASH_BUFFER / price))
    else:
        qty = min(math.floor(max(-delta_value, 0) / price), held)
    if qty < 1:
        return "hold", 0, (f"target {target:.1f}% of equity is already met "
                           f"(asked {intent.target_weight_pct:g}%, cap {cap:.1f}%)")
    return intent.action, qty, intent.rationale


FANOUT = 8  # accounts sized and proposed to at once


async def propose_trade(payload: dict) -> dict:
    req = ProposeTradeRequest.model_validate(payload)
    if not req.risk.get("actionable"):
        raise InvalidRequest("brief is not actionable; the risk gate did not clear it")
    result = ProposeTradeResult(run_id=req.run_id, symbol=req.symbol, action="hold",
                                reason="")

    async with McpToolset(MARKET_DATA, allow={"get_valuation"}) as md:
        quote = await md.call("get_valuation", symbol=req.symbol)
    price = quote["values"].get("price")
    if not price or quote.get("stale"):
        return result.model_copy(update={"reason": "no live price; nothing proposed"})             .model_dump(mode="json")

    # ---- the judgement, once per brief
    client = GatewayClient(GATEWAY, caller="portfolio-agent", lane=Lane.pipeline)
    try:
        reply = await client.chat(
            [ChatMessage(role="system", content=SYSTEM.format(max_pos=MAX_POSITION_PCT)),
             ChatMessage(role="user", content=describe(req, price))],
            tier=Tier.small, max_tokens=700)
        intent, _ = await parse_with_repair(client, reply.message.content or "",
                                            TradeIntent, tier=Tier.small)
        tokens = reply.usage.total_tokens
    finally:
        await client.aclose()
    result = result.model_copy(update={"action": intent.action, "reason": intent.rationale,
                                       "tokens_used": tokens,
                                       "target_weight_pct": intent.target_weight_pct})
    if intent.action == "buy" and req.brief["stance"] != "bullish":
        return result.model_copy(update={
            "action": "hold",
            "reason": f"model said buy on a {req.brief['stance']} brief; overridden to hold",
        }).model_dump(mode="json")
    if intent.action == "hold":
        return result.model_dump(mode="json")

    brief_view = {k: req.brief[k] for k in ("stance", "confidence", "thesis")}
    brief_view |= {"risk": req.risk["verdict"],
                   "flags": [f["rule"] for f in req.risk.get("flags", [])],
                   "key_points": [c["text"] for c in req.brief.get("key_points", [])][:4],
                   "risks": [c["text"] for c in req.brief.get("risks", [])][:4]}
    ids = sorted({i for c in [*req.brief.get("key_points", []), *req.brief.get("risks", [])]
                  for i in c["evidence_ids"]})

    # ---- the arithmetic, per account that follows or holds the stock
    gate = asyncio.Semaphore(FANOUT)
    async with McpToolset(BROKER, allow={"interested_accounts", "get_portfolio",
                                         "propose_order"}) as broker:
        accounts = await broker.call("interested_accounts", symbol=req.symbol)

        async def one(account: str) -> str | None:
            async with gate:
                try:
                    pf = Portfolio.model_validate(
                        await broker.call("get_portfolio", account=account))
                    action, qty, reason = size(intent, req.brief["stance"],
                                               req.brief["confidence"], pf, req.symbol,
                                               float(price))
                    if action == "hold":
                        return None
                    p = await broker.call(
                        "propose_order", account=account,
                        client_order_id=f"run:{req.run_id}:{account}", symbol=req.symbol,
                        side=action, quantity=qty, rationale=reason[:1000], brief=brief_view,
                        run_id=req.run_id, evidence_ids=ids)
                    return p["status"]
                except Exception as e:  # noqa: BLE001  one account never sinks the rest
                    log.warning("proposal for one account failed: %s", e)
                    return "error"

        statuses = await asyncio.gather(*(one(a) for a in accounts))
    pending = sum(s == "pending" for s in statuses)
    return result.model_copy(update={
        "accounts": pending, "status": "pending" if pending else None,
        "reason": intent.rationale if pending else
                  f"{intent.rationale} (no account needed a change: "
                  f"{len(accounts)} follow or hold {req.symbol})",
    }).model_dump(mode="json")


app = build_a2a_app(A2AService(
    name="portfolio-agent",
    description="Turns an actionable research brief into a paper-trade proposal for a human.",
    version="0.1.0",
    url=os.getenv("PUBLIC_URL", "http://127.0.0.1:8000/"),
    skills=[Skill(id="propose_trade", name="Propose trade",
                  description="Size a paper trade from a verified, risk-gated brief and "
                              "submit it to the broker as a pending proposal.",
                  tags=["psx", "portfolio", "paper-trading"])],
    handler=propose_trade,
))


def run() -> None:
    serve(app, "portfolio-agent")
