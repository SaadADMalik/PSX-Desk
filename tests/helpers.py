"""Test doubles shared across test modules."""

import json

import pandas as pd
from contracts import ChatMessage, ChatResponse, ToolCall, Usage
from marketdata import SourceError


def answer(content=None, calls=None, cached=False):
    return ChatResponse(message=ChatMessage(role="assistant", content=content, tool_calls=calls),
                        model="fake", usage=Usage(prompt_tokens=10, completion_tokens=5),
                        cached=cached)


def call(name, args, id_="c1"):
    return ToolCall(id=id_, function={"name": name, "arguments": json.dumps(args)})


class FakeClient:
    """Stands in for GatewayClient: plays back scripted model replies."""

    caller = "test"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[ChatMessage]] = []

    async def chat(self, messages, **kw):
        self.seen.append(list(messages))
        return self.replies.pop(0)

    async def aclose(self):
        pass


def frame(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n, freq="B"),
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000.0] * n,
    })


class FlakySource:
    name = "fake"

    def __init__(self):
        self.up = True

    async def history(self, symbol):
        if not self.up:
            raise SourceError("down")
        return frame([float(x) for x in range(100, 160)])

    async def quote(self, symbol):
        if not self.up:
            raise SourceError("down")
        return {"price": 159.0, "pe_ratio": 8.5, "dividend_yield": 4.0}

    async def screener(self):
        if not self.up:
            raise SourceError("down")
        return {"TEST": {"price": 159.0, "change_pct": 1.25, "volume_avg_30d": 1000.0,
                         "change_1y_pct": 12.0, "pe_ratio": 8.5, "dividend_yield": 4.0,
                         "market_cap": None, "sector_code": "0804"},
                "HUBCXD": {"price": 120.0, "change_pct": -0.5, "volume_avg_30d": 5000.0},
                "BOND1": {"price": 100.0, "change_pct": 0.0, "volume_avg_30d": 10.0},
                "TESTNC": {"price": 3.0, "change_pct": 0.0, "volume_avg_30d": 100.0}}

    async def listings(self):
        return {"TEST": {"name": "Test Cement Ltd", "sector": "Cement", "is_etf": False,
                         "is_debt": False},
                "HUBC": {"name": "Hub Power Company", "sector": "Power", "is_etf": False,
                         "is_debt": False},
                "TESTNC": {"name": "", "sector": "", "is_etf": False, "is_debt": False},
                "BOND1": {"name": "Some TFC", "sector": "Bills And Bonds", "is_etf": False,
                          "is_debt": True}}

    async def tickers(self, index):
        return ["AAA", "BBB"]

    async def announcements(self, symbol):
        if not self.up:
            raise SourceError("down")
        return [{"date": "2026-11-13", "title": "Financial Results for the Quarter Ended"},
                {"date": "2026-11-10", "title": "Board Meeting"}]
