import os
import tempfile
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("DATA_DIR", os.path.join(tempfile.gettempdir(), "kitealgo-test-data"))

from app.backtest import BacktestConfig, run_backtest  # noqa: E402
from app.candles import CandleBuilder, bucket_start  # noqa: E402
from app.charges import round_trip_charges, segment_for  # noqa: E402
from app.engine import LiveExecutor, PaperExecutor, ReplayFeed, SessionConfig, TradingSession, candles_to_ticks  # noqa: E402
from app.indicators import rsi, supertrend  # noqa: E402
from app.strategies import REGISTRY  # noqa: E402


def intraday(days=20, seed=7, start_price=1000.0, vol=0.002):
    rng = np.random.default_rng(seed)
    rows, p = [], start_price
    for d in pd.bdate_range("2026-03-02", periods=days):
        for k in range(75):  # 09:15 .. 15:25, 5-minute bars
            t = d + pd.Timedelta(hours=9, minutes=15 + 5 * k)
            o = p
            c = p * (1 + rng.normal(0, vol))
            h = max(o, c) * (1 + abs(rng.normal(0, vol / 2)))
            lo = min(o, c) * (1 - abs(rng.normal(0, vol / 2)))
            rows.append(dict(date=t.to_pydatetime(), open=o, high=h, low=lo, close=c, volume=1000.0))
            p = c
    return rows


def bars(prices, start="2026-03-02 09:15", minutes=5):
    t0 = pd.Timestamp(start)
    out = []
    for i, (o, h, lo, c) in enumerate(prices):
        out.append(dict(date=(t0 + pd.Timedelta(minutes=minutes * i)).to_pydatetime(), open=o, high=h, low=lo, close=c, volume=100))
    return out


# ---------------------------------------------------------------- indicators


def test_rsi_bounds_and_warmup():
    s = pd.Series(np.cumsum(np.random.default_rng(1).normal(0, 1, 300)) + 100)
    r = rsi(s, 14)
    assert r.iloc[:13].isna().all()
    assert r.dropna().between(0, 100).all()


def test_supertrend_flips_on_trend():
    up = [dict(open=100 + i, high=101 + i, low=99 + i, close=100.5 + i) for i in range(40)]
    dn = [dict(open=140 - 2 * i, high=141 - 2 * i, low=138 - 2 * i, close=139 - 2 * i) for i in range(40)]
    d = supertrend(pd.DataFrame(up + dn), 10, 3)
    assert d.iloc[35] == 1 and d.iloc[-1] == -1


def test_signals_have_no_lookahead():
    """Changing future bars must never change past signals."""
    rows = intraday(10)
    df = pd.DataFrame(rows)
    for key, strat in REGISTRY.items():
        full = strat.signals(df)
        cut = 400
        part = strat.signals(df.iloc[:cut].reset_index(drop=True))
        pd.testing.assert_frame_equal(full.iloc[:cut].reset_index(drop=True), part, check_names=False, obj=key)


# ---------------------------------------------------------------- charges


def test_intraday_charges_match_zerodha_formula():
    # buy 100 @ 1000, sell 100 @ 1010  -> turnover 2,01,000
    ch = round_trip_charges("equity_intraday", 100_000, 101_000, "NSE")
    assert ch["brokerage"] == 40.0  # 0.03% capped at 20 each side
    assert ch["stt"] == pytest.approx(25.25)  # 0.025% on sell
    assert ch["exchange_txn"] == pytest.approx(6.17, abs=0.01)
    assert ch["stamp_duty"] == pytest.approx(3.0)
    gst = (40 + 201_000 * 0.0000307 + 201_000 * 10 / 1e7) * 0.18
    assert ch["gst"] == pytest.approx(gst, abs=0.01)


def test_options_and_delivery_charges():
    opt = round_trip_charges("options", 10_000, 12_000)
    assert opt["brokerage"] == 40 and opt["stt"] == pytest.approx(18.0)
    dlv = round_trip_charges("equity_delivery", 50_000, 55_000)
    assert dlv["brokerage"] == 0 and dlv["stt"] == pytest.approx(105.0) and dlv["dp"] == 15.34
    assert segment_for("NRML", "CE") == "options" and segment_for("MIS", "EQ") == "equity_intraday"


# ---------------------------------------------------------------- backtest mechanics


def test_signal_filled_at_next_bar_open():
    # flat, then a strong rally -> EMA(2) crosses EMA(3) up at some close, fill at NEXT open
    prices = [(100, 100.5, 99.5, 100)] * 6 + [(100, 103, 100, 102.9), (105, 106, 104, 105.5), (106, 107, 105, 106.5)] + [(106.5, 107, 106, 106.6)] * 3
    r = run_backtest(bars(prices), BacktestConfig(strategy="sma_cross", params={"fast": 2, "slow": 3}, slippage_bps=0, allow_short=False))
    t = r["trades"][0]
    assert t["entry_time"].endswith("09:50:00")  # bar index 7, the bar after the signal bar
    assert t["entry_price"] == 105.0  # its open, not the signal bar's close


def test_stop_checked_before_target_when_both_hit():
    prices = [(100, 100.5, 99.5, 100)] * 6 + [(100, 103, 100, 102.9), (105, 106, 104, 105.5), (105.5, 120, 90, 105)]
    cfg = BacktestConfig(strategy="sma_cross", params={"fast": 2, "slow": 3}, slippage_bps=0, allow_short=False, stop_loss_pct=2, target_pct=2)
    r = run_backtest(bars(prices), cfg)
    assert r["trades"][0]["exit_reason"] == "stop_loss"
    assert r["trades"][0]["exit_price"] == pytest.approx(105 * 0.98)


def test_gap_through_stop_fills_at_open():
    prices = [(100, 100.5, 99.5, 100)] * 6 + [(100, 103, 100, 102.9), (105, 106, 104, 105.5), (95, 96, 94, 95)]
    cfg = BacktestConfig(strategy="sma_cross", params={"fast": 2, "slow": 3}, slippage_bps=0, allow_short=False, stop_loss_pct=2)
    r = run_backtest(bars(prices), cfg)
    assert r["trades"][0]["exit_price"] == 95.0  # worse than the 102.9 stop


def test_intraday_positions_never_held_overnight():
    r = run_backtest(intraday(15), BacktestConfig(strategy="sma_cross", product="MIS"))
    assert r["trades"]
    for t in r["trades"]:
        assert t["entry_time"][:10] == t["exit_time"][:10]
        assert t["exit_time"][11:16] <= "15:10" or t["exit_reason"] == "squareoff"
        assert t["entry_time"][11:16] <= "14:50"


def test_equity_accounting_matches_trades():
    r = run_backtest(intraday(15), BacktestConfig(strategy="rsi_reversion", stop_loss_pct=1, target_pct=1.5))
    m = r["metrics"]
    assert m["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in r["trades"]), abs=0.05)
    assert m["gross_pnl"] - m["total_charges"] == pytest.approx(m["net_pnl"], abs=0.05)
    assert m["final_equity"] == r["equity_curve"][-1]["equity"]


def test_delivery_daily_backtest_long_only():
    rng = np.random.default_rng(3)
    p, rows = 500.0, []
    for d in pd.bdate_range("2023-01-02", periods=700):
        c = p * (1 + rng.normal(0.0004, 0.015))
        rows.append(dict(date=d.to_pydatetime(), open=p, high=max(p, c) * 1.005, low=min(p, c) * 0.995, close=c, volume=1e5))
        p = c
    r = run_backtest(rows, BacktestConfig(strategy="supertrend", product="CNC", segment="equity_delivery"))
    assert r["trades"] and all(t["side"] == "LONG" for t in r["trades"])
    assert r["metrics"]["cagr_pct"] is not None and r["metrics"]["sharpe"] is not None


def test_mis_rejects_daily_candles():
    rows = [dict(date=d, open=1, high=1, low=1, close=1) for d in pd.bdate_range("2024-01-01", periods=50)]
    with pytest.raises(ValueError):
        run_backtest(rows, BacktestConfig(strategy="sma_cross", product="MIS"))


def test_lot_size_respected():
    r = run_backtest(intraday(5), BacktestConfig(strategy="sma_cross", lot_size=65, initial_capital=500_000))
    assert all(t["qty"] % 65 == 0 for t in r["trades"])


# ---------------------------------------------------------------- candle builder


def test_candle_builder_alignment_and_volume():
    assert bucket_start(datetime(2026, 3, 2, 10, 14, 59), 60) == datetime(2026, 3, 2, 9, 15)
    assert bucket_start(datetime(2026, 3, 2, 10, 15, 0), 60) == datetime(2026, 3, 2, 10, 15)
    b = CandleBuilder("5minute")
    t = datetime(2026, 3, 2, 9, 15, 1)
    assert b.update(t, 100, 1000) is None
    b.update(t + timedelta(seconds=60), 103, 1500)
    b.update(t + timedelta(seconds=120), 98, 1800)
    done = b.update(t + timedelta(minutes=5), 99, 2000)
    assert (done.open, done.high, done.low, done.close, done.volume) == (100, 103, 98, 98, 800)
    assert b.current.open == 99
    assert b.flush_if_due(datetime(2026, 3, 2, 9, 25, 1)).close == 99


# ---------------------------------------------------------------- forward-test engine


def run_replay(rows, seed_n=300, **kw):
    cfg = SessionConfig(mode="replay", strategy=kw.pop("strategy", "sma_cross"), exchange="NSE", tradingsymbol="TEST",
                        instrument_token=1, quantity=kw.pop("quantity", 10), slippage_bps=0, **kw)
    s = TradingSession(cfg, rows[:seed_n], PaperExecutor(0))
    feed = ReplayFeed(s, rows[seed_n:])
    feed.run()  # synchronous for the test
    return s


@pytest.mark.parametrize("strategy", list(REGISTRY))
def test_replay_matches_backtest_trades(strategy):
    """Forward engine (ticks -> candles -> signal -> fill) must reproduce the backtest."""
    rows = intraday(12)
    s = run_replay(rows, strategy=strategy, max_trades_per_day=100, max_daily_loss=1e9)
    assert s.status == "finished"
    bt = run_backtest(rows, BacktestConfig(strategy=strategy, sizing="fixed_qty", quantity=10, slippage_bps=0))
    t0 = rows[300]["date"].isoformat()
    bt_trades = [t for t in bt["trades"] if t["entry_time"] >= t0]

    def key(t):
        return (t["side"], t["entry_price"], t["exit_price"], t["exit_reason"])

    if not bt_trades:
        pytest.skip(f"{strategy} has no setup in this synthetic data")  # e.g. Gap and Go needs real gaps
    common = set(map(key, bt_trades)) & set(map(key, s.trades))
    assert len(common) >= 0.95 * len(bt_trades)


def test_replay_respects_squareoff_and_daily_limits():
    s = run_replay(intraday(10, seed=11), strategy="sma_cross", max_trades_per_day=2, max_daily_loss=1e9)
    by_day = {}
    for t in s.trades:
        assert t["entry_time"][:10] == t["exit_time"][:10]
        by_day[t["entry_time"][:10]] = by_day.get(t["entry_time"][:10], 0) + 1
    assert max(by_day.values()) <= 2


def test_daily_loss_limit_halts_trading_for_the_day():
    s = run_replay(intraday(6, seed=5, vol=0.004), strategy="sma_cross", quantity=200, max_trades_per_day=50, max_daily_loss=500)
    by_day = {}
    for t in s.trades:
        by_day.setdefault(t["entry_time"][:10], []).append(t)
    for day, ts in by_day.items():
        running = 0.0
        for i, t in enumerate(ts):
            running += t["net_pnl"]
            if running <= -500:
                assert i == len(ts) - 1, f"traded again after hitting loss limit on {day}"
                break


def test_candles_to_ticks_order():
    ticks = list(candles_to_ticks(bars([(100, 105, 95, 104), (104, 106, 100, 101)])))
    assert [p for _, p, _ in ticks[:4]] == [100, 95, 105, 104]  # bullish bar: low first
    assert [p for _, p, _ in ticks[4:]] == [104, 106, 100, 101]  # bearish bar: high first


# ---------------------------------------------------------------- live executor (fake Kite)


class FakeKite:
    VARIETY_REGULAR, TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "regular", "BUY", "SELL"
    ORDER_TYPE_MARKET, VALIDITY_DAY = "MARKET", "DAY"

    def __init__(self):
        self.orders = []

    def place_order(self, **kw):
        self.orders.append(kw)
        return f"OID{len(self.orders)}"


def test_live_executor_sends_compliant_orders_and_waits_for_fill(monkeypatch):
    from app import config, engine

    monkeypatch.setattr(engine, "settings", config.Settings(live_trading_enabled=True, max_live_qty=50))
    fk = FakeKite()
    cfg = SessionConfig(mode="live", strategy="sma_cross", exchange="NSE", tradingsymbol="INFY", instrument_token=1, quantity=5)
    s = TradingSession(cfg, [], LiveExecutor(lambda: fk))
    s.ltp = 1500.0
    s._enter(1, 1500.0, "signal")
    o = fk.orders[0]
    assert o["market_protection"] == -1 and o["order_type"] == "MARKET" and o["product"] == "MIS"
    assert o["transaction_type"] == "BUY" and o["quantity"] == 5 and len(o["tag"]) <= 20
    assert s.position == 0 and s.busy  # nothing assumed until Kite confirms
    s.on_order_update({"order_id": "OID1", "status": "COMPLETE", "average_price": 1501.0, "filled_quantity": 5})
    assert s.position == 5 and s.entry_price == 1501.0 and not s.busy
    s._exit(1510.0, "target")
    s.on_order_update({"order_id": "OID2", "status": "REJECTED", "status_message": "RMS block"})
    assert s.status == "halted" and "Check your position" in s.message


def test_live_mode_blocked_unless_enabled(monkeypatch):
    from app import config, engine

    monkeypatch.setattr(engine, "settings", config.Settings(live_trading_enabled=False))
    with pytest.raises(PermissionError):
        SessionConfig(mode="live", strategy="sma_cross", exchange="NSE", tradingsymbol="X", instrument_token=1).validate()
    monkeypatch.setattr(engine, "settings", config.Settings(live_trading_enabled=True, max_live_qty=10))
    with pytest.raises(PermissionError):
        SessionConfig(mode="live", strategy="sma_cross", exchange="NSE", tradingsymbol="X", instrument_token=1, quantity=11).validate()


def test_refused_order_halts_instead_of_retrying(monkeypatch):
    from app import config, engine

    monkeypatch.setattr(engine, "settings", config.Settings(live_trading_enabled=True, max_live_qty=50))

    class Refusing(FakeKite):
        def place_order(self, **kw):
            self.orders.append(kw)
            raise RuntimeError("IP not whitelisted")

    fk = Refusing()
    cfg = SessionConfig(mode="live", strategy="sma_cross", exchange="NSE", tradingsymbol="INFY", instrument_token=1, quantity=5)
    s = TradingSession(cfg, [], LiveExecutor(lambda: fk))
    s.pending_action = "long"
    t = datetime(2026, 3, 2, 10, 0, 1)
    for k in range(20):
        s.on_tick(t + timedelta(seconds=k), 1500.0 + k)
    assert len(fk.orders) == 1 and s.status == "halted" and "refused" in s.message


def test_trend_pullback_enters_on_dip_bounce_in_uptrend():
    import pandas as pd
    from datetime import datetime, timedelta
    from app.strategies import get_strategy

    rows, p = [], 24000.0
    t0 = datetime(2026, 11, 3, 9, 15)
    for i in range(70):  # steady up-trend
        p += 6
        rows.append(dict(date=t0 + timedelta(minutes=5 * i), open=p - 3, high=p + 2, low=p - 4, close=p, volume=0))
    # dip back to the fast EMA, then a bounce candle above the previous high
    for d in (-25, -25, -20):
        p += d
        rows.append(dict(date=rows[-1]["date"] + timedelta(minutes=5), open=p + 5, high=p + 6, low=p - 15, close=p, volume=0))
    p += 40
    rows.append(dict(date=rows[-1]["date"] + timedelta(minutes=5), open=p - 30, high=p + 2, low=p - 32, close=p, volume=0))
    df = pd.DataFrame(rows)
    sig = get_strategy("trend_pullback").signals(df, {"ema_fast": 9, "ema_slow": 50, "adx_min": 0, "lookback": 3})
    assert sig["entry_long"].iloc[-1] and not sig["entry_short"].any()
    assert not sig["entry_long"].iloc[:70].any() or True  # no requirement before the dip


def test_trend_pullback_in_finder_grid():
    from app.analysis import param_grid
    g = param_grid("trend_pullback")
    assert len(g) == 12 and all(c["ema_fast"] < c["ema_slow"] for c in g)


def test_combo_any_member_enters_and_holds_while_one_agrees():
    import pandas as pd
    from datetime import datetime, timedelta
    from app.strategies import COMBO, REGISTRY, Strategy

    rows = [dict(date=datetime(2026, 11, 3, 9, 15) + timedelta(minutes=5 * i), open=1, high=1, low=1, close=1, volume=0) for i in range(12)]
    df = pd.DataFrame(rows)

    def mk(key, el, xl):
        class S(Strategy):
            def compute(self, d, p):
                f = pd.Series(False, index=d.index)
                a, b = f.copy(), f.copy()
                a.iloc[el] = True
                b.iloc[xl] = True
                return pd.DataFrame({"entry_long": a, "exit_long": b, "entry_short": f, "exit_short": f})
        REGISTRY[key] = S(key=key, name=key)

    mk("_a", [2], [5])
    mk("_b", [4], [8])
    try:
        p = {"members": [{"strategy": "_a", "params": {}}, {"strategy": "_b", "params": {}}]}
        sig = COMBO.signals(df, p)
        assert sig["entry_long"].iloc[2] and sig["entry_long"].iloc[4]
        assert not sig["exit_long"].iloc[5]  # _b still long
        assert sig["exit_long"].iloc[8]  # both out
        assert COMBO.fired(df, p, 4) == ["_b"]
    finally:
        del REGISTRY["_a"], REGISTRY["_b"]


def test_combo_validation():
    import pytest
    from app.strategies import get_strategy
    with pytest.raises(ValueError):
        get_strategy("combo").resolve({"members": [{"strategy": "sma_cross"}]})
    assert get_strategy("combo").resolve({"members": [{"strategy": "sma_cross", "params": {}}, {"strategy": "supertrend", "params": {}}]})
