"""The single-agent baseline.

One model, every tool, one context window. This is the simplest thing that could
work, and it is what the multi-agent system has to beat in Slice 5 on quality,
groundedness, and cost. If it never does, that is a finding, not a failure.
"""

from dataclasses import dataclass, field

from agentkit import GatewayClient, Tool, parse_with_repair, run_agent
from contracts import Brief, Evidence, Tier, Usage
from marketdata import MarketData, validate_symbol
from opentelemetry import trace

SYSTEM = """You are an equity research analyst covering the Pakistan Stock Exchange (PSX).

Rules:
1. Get facts only from tools. Call get_price_summary and get_valuation for the symbol.
2. Never state a number that is not in a tool result. Never use memory for prices,
   ratios, or company facts.
3. Every key point and risk must cite the evidence id(s) it relies on, from the
   "id" field of tool results.
4. If a value is null or evidence is marked stale, say so in data_gaps.
5. This is research, not investment advice. Be balanced; always give real risks.

When finished, do NOT call a tool to submit the answer. Reply in plain message text
with ONLY a JSON object:
{"symbol": str, "stance": "bullish"|"neutral"|"bearish", "confidence": 0..1,
 "thesis": str,
 "key_points": [{"text": str, "evidence_ids": [str, ...]}, ...],   (1-6 items)
 "risks":      [{"text": str, "evidence_ids": [str, ...]}, ...],   (1-6 items)
 "data_gaps": [str, ...]}"""

SYMBOL_ARG = {
    "type": "object",
    "properties": {"symbol": {"type": "string", "description": "PSX ticker, e.g. LUCK"}},
    "required": ["symbol"],
    "additionalProperties": False,
}


@dataclass
class BriefRun:
    brief: Brief
    evidence: dict[str, Evidence]
    unknown_citations: list[str]
    usage: Usage
    steps: int
    tool_calls: int
    models: list[str]
    repaired: bool
    notes: list[str] = field(default_factory=list)
    cached_calls: int = 0


def build_tools(md: MarketData, registry: dict[str, Evidence]) -> list[Tool]:
    """Every evidence object a tool returns is recorded, so citations can be checked later."""

    async def get_price_summary(symbol: str) -> dict:
        ev = await md.price_summary(symbol)
        registry[ev.id] = ev
        return ev.prompt_view()

    async def get_valuation(symbol: str) -> dict:
        ev = await md.valuation(symbol)
        registry[ev.id] = ev
        return ev.prompt_view()

    return [
        Tool("get_price_summary",
             "Price history summary for a PSX stock: returns, moving averages, RSI, "
             "volatility, drawdown, 52-week range, volume. Computed from end-of-day data.",
             SYMBOL_ARG, get_price_summary),
        Tool("get_valuation",
             "Latest valuation snapshot for a PSX stock: price, P/E, dividend yield, "
             "average volume, market cap if available.",
             SYMBOL_ARG, get_valuation),
    ]


async def write_brief(client: GatewayClient, md: MarketData, symbol: str) -> BriefRun:
    symbol = validate_symbol(symbol)
    # One root span so the whole run, including any repair call, is a single trace.
    with trace.get_tracer("baseline").start_as_current_span("baseline.write_brief") as span:
        span.set_attribute("psx.symbol", symbol)
        return await _write_brief(client, md, symbol)


async def _write_brief(client: GatewayClient, md: MarketData, symbol: str) -> BriefRun:
    registry: dict[str, Evidence] = {}
    result = await run_agent(
        client,
        system=SYSTEM,
        user=f"Write a research brief for {symbol}.",
        tools=build_tools(md, registry),
        tier=Tier.large,
        max_steps=6,
        max_tokens=2500,
    )
    brief, repaired = await parse_with_repair(client, result.text, Brief, tier=Tier.small)

    # Cheap grounding check now; the full number-by-number verifier arrives in Slice 3.
    unknown = sorted(brief.cited_ids() - registry.keys())
    notes = []
    if brief.symbol.upper() != symbol:
        notes.append(f"model wrote symbol {brief.symbol!r}, expected {symbol!r}")
    stale = [ev.kind for ev in registry.values() if ev.stale]
    if stale:
        brief.data_gaps.append(f"served from last-good snapshot (stale): {', '.join(stale)}")
    return BriefRun(brief, registry, unknown, result.usage, result.steps, result.tool_calls,
                    result.models, repaired, notes, result.cached_calls)
