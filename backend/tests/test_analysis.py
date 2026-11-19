import os
import sys
import tempfile
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("DATA_DIR", os.path.join(tempfile.gettempdir(), "kitealgo-test-data"))

from app.analysis import current_signal, find_strategies, param_grid, rule_based_summary, technical_snapshot  # noqa: E402
from app.backtest import BacktestConfig, prepare_candles  # noqa: E402
from app.indicators import adx, macd  # noqa: E402
from app.strategies import REGISTRY  # noqa: E402
from tests.test_engine import intraday  # noqa: E402


def trend(n=300, step=1.0, start=100.0):
    rows = []
    t0 = pd.Timestamp("2025-01-01")
    for i in range(n):
        p = start + step * i
        rows.append(dict(date=t0 + pd.Timedelta(days=i), open=p, high=p + 1, low=p - 1, close=p + 0.5 * np.sign(step), volume=1000))
    return rows


def test_macd_adx_shapes():
    df = pd.DataFrame(trend())
    line, sig, hist = macd(df["close"])
    a, pdi, mdi = adx(df)
    assert hist.iloc[-1] == pytest.approx(line.iloc[-1] - sig.iloc[-1])
    assert a.iloc[-1] > 25 and pdi.iloc[-1] > mdi.iloc[-1]  # clean uptrend


def test_snapshot_reads_trend_direction():
    up = technical_snapshot(trend(step=1.0))
    dn = technical_snapshot(trend(step=-0.2, start=200))
    assert up["verdict"] == "BULLISH" and up["score"] > 25 and up["regime"] == "TRENDING"
    assert dn["verdict"] == "BEARISH" and dn["score"] < -25
    assert all({"indicator", "vote", "reason"} <= set(s) for s in up["signals"])


def test_param_grids_valid():
    for key in REGISTRY:
        for combo in param_grid(key):
            REGISTRY[key].resolve(combo)  # raises if out of bounds
            assert combo.get("fast", 0) < combo.get("slow", 1e9)


def test_current_signal_on_trend():
    down = trend(100, -1.0, 300)
    up = trend(150, 1.0, 200)
    for i, r in enumerate(up):
        r["date"] = down[-1]["date"] + pd.Timedelta(days=i + 1)
    df = prepare_candles(down + up)  # downtrend then uptrend -> supertrend flips long
    s = current_signal(df, "supertrend", {"period": 10, "multiplier": 3.0}, allow_short=True)
    assert s["position"] == "LONG" and s["action"] in ("HOLD LONG", "BUY")


def test_finder_uses_unseen_data_and_ranks():
    rows = intraday(40, seed=21)
    t = time.time()
    res = find_strategies(rows, BacktestConfig(strategy="sma_cross", product="MIS"))
    elapsed = time.time() - t
    assert res["combinations_tested"] > 50
    assert res["train_period"][1] < res["test_period"][0]  # no overlap
    assert res["ranking"]
    for r in res["ranking"]:
        assert r["now"]["action"] in ("BUY", "SELL", "EXIT", "WAIT", "HOLD LONG", "HOLD SHORT")
        assert r["test"]["trades"] >= 0
    scores = [r["score"] for r in res["ranking"] if r["score"] is not None]
    assert scores == sorted(scores, reverse=True)
    assert elapsed < 120, f"finder too slow: {elapsed:.0f}s"
    text = rule_based_summary("TEST", technical_snapshot(rows), res)
    assert "not a prediction" in text


def test_finder_daily_skips_intraday_only():
    res = find_strategies(trend(400, 0.3), BacktestConfig(strategy="sma_cross", product="CNC", segment="equity_delivery"))
    assert all(r["strategy"] != "orb" for r in res["ranking"])


def test_current_signal_no_flip_means_wait():
    df = prepare_candles(trend(step=1.0))
    s = current_signal(df, "supertrend", {"period": 10, "multiplier": 3.0}, allow_short=True)
    assert s["action"] == "WAIT"  # strategy only enters on a flip, never chases


def test_results_are_strict_json():
    """FastAPI cannot serialise numpy types; everything must be plain Python."""
    import json

    rows = intraday(20)
    json.dumps(technical_snapshot(rows))
    json.dumps(find_strategies(rows, BacktestConfig(strategy="sma_cross")))
    from app.backtest import run_backtest
    json.dumps(run_backtest(rows, BacktestConfig(strategy="supertrend", stop_loss_pct=1)))


def test_current_signal_intraday_resets_each_day():
    import pandas as pd
    from datetime import datetime, timedelta
    from app.analysis import current_signal
    from app.strategies import REGISTRY, Strategy

    rows = []
    for d in (datetime(2026, 10, 30), datetime(2026, 11, 3)):
        for k in range(10):
            rows.append(dict(date=d + timedelta(hours=9, minutes=15 + 5 * k), open=1, high=1, low=1, close=1, volume=0))
    df = pd.DataFrame(rows)

    class Fake(Strategy):
        def compute(self, df, p):
            e = pd.Series(False, index=df.index)
            e.iloc[[3, 15]] = True  # entries at 09:30 on both days, never an exit
            f = pd.Series(False, index=df.index)
            return pd.DataFrame({"entry_long": e, "exit_long": f, "entry_short": f, "exit_short": f})

    REGISTRY["_fake"] = Fake(key="_fake", name="fake")
    try:
        cs = current_signal(df, "_fake", {}, allow_short=True)
        assert cs["position"] == "LONG" and cs["since"].startswith("2026-11-03T09:40")
    finally:
        del REGISTRY["_fake"]


def test_forecast_direction_and_points():
    from app.analysis import forecast
    row = lambda act, robust=True: {"strategy": "supertrend", "name": "Supertrend", "params": {}, "robust": robust,
                                    "now": {"action": act, "since": "2026-11-03T10:15:00"},
                                    "test": {"win_rate_pct": 60.0, "trades": 10},
                                    "moves": {"avg_win_pts": 80.0, "avg_loss_pts": 40.0, "avg_bars": 6}}
    snap = {"close": 24000.0, "verdict": "BULLISH"}
    f = forecast([row("BUY")], snap, "5minute")
    assert f["direction"] == "UP" and f["expected_pts"] == 80 and f["target_price"] == 24080 and f["stop_price"] == 23960
    assert f["hold_minutes"] == 30 and f["edge_pts"] == 32.0 and f["fresh"] and f["agree_with_indicators"]
    assert forecast([row("SELL")], snap, "5minute")["direction"] == "DOWN"
    assert forecast([row("WAIT")], snap, "5minute")["direction"] == "NONE"
    assert forecast([row("BUY", robust=False)], snap, "5minute")["direction"] == "NONE"
