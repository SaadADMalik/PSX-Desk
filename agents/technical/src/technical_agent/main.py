"""Technical analyst. Sees only price history; knows nothing about valuation."""

from agentkit.a2a import Skill
from agentkit.analyst import AnalystConfig, analyst_app, serve
from contracts import Tier

CONFIG = AnalystConfig(
    agent="technical-agent",
    tools=frozenset({"get_price_summary"}),
    tier=Tier.small,
    system="""You are a technical analyst for Pakistan Stock Exchange (PSX) stocks.

Call get_price_summary for the symbol, then assess:
- trend: price relative to the 50- and 200-day moving averages (pct_vs_sma_50/200)
- momentum: RSI(14) and 1m/3m/1y returns
- risk: 60-day volatility and 1-year max drawdown
- liquidity: average volume

Rules:
- Use only values in the tool result. Do not name the company or use outside knowledge.
- Describe relationships accurately: "3.35% above" (copied from pct_vs_sma_50), not "near".
- Every point cites the evidence id it uses (the "id" field).
- If a value is null, put it in data_gaps.""",
)

app = analyst_app(
    CONFIG,
    description="Technical analysis of PSX stocks from end-of-day price history.",
    skill=Skill(id="technical_analysis", name="Technical analysis",
                description="Trend, momentum, volatility and liquidity for one PSX symbol.",
                tags=["psx", "technical", "price"], examples=['{"symbol": "LUCK"}']),
)


def run() -> None:
    serve(app, "technical-agent")
