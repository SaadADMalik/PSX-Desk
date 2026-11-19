"""Portfolio manager: turns an actionable brief into a paper-trade *proposal*.

    brief + risk ─► [model] intent: buy / sell / hold, target weight, rationale
                 ─► [code]  sizing: shares from weight, price, cash and limits
                 ─► broker.propose_order  ─► pending, until a human decides

Split by what each part is good at. Whether a brief justifies changing a position,
given what is already held, is judgement: a model. How many shares that is, is
arithmetic: code. The model gets no tools and writes no numbers the broker uses
except a target weight, which code clamps.

What this agent can do, by construction:
  * read the portfolio and propose (broker:read, broker:propose);
  * never approve or execute: it holds no broker:decide scope, and `decide` is not
    on its tool allowlist either. Two independent locks.

Its proposals are idempotent: client_order_id = run:<run id>, so a workflow step
redone after a crash returns the same proposal instead of a second one.
"""

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
risk gate. Decide whether it justifies changing the position in this stock.

- buy: raise the position to target_weight_pct of equity.
- sell: cut the position to target_weight_pct (0 = exit). Only if shares are held.
- hold: no change. Often right: a brief is one view, not a mandate.
- target_weight_pct is at most {max_pos} and should scale with confidence.
- Consider what is already held and how concentrated the portfolio is.
- rationale: 1-3 sentences a human approver can check against the brief.

This is paper trading for research, not investment advice.

Reply with ONLY: {{"action": "buy"|"sell"|"hold", "target_weight_pct": number,
"rationale": str}}"""


def describe(req: ProposeTradeRequest, pf: Portfolio, price: float) -> str:
    b, r = req.brief, req.risk
    held = next((p for p in pf.positions if p.symbol == req.symbol), None)
    lines = [
        f"Symbol: {req.symbol}. Current price: {price}.",
        f"Brief: {b['stance']} (confidence {b['confidence']:.2f}). {b['thesis']}",
        "Key points:", *[f"- {c['text']}" for c in b.get("key_points", [])],
        "Risks:", *[f"- {c['text']}" for c in b.get("risks", [])],
        "Risk gate: {}; flags: {}".format(
            r["verdict"],
            "; ".join(f"{f['rule']}: {f['detail']}" for f in r.get("flags", [])) or "none"),
        (f"Portfolio: equity {pf.equity:,.0f} PKR, cash {pf.cash:,.0f} PKR, "
         f"{len(pf.positions)} positions."),
        (f"Held: {held.quantity} shares, {held.weight_pct}% of equity." if held
         else f"Not held. Largest position: "
              f"{max((p.weight_pct for p in pf.positions), default=0)}% of equity."),
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
        return result.model_copy(update={"reason": "no live price; nothing proposed"}) \
            .model_dump(mode="json")

    async with McpToolset(BROKER, allow={"get_portfolio", "propose_order"}) as broker:
        pf = Portfolio.model_validate(await broker.call("get_portfolio"))
        client = GatewayClient(GATEWAY, caller="portfolio-agent", lane=Lane.pipeline)
        try:
            reply = await client.chat(
                [ChatMessage(role="system", content=SYSTEM.format(max_pos=MAX_POSITION_PCT)),
                 ChatMessage(role="user", content=describe(req, pf, price))],
                tier=Tier.small, max_tokens=700)
            intent, _ = await parse_with_repair(client, reply.message.content or "",
                                                TradeIntent, tier=Tier.small)
            tokens = reply.usage.total_tokens
        finally:
            await client.aclose()

        action, qty, reason = size(intent, req.brief["stance"], req.brief["confidence"], pf,
                                   req.symbol, float(price))
        result = result.model_copy(update={"action": action, "quantity": qty,
                                           "reason": reason, "tokens_used": tokens})
        if action == "hold":
            return result.model_dump(mode="json")

        brief_view = {k: req.brief[k] for k in ("stance", "confidence", "thesis")}
        brief_view |= {"risk": req.risk["verdict"],
                       "flags": [f["rule"] for f in req.risk.get("flags", [])],
                       "key_points": [c["text"] for c in req.brief.get("key_points", [])][:4],
                       "risks": [c["text"] for c in req.brief.get("risks", [])][:4]}
        ids = sorted({i for c in [*req.brief.get("key_points", []), *req.brief.get("risks", [])]
                      for i in c["evidence_ids"]})
        p = await broker.call("propose_order", client_order_id=f"run:{req.run_id}",
                              symbol=req.symbol, side=action, quantity=qty,
                              rationale=reason[:1000], brief=brief_view, run_id=req.run_id,
                              evidence_ids=ids)
    return result.model_copy(update={"proposal_id": p["id"], "status": p["status"]}) \
        .model_dump(mode="json")


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
