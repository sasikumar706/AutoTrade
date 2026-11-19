from datetime import datetime, timedelta

from app.market import history_stats, plain_trend, quote_summary


def test_quote_summary_change_and_range():
    q = {"last_price": 105.0, "ohlc": {"open": 101, "high": 110, "low": 100, "close": 100.0}, "volume": 1200,
         "depth": {"buy": [{"price": 104.9}], "sell": [{"price": 105.1}]}}
    s = quote_summary(q)
    assert s["change"] == 5.0 and s["change_pct"] == 5.0 and s["direction"] == "UP"
    assert s["range_pos"] == 50 and s["bid"] == 104.9 and s["volume"] == 1200
    assert quote_summary({"last_price": 100.1, "ohlc": {"close": 100.0}})["direction"] == "FLAT"
    assert quote_summary({})["change"] is None


def test_history_stats_returns_and_52w():
    t0 = datetime(2025, 1, 1)
    daily = [{"date": t0 + timedelta(days=i), "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i} for i in range(300)]
    st = history_stats(daily, 399.0)
    assert st["high_52w"] == 400 and st["low_52w"] == 149
    assert st["returns"]["1W"] == round((399 - 394) / 394 * 100, 2) and "1Y" in st["returns"]
    assert history_stats([], None) == {}


def test_plain_trend_words():
    assert plain_trend({"verdict": "BULLISH", "regime": "Trending"}).startswith("Mostly positive")
    assert plain_trend({"verdict": "NEUTRAL", "regime": "Range"}).startswith("Mixed")
