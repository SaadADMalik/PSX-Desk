import pandas as pd
import pytest
from marketdata import DataUnavailable, MarketData, SourceError, validate_symbol
from marketdata.indicators import max_drawdown, pct_return, price_summary, rsi
from marketdata.source import normalise_history


def frame(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n, freq="B"),
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000.0] * n,
    })


def test_rsi_of_a_steady_rise_is_100():
    assert rsi(pd.Series(range(1, 40), dtype=float)) == 100.0


def test_pct_return_and_drawdown():
    s = pd.Series([100.0, 120.0, 90.0, 110.0])
    assert pct_return(s, 3) == 10.0
    assert max_drawdown(s) == -25.0


def test_summary_handles_short_history():
    out = price_summary(frame([10.0, 11.0, 12.0]))
    assert out["last_close"] == 12.0 and out["sma_200"] is None and out["above_sma_200"] is None


def test_normalise_sorts_ascending_and_drops_anomalies():
    # Oldest day closes at 1, newest at 3; PSX hands rows over newest first.
    raw = frame([1.0, 2.0, 3.0]).iloc[::-1].reset_index(drop=True)
    raw["is_anomaly"] = [False, True, False]  # the middle day is flagged
    out = normalise_history(raw)
    assert list(out["close"]) == [1.0, 3.0]
    assert out["date"].is_monotonic_increasing


@pytest.mark.parametrize("bad", ["../etc", "LU CK", "", "A" * 13])
def test_symbol_validation_rejects_path_tricks(bad):
    with pytest.raises(ValueError):
        validate_symbol(bad)


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


async def test_falls_back_to_last_good_snapshot_and_marks_it_stale(tmp_path):
    src = FlakySource()
    md = MarketData(src, tmp_path)
    live = await md.price_summary("TEST")
    src.up = False
    stale = await md.price_summary("TEST")
    assert not live.stale and stale.stale and stale.source.startswith("snapshot:")
    assert stale.values == live.values and stale.id == live.id  # same facts, same id


async def test_prompt_view_is_stable_across_fetches(tmp_path):
    """Refetching identical facts must give the model an identical prompt (cache hits)."""
    md = MarketData(FlakySource(), tmp_path)
    a, b = await md.price_summary("TEST"), await md.price_summary("TEST")
    assert a.fetched_at != b.fetched_at
    assert a.prompt_view() == b.prompt_view() and "fetched_at" not in a.prompt_view()


async def test_quote_falls_back_too(tmp_path):
    src = FlakySource()
    md = MarketData(src, tmp_path)
    live = await md.valuation("TEST")
    src.up = False
    stale = await md.valuation("TEST")
    assert not live.stale and stale.stale
    assert stale.values["pe_ratio"] == 8.5 and stale.values["dividend_yield_pct"] == 4.0


async def test_no_live_data_and_no_snapshot_is_an_explicit_error(tmp_path):
    src = FlakySource()
    src.up = False
    with pytest.raises(DataUnavailable):
        await MarketData(src, tmp_path).price_summary("TEST")


async def test_quote_finds_symbols_listed_with_a_corporate_action_marker(monkeypatch):
    import pandas as pd
    import psxdata
    from marketdata import PsxDataSource

    empty = pd.DataFrame(columns=["symbol", "price"])
    screen = pd.DataFrame({"symbol": ["HUBCXD", "HUBCO", "CPPL"], "price": [207.02, 1.0, 9.0]})
    monkeypatch.setattr(psxdata, "quote", lambda s: empty)
    monkeypatch.setattr(psxdata, "screener", lambda: screen)
    q = await PsxDataSource(min_interval=0).quote("HUBC")
    assert q["symbol"] == "HUBCXD" and q["ex_marker"] == "XD" and q["price"] == 207.02
