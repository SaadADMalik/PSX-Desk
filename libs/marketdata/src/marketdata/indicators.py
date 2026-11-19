"""Indicators computed in code. Pure functions over an ascending OHLCV frame.

Nothing here touches a model. These numbers become Evidence, and models may only
cite them, never restate them from memory.
"""

import math

import pandas as pd

TRADING_DAYS_PER_YEAR = 245  # PSX trades roughly 245 days a year


def _r(x: float | None, nd: int = 2) -> float | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), nd)


def pct_return(close: pd.Series, days: int) -> float | None:
    if len(close) <= days:
        return None
    return _r((close.iloc[-1] / close.iloc[-1 - days] - 1) * 100)


def sma(close: pd.Series, window: int) -> float | None:
    if len(close) < window:
        return None
    return _r(close.iloc[-window:].mean())


def rsi(close: pd.Series, period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(close) <= period:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    return _r(100 - 100 / (1 + avg_gain / avg_loss))


def annualised_volatility(close: pd.Series, window: int = 60) -> float | None:
    rets = close.pct_change().dropna()
    if len(rets) < window:
        return None
    return _r(rets.iloc[-window:].std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100)


def max_drawdown(close: pd.Series, window: int = TRADING_DAYS_PER_YEAR) -> float | None:
    s = close.iloc[-window:]
    if len(s) < 2:
        return None
    dd = s / s.cummax() - 1
    return _r(dd.min() * 100)


def price_summary(df: pd.DataFrame) -> dict:
    """Compact summary of one stock's history. Small on purpose: it goes into a prompt."""
    close = df["close"]
    last_year = df.iloc[-TRADING_DAYS_PER_YEAR:]
    last = close.iloc[-1]
    sma50, sma200 = sma(close, 50), sma(close, 200)
    return {
        "last_close": _r(last),
        "return_1m_pct": pct_return(close, 21),
        "return_3m_pct": pct_return(close, 63),
        "return_1y_pct": pct_return(close, TRADING_DAYS_PER_YEAR),
        "sma_50": sma50,
        "sma_200": sma200,
        # Derived numbers are computed here too. A model asked to say "x% above the
        # 50-day average" will otherwise do the arithmetic itself, and get it wrong.
        "pct_vs_sma_50": None if not sma50 else _r((last / sma50 - 1) * 100),
        "pct_vs_sma_200": None if not sma200 else _r((last / sma200 - 1) * 100),
        "above_sma_200": None if sma200 is None else bool(last > sma200),
        "rsi_14": rsi(close),
        "volatility_60d_annualised_pct": annualised_volatility(close),
        "max_drawdown_1y_pct": max_drawdown(close),
        "high_52w": _r(last_year["high"].max()),
        "low_52w": _r(last_year["low"].min()),
        "pct_below_high_52w": _r((1 - last / last_year["high"].max()) * 100),
        "avg_volume_20d": int(df["volume"].iloc[-20:].mean()) if len(df) >= 20 else None,
        "trading_days_available": len(df),
    }
