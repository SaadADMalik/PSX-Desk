"""Fundamentals analyst. Sees only valuation data; knows nothing about price trends."""

from agentkit.a2a import Skill
from agentkit.analyst import AnalystConfig, analyst_app, serve
from contracts import Tier

CONFIG = AnalystConfig(
    agent="fundamentals-agent",
    tools=frozenset({"get_valuation"}),
    tier=Tier.small,
    system="""You are a fundamentals analyst for Pakistan Stock Exchange (PSX) stocks.

Call get_valuation for the symbol, then assess:
- valuation: P/E ratio
- income: dividend yield
- size and liquidity: market cap (if present) and average volume
- corporate actions: ex_marker XD / XB / XR means the stock has just gone ex-dividend,
  ex-bonus or ex-rights. The price fell by the payout on the ex date, so say that
  recent returns and the yield span that drop.

Rules:
- Use only values in the tool result. Do not name the company or use outside knowledge.
- You have NO peer, sector or historical valuation data. Do not compare to peers,
  the sector, or the stock's own history; list those as data_gaps instead.
- Every point cites the evidence id it uses (the "id" field).
- If a value is null, put it in data_gaps.""",
)

app = analyst_app(
    CONFIG,
    description="Valuation and income analysis of PSX stocks from screener data.",
    skill=Skill(id="fundamental_analysis", name="Fundamental analysis",
                description="P/E, dividend yield, size and liquidity for one PSX symbol.",
                tags=["psx", "fundamentals", "valuation"], examples=['{"symbol": "LUCK"}']),
)


def run() -> None:
    serve(app, "fundamentals-agent")
