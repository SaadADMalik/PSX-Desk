"""MarketData: the one entry point agents (and later the MCP server) use.

Returns Evidence, never raw frames. Tries the live source, falls back to the
last-good snapshot, and says which one it used.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from contracts import Evidence

from marketdata.indicators import price_summary
from marketdata.news import news_summary
from marketdata.snapshot import SnapshotStore
from marketdata.source import PriceSource, PsxDataSource, SourceError, validate_symbol

log = logging.getLogger(__name__)

# The screener also has `change_1y_pct`, but it disagrees with the 1-year return we
# compute from history (different, undocumented definition). One quantity must have
# one source of truth, so we keep ours and drop theirs.
QUOTE_FIELDS = {
    "price": "price",
    "pe_ratio": "pe_ratio",
    "dividend_yield": "dividend_yield_pct",
    "volume_avg_30d": "avg_volume_30d",
    "market_cap": "market_cap",
    "sector": "sector_code",
    # XD = ex-dividend, XB = ex-bonus, XR = ex-rights. The price drops by the payout
    # on the ex date, so returns across it are not like-for-like.
    "ex_marker": "ex_marker",
}


PKT = ZoneInfo("Asia/Karachi")


class DataUnavailable(Exception):
    """Neither the live source nor a snapshot could answer."""


class MarketData:
    def __init__(self, source: PriceSource | None = None, snapshot_dir: Path | None = None) -> None:
        self.source = source or PsxDataSource()
        self.snapshots = SnapshotStore(snapshot_dir or Path("data/snapshots"))

    async def _history(self, symbol: str) -> tuple[pd.DataFrame, str, bool, datetime]:
        try:
            df = await self.source.history(symbol)
            self.snapshots.save_history(symbol, df)
            return df, self.source.name, False, datetime.now(UTC)
        except SourceError as e:
            log.warning("live history failed for %s (%s); trying snapshot", symbol, e)
            snap = self.snapshots.load_history(symbol)
            if snap is None:
                raise DataUnavailable(f"no live data and no snapshot for {symbol}") from e
            df, saved_at = snap
            return df, f"snapshot:{self.source.name}", True, saved_at

    async def price_summary(self, symbol: str) -> Evidence:
        symbol = validate_symbol(symbol)
        df, source, stale, fetched_at = await self._history(symbol)
        return Evidence(
            symbol=symbol,
            kind="price_summary",
            values=price_summary(df),
            source=source,
            as_of=df["date"].iloc[-1].date().isoformat(),
            fetched_at=fetched_at,
            stale=stale,
        )

    async def valuation(self, symbol: str) -> Evidence:
        symbol = validate_symbol(symbol)
        try:
            raw = await self.source.quote(symbol)
            self.snapshots.save_quote(symbol, raw)
            source, stale, fetched_at = self.source.name, False, datetime.now(UTC)
        except SourceError as e:
            snap = self.snapshots.load_quote(symbol)
            if snap is None:
                raise DataUnavailable(f"no live quote and no snapshot for {symbol}") from e
            raw, fetched_at = snap
            source, stale = f"snapshot:{self.source.name}", True
        values = {out: raw.get(src) for src, out in QUOTE_FIELDS.items()}
        return Evidence(
            symbol=symbol,
            kind="valuation",
            values=values,
            source=source,
            as_of=fetched_at.date().isoformat(),
            fetched_at=fetched_at,
            stale=stale,
        )

    async def announcements(self, symbol: str) -> Evidence:
        symbol = validate_symbol(symbol)
        fetch = getattr(self.source, "announcements", None)
        try:
            if fetch is None:
                raise SourceError("source has no announcements")
            items = await fetch(symbol)
            self.snapshots.save_json(symbol, "news", items)
            source, stale, fetched_at = self.source.name, False, datetime.now(UTC)
        except SourceError as e:
            snap = self.snapshots.load_json(symbol, "news")
            if snap is None:
                raise DataUnavailable(f"no live announcements and no snapshot for {symbol}") from e
            items, fetched_at = snap
            source, stale = f"snapshot:{self.source.name}", True
        today = fetched_at.astimezone(PKT).date()
        return Evidence(
            symbol=symbol,
            kind="news",
            values=news_summary(items, today),
            source=source,
            as_of=today.isoformat(),
            fetched_at=fetched_at,
            stale=stale,
        )

    # ------------------------------------------------------------ charts and live board
    #
    # Not Evidence: these feed charts and the live price board, not briefs, so they
    # carry no evidence id and cannot be cited.

    async def price_history(self, symbol: str, days: int = 365) -> dict:
        """Daily closes and volume for charts (most recent `days` trading days)."""
        symbol = validate_symbol(symbol)
        df, source, stale, _ = await self._history(symbol)
        df = df.tail(max(5, min(days, 2000)))
        return {"symbol": symbol, "source": source, "stale": stale,
                "as_of": df["date"].iloc[-1].date().isoformat(),
                "points": [{"date": d.date().isoformat(), "close": round(float(c), 4),
                            "volume": float(v)}
                           for d, c, v in zip(df["date"], df["close"], df["volume"],
                                              strict=True)]}

    async def quotes(self, symbols: list[str], max_age_s: float = 50.0) -> dict:
        """Latest price and day change for many symbols in ONE request to PSX (the
        screener), cached briefly so many viewers cost one fetch."""
        wanted = [validate_symbol(s) for s in symbols]
        fetched, rows = await self._screen(max_age_s)
        out = {}
        for s in wanted:
            r = rows.get(s) or next((v for k, v in rows.items()
                                     if k.startswith(s) and k[len(s):] in EX_SUFFIXES), None)
            if r:
                out[s] = {k: r.get(k) for k in ("price", "change_pct", "volume_avg_30d")}
        return {"fetched_at": fetched.isoformat(), "quotes": out}

    async def _screen(self, max_age_s: float) -> tuple[datetime, dict]:
        """The whole market in ONE request to PSX (the screener), cached briefly so many
        viewers and callers cost one fetch. Last good copy if PSX fails."""
        now = datetime.now(UTC)
        cached = getattr(self, "_board", None)
        if cached is None or (now - cached[0]).total_seconds() > max_age_s:
            screen = getattr(self.source, "screener", None)
            if screen is None:
                raise DataUnavailable("source has no market screener")
            try:
                rows = await screen()
            except SourceError as e:
                if cached is None:
                    raise DataUnavailable(f"no live quotes: {e}") from e
                rows = None
            if rows is not None:
                cached = (now, rows)
                self._board = cached
        return cached

    async def _listings(self) -> dict[str, dict]:
        """Names and sectors; they change rarely, so cached for 12 hours."""
        now = datetime.now(UTC)
        cached = getattr(self, "_names", None)
        if cached is None or (now - cached[0]).total_seconds() > 12 * 3600:
            fetch = getattr(self.source, "listings", None)
            try:
                names = await fetch() if fetch else {}
            except SourceError as e:
                log.warning("no listings: %s", e)
                names = cached[1] if cached else {}
            cached = (now, names)
            self._names = cached
        return cached[1]

    async def board(self, max_age_s: float = 50.0) -> dict:
        """Every traded stock: name, sector, price, day and 1-year change, P/E, dividend
        yield, market cap, volume. Symbols PSX has renamed for an ex-dividend period
        (HUBCXD) are reported under their usual symbol (HUBC), marked `ex`."""
        fetched, rows = await self._screen(max_age_s)
        names = await self._listings()
        out = []
        for sym, r in rows.items():
            base, ex = sym, None
            if sym not in names:
                for suffix in sorted(EX_SUFFIXES, key=len, reverse=True):
                    if sym.endswith(suffix) and sym[:-len(suffix)] in names:
                        base, ex = sym[:-len(suffix)], suffix
                        break
            info = names.get(base, {})
            if not info.get("name"):  # unknown, or listed with a blank name
                # Separate securities of a listed company: same name, labelled.
                for suffix, label in SPECIAL_SUFFIXES.items():
                    parent = names.get(sym[:-len(suffix)]) if sym.endswith(suffix) else None
                    if parent:
                        info = {**parent, **{k: v for k, v in info.items() if v},
                                "name": f"{parent['name']} ({label})"}
                        break
            if info.get("is_debt"):
                continue
            out.append({"symbol": base, "name": info.get("name") or base,
                        "sector": info.get("sector") or "", "etf": info.get("is_etf", False),
                        "ex": ex, **{k: r.get(k) for k in (
                            "price", "change_pct", "change_1y_pct", "pe_ratio",
                            "dividend_yield", "market_cap", "volume_avg_30d")}})
        out.sort(key=lambda s: s["symbol"])
        return {"fetched_at": fetched.isoformat(), "count": len(out), "stocks": out}


EX_SUFFIXES = {"XD", "XB", "XR", "XDXB", "XDXR", "XBXR"}
# Other securities PSX lists under a company's symbol plus a suffix.
SPECIAL_SUFFIXES = {"NC": "non-compliant segment", "NV": "non-voting", "R1": "rights",
                    "R2": "rights", "R": "rights"}
