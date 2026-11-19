"""Where raw PSX data comes from.

PSX has no official API. `psxdata` scrapes the data portal and already retries
and caches on disk. We add two things on top:

* a polite limiter, so every caller in this process shares one request pace;
* a narrow interface (`PriceSource`), so tests and future sources plug in
  without touching indicator or agent code.
"""

import asyncio
import re
import time
from typing import Protocol

import pandas as pd

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,12}$")
EX_MARKED = r"{}(?:XD|XB|XR)+"  # symbol is validated alphanumeric, so safe in a regex


class SourceError(Exception):
    """The source could not deliver. Callers may fall back to a snapshot."""


class PriceSource(Protocol):
    name: str

    async def history(self, symbol: str) -> pd.DataFrame: ...

    async def quote(self, symbol: str) -> dict: ...


def validate_symbol(symbol: str) -> str:
    """Symbols reach file paths and URLs, so reject anything that is not a plain ticker."""
    s = symbol.strip().upper()
    if not SYMBOL_RE.match(s):
        raise ValueError(f"invalid PSX symbol: {symbol!r}")
    return s


class PoliteLimiter:
    """At most one network call every `min_interval` seconds, shared across tasks."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aexit__(self, *exc: object) -> None:
        self._last = time.monotonic()
        self._lock.release()


class PsxDataSource:
    name = "psxdata/dps.psx.com.pk"

    def __init__(self, min_interval: float = 1.5) -> None:
        self._limiter = PoliteLimiter(min_interval)

    async def history(self, symbol: str) -> pd.DataFrame:
        import psxdata

        async with self._limiter:
            try:
                df = await asyncio.to_thread(psxdata.stocks, symbol)
            except Exception as e:  # psxdata raises its own hierarchy; normalise it
                raise SourceError(f"{type(e).__name__}: {e}") from e
        return normalise_history(df)

    async def quote(self, symbol: str) -> dict:
        import psxdata

        async with self._limiter:
            try:
                q = await asyncio.to_thread(psxdata.quote, symbol)
                if q.empty:
                    # During a corporate action PSX lists the symbol with a marker:
                    # HUBC becomes HUBCXD (ex-dividend), XB ex-bonus, XR ex-rights.
                    screen = await asyncio.to_thread(psxdata.screener)
                    q = screen[screen["symbol"].astype(str).str.fullmatch(EX_MARKED.format(symbol))]
            except Exception as e:
                raise SourceError(f"{type(e).__name__}: {e}") from e
        if q.empty:
            raise SourceError(f"no quote for {symbol}")
        row = q.iloc[0].to_dict()
        row["ex_marker"] = str(row["symbol"])[len(symbol):] or None
        return {k: (None if pd.isna(v) else v) for k, v in row.items()}


    async def screener(self) -> dict[str, dict]:
        """Every listed symbol's latest price, day change and 30-day volume, one request."""
        import psxdata

        async with self._limiter:
            try:
                df = await asyncio.to_thread(psxdata.screener, cache=False)
            except Exception as e:
                raise SourceError(f"{type(e).__name__}: {e}") from e
        def num(v):
            return None if v is None or pd.isna(v) else float(v)

        out = {}
        for r in df.to_dict("records"):
            price = num(r.get("price"))
            if price is None:
                continue
            out[str(r["symbol"])] = {
                "price": price,
                "change_pct": num(r.get("change_pct")),
                "volume_avg_30d": num(r.get("volume_avg_30d")),
                "change_1y_pct": num(r.get("change_1y_pct")),
                "pe_ratio": num(r.get("pe_ratio")),
                "dividend_yield": num(r.get("dividend_yield")),
                "market_cap": num(r.get("market_cap")),
                "sector_code": None if pd.isna(r.get("sector")) else str(r.get("sector")),
            }
        return out

    async def listings(self) -> dict[str, dict]:
        """Every listed security's name and sector (about 1,000, incl. debt), one request."""
        import psxdata

        async with self._limiter:
            try:
                df = await asyncio.to_thread(psxdata.symbols, cache=True)
            except Exception as e:
                raise SourceError(f"{type(e).__name__}: {e}") from e
        return {str(r["symbol"]): {"name": str(r.get("name") or "").strip(),
                                   "sector": str(r.get("sector_name") or "").strip().title(),
                                   "is_etf": bool(r.get("is_etf")),
                                   "is_debt": bool(r.get("is_debt"))}
                for r in df.to_dict("records")}

    async def announcements(self, symbol: str, count: int = 20) -> list[dict]:
        """Company announcements, newest first. psxdata has no public call for these, so
        this uses its HTTP session (headers, PSX request token, retries) directly. That
        is a private API: if psxdata changes it, this raises SourceError and the
        snapshot takes over."""
        from psxdata.scrapers.base import BaseScraper

        from marketdata.news import parse_announcements

        form = {"type": "C", "symbol": symbol, "query": "", "count": str(count),
                "offset": "0", "date_from": "", "date_to": "", "page": "annc"}

        def fetch() -> str:
            return BaseScraper()._request("POST", "https://dps.psx.com.pk/announcements",
                                          data=form).text

        async with self._limiter:
            try:
                html = await asyncio.to_thread(fetch)
            except Exception as e:
                raise SourceError(f"{type(e).__name__}: {e}") from e
        return parse_announcements(html)

    async def tickers(self, index: str) -> list[str]:
        import psxdata

        async with self._limiter:
            try:
                return await asyncio.to_thread(psxdata.tickers, index)
            except Exception as e:
                raise SourceError(f"{type(e).__name__}: {e}") from e


def normalise_history(df: pd.DataFrame) -> pd.DataFrame:
    """Ascending dates, no flagged anomalies, no duplicate days, required columns only."""
    if df is None or df.empty:
        raise SourceError("empty history")
    out = df.copy()
    if "is_anomaly" in out:
        out = out[~out["is_anomaly"].astype(bool)]
    out["date"] = pd.to_datetime(out["date"])
    out = out.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    cols = ["date", "open", "high", "low", "close", "volume"]
    missing = set(cols) - set(out.columns)
    if missing:
        raise SourceError(f"history missing columns: {sorted(missing)}")
    out = out[cols]
    out["volume"] = out["volume"].astype("float64")
    return out
