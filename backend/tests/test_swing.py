from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.autotrade import Deps
from app.swing import SwingConfig, SwingTrader, backtest, monthly_expiries, trend_state
from tests.test_options import FakeKite, make_instruments


def swing_instruments():
    base = make_instruments()
    rows, tok = [], 900000
    for exp in ("2026-11-24", "2026-12-29", "2027-01-26"):
        for k in range(23000, 25001, 50):
            for ot in ("CE", "PE"):
                tok += 1
                rows.append(dict(instrument_token=tok, exchange="NFO", tradingsymbol=f"NIFTY{exp[2:4]}M{exp[5:7]}{k}{ot}", name="NIFTY",
                                 instrument_type=ot, segment="NFO-OPT", expiry=exp, strike=float(k), lot_size=65, tick_size=0.05))
    return pd.concat([base, pd.DataFrame(rows)], ignore_index=True)


INST = swing_instruments()


class SpotKite(FakeKite):
    def __init__(self, spot=24000.0, premium=300.0):
        super().__init__(premium)
        self.spot = spot

    def quote(self, keys):
        out = super().quote(keys)
        for k in keys:
            if k in ("NSE:NIFTY 50", "NSE:NIFTY BANK", "BSE:SENSEX"):
                out[k] = {"last_price": self.spot}
        return out


def daily_rows(trend: float, n=120, end=date(2026, 10, 27), start_px=23000.0):
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    px = start_px + trend * np.arange(n)
    return [{"date": d.to_pydatetime(), "open": p, "high": p + 20, "low": p - 20, "close": p, "volume": 0} for d, p in zip(days, px)]


def make_swing(trend=+10.0, spot=24200.0, **over):
    k = SpotKite(spot=spot)
    rows = daily_rows(trend)
    reg = []
    deps = Deps(instruments=lambda: INST, quote=k.quote, history=lambda tok, iv, a, b: rows, kite_getter=lambda: k,
                margin_available=lambda: 1e6, order_margin=lambda o: 1.0, data_dir=None)
    w = SwingTrader(SwingConfig(**over), deps, reg.append)
    return w, reg, k


def test_monthly_expiry_and_trend_state():
    assert monthly_expiries(["2026-11-03", "2026-11-10", "2026-11-24", "2026-12-01", "2026-12-29"]) == ["2026-11-24", "2026-12-29"]
    up = trend_state(pd.Series(np.arange(100, dtype=float)), 10, 50)
    assert up["trend"] == "UP" and up["fast"] > up["slow"]
    dn = trend_state(pd.Series(np.arange(100, 0, -1, dtype=float)), 10, 50)
    assert dn["trend"] == "DOWN"
    with pytest.raises(ValueError):
        trend_state(pd.Series(np.arange(20, dtype=float)), 10, 50)


def test_uptrend_buys_monthly_itm_ce_with_enough_days():
    w, reg, _ = make_swing(trend=+10.0)
    w.enabled = True
    now = datetime(2026, 10, 28, 15, 16)
    w.on_clock(now)
    assert len(reg) == 1
    p = reg[0]
    assert p.spec.instrument_type == "CE" and p.spec.product == "NRML" and p.spec.source == f"swing:{w.id}"
    assert p.spec.expiry == "2026-11-24" or str(p.spec.expiry)[:10] == "2026-11-24"  # 27 days left >= 20
    assert p.spec.strike == 24100.0  # two strikes in the money (default)
    assert p.state == "POSITION_OPEN" and p.target is None
    loss_at_stop = (p.entry_price - p.stop) * p.spec.qty
    assert loss_at_stop == pytest.approx(1_000_000 * 0.02, rel=0.03)  # stop set so one trade loses at most 2% of the money
    w.on_clock(now + timedelta(minutes=2))  # once a day only
    assert len(reg) == 1
    assert w.signal["trend"] == "UP"


def test_downtrend_exits_and_stays_out_and_roll():
    w, reg, k = make_swing(trend=+10.0)
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    p = reg[0]
    # trend flips down the next day
    rows_dn = daily_rows(-15.0, end=date(2026, 10, 28))
    w.deps.history = lambda tok, iv, a, b: rows_dn
    w.on_clock(datetime(2026, 10, 29, 15, 16))
    assert p.state == "POSITION_CLOSED" and p.exit_reason == "trend_down" and len(reg) == 1
    w.on_clock(datetime(2026, 10, 30, 15, 16))
    assert len(reg) == 1  # stays out
    # roll: uptrend again, position close to expiry -> sell and buy the next monthly
    w2, reg2, _ = make_swing(trend=+10.0)
    w2.enabled = True
    w2.on_clock(datetime(2026, 10, 28, 15, 16))
    first = reg2[0]
    w2.on_clock(datetime(2026, 11, 18, 15, 16))  # 6 days left on 2026-11-24
    assert first.exit_reason == "roll" and len(reg2) == 2
    assert str(reg2[1].spec.expiry)[:10] == "2026-12-29"


def test_no_buy_before_check_time_or_when_disabled_and_premium_cap():
    w, reg, _ = make_swing(trend=+10.0)
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    assert not reg  # not started
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 11, 0))
    assert not reg  # before the daily check time
    w3, reg3, _ = make_swing(trend=+10.0, max_premium=5000.0)  # 1 lot = 300 x 65 = 19,500
    w3.enabled = True
    w3.on_clock(datetime(2026, 10, 28, 15, 16))
    assert not reg3 and any("premium cap" in e["msg"] for e in w3.timeline)
    with pytest.raises(ValueError):
        SwingConfig(fast=50, slow=10).validate()
    with pytest.raises(ValueError):
        SwingConfig(check_time="10:00").validate()


def test_backtest_rule_shape():
    idx = pd.bdate_range("2020-01-01", periods=600)
    # up 300 days, down 150, up 150
    px = np.r_[np.linspace(10000, 13000, 300), np.linspace(13000, 11000, 150), np.linspace(11000, 14000, 150)]
    r = backtest(pd.DataFrame({"close": px}, index=idx), SwingConfig(), lot=65, step=50.0)
    assert r["trades"] >= 2 and r["net"] > 0 and r["buy_hold"] > 0
    assert 0 < r["in_market_pct"] < 100 and r["max_drawdown"] <= 0
    flat = backtest(pd.DataFrame({"close": np.linspace(13000, 9000, 400)}, index=pd.bdate_range("2020-01-01", periods=400)),
                    SwingConfig(), lot=65, step=50.0)
    assert flat["trades"] == 0 and flat["net"] == 0


def test_persistence_roundtrip_via_store():
    w, reg, _ = make_swing(trend=+10.0)
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    d = w.to_store()
    assert d["enabled"] and d["position_id"] == reg[0].id and d["last_check"] == "2026-10-28"
    snap = w.snapshot(datetime(2026, 10, 29, 10, 0))
    assert snap["position"]["days_left"] == 26 and snap["signal"]["trend"] == "UP"


def test_capital_sizing_grows_and_shrinks_lots():
    w, reg, k = make_swing(trend=+10.0, sizing="capital", capital=200000.0, alloc_pct=20.0, max_lots=10)
    n, why = w.lots_for(300.0, 65)  # 20% of 2L = 40,000 / 19,500 per lot = 2 lots
    assert n == 2 and "20%" in why
    w.realized = 200000.0  # money doubled -> 4 lots
    assert w.lots_for(300.0, 65)[0] == 4
    w.realized = -170000.0  # 30k left: 20% = 6k < 1 lot, but can afford one -> 1
    assert w.lots_for(300.0, 65)[0] == 1
    w.realized = -185000.0  # 15k left: can't afford a lot -> 0
    assert w.lots_for(300.0, 65)[0] == 0
    w.realized = 5_000_000.0
    assert w.lots_for(300.0, 65)[0] == 10  # capped
    f, _, _ = make_swing(trend=+10.0, sizing="fixed", lots=3)
    assert f.lots_for(300.0, 65)[0] == 3
    # settle adds a closed trade's net once
    w2, reg2, _ = make_swing(trend=+10.0, capital=200000.0)
    w2.enabled = True
    w2.on_clock(datetime(2026, 10, 28, 15, 16))
    p = reg2[0]
    assert p.filled_qty == 65  # default 10% of ₹2L = ₹20,000 -> 1 lot at ₹300 x 65
    p.request_exit("manual_close")
    w2.settle(); w2.settle()
    assert w2.realized == pytest.approx(float(p.net), abs=0.01) and w2.equity == pytest.approx(200000 + float(p.net), abs=0.01)


def test_backtest_reports_capital_path():
    idx = pd.bdate_range("2020-01-01", periods=600)
    px = np.r_[np.linspace(10000, 13000, 300), np.linspace(13000, 11000, 150), np.linspace(11000, 14000, 150)]
    r = backtest(pd.DataFrame({"close": px}, index=idx), SwingConfig(sizing="capital", capital=200000, alloc_pct=20), lot=65, step=50.0)
    assert r["capital_end"] > 200000 and r["lots_range"][1] >= r["lots_range"][0] >= 1
    assert set(r["capital_yearly_pct"]) <= {"2020", "2021", "2022"} and r["capital_worst_drop"] <= 0



def test_book_half_and_trailing_exit():
    w, reg, k = make_swing(trend=+10.0, book_half_pct=4.0, trail_atr=3.0, sizing="fixed", lots=2)
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    p = reg[0]
    assert p.filled_qty == 130 and p.stats["spot_at_entry"] == 24200.0
    k.spot = 24200.0 * 1.05  # +5% -> book half (uptrend still on)
    w.on_clock(datetime(2026, 10, 29, 15, 16))
    assert p.stats.get("booked_half") and p.filled_qty == 65 and p.booked and p.is_open
    # sharp fall well below the best close but averages still up -> trailing exit, then no re-buy until a fresh trend
    k.spot = 24200.0 * 1.05 - 1000
    w.on_clock(datetime(2026, 10, 30, 15, 16))
    assert p.state == "POSITION_CLOSED" and p.exit_reason == "trail" and w.wait_cross
    k.spot = 24300.0
    w.on_clock(datetime(2026, 11, 2, 15, 16))
    assert len(reg) == 1 and any("fresh uptrend" in e["msg"] for e in w.timeline)


def test_backtest_options_affect_results():
    idx = pd.bdate_range("2019-01-01", periods=700)
    rng = np.random.default_rng(3)
    px = 10000 + np.cumsum(rng.normal(6, 60, 700))
    df = pd.DataFrame({"close": px, "high": px + 40, "low": px - 40}, index=idx)
    base = backtest(df, SwingConfig(itm_strikes=1), lot=65, step=50.0)
    deeper = backtest(df, SwingConfig(itm_strikes=2), lot=65, step=50.0)
    booked = backtest(df, SwingConfig(book_half_pct=4.0, alloc_pct=40.0), lot=65, step=50.0)  # 2+ lots needed to book half
    trail = backtest(df, SwingConfig(trail_atr=3.0), lot=65, step=50.0)
    assert base["trades"] == deeper["trades"] and base["net"] != deeper["net"]
    assert any(t["booked_half"] for t in booked["list"]) or booked["trades"] == 0
    assert trail["trades"] >= base["trades"]


def test_crash_guard_sells_intraday_and_waits_for_fresh_trend():
    w, reg, k = make_swing(trend=+10.0, crash_guard_atr=2.0)
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    p = reg[0]
    k.spot = 24150.0
    w.on_clock(datetime(2026, 10, 29, 10, 0))  # small dip: daily range in the fake data is 40 pts -> level = 24180 - 2*ATR
    assert p.is_open and w.watching and w.watching["level"] < 24180
    w._watched = 0.0
    k.spot = 23000.0  # crash
    w.on_clock(datetime(2026, 10, 29, 10, 1))
    assert p.state == "POSITION_CLOSED" and p.exit_reason == "crash_guard" and w.wait_cross
    off, reg2, k2 = make_swing(trend=+10.0, crash_guard_atr=0.0)
    off.enabled = True
    off.on_clock(datetime(2026, 10, 28, 15, 16))
    k2.spot = 20000.0
    off.on_clock(datetime(2026, 10, 29, 10, 0))
    assert reg2[0].is_open


def test_backtest_crash_guard_exits_on_big_down_day():
    idx = pd.bdate_range("2020-01-01", periods=200)
    px = np.linspace(10000, 12000, 200)
    lo = px - 30
    lo[150] = px[149] - 2000  # one crash day intraday, closes back up
    df = pd.DataFrame({"close": px, "high": px + 30, "low": lo}, index=idx)
    g = backtest(df, SwingConfig(crash_guard_atr=2.0), lot=65, step=50.0)
    assert any(t.get("crash_guard") for t in g["list"])
    n = backtest(df, SwingConfig(crash_guard_atr=0.0), lot=65, step=50.0)
    assert not any(t.get("crash_guard") for t in n["list"])


def test_loss_cap_off_keeps_emergency_stop_and_cap_trims_lots():
    w, reg, _ = make_swing(trend=+10.0, max_loss_pct=0)
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    p = reg[0]
    assert p.stop == pytest.approx(p.entry_price * 0.4, rel=0.02)  # 60% emergency stop
    w2, reg2, _ = make_swing(trend=+10.0, alloc_pct=40.0, capital=200000.0)  # money allows 4 lots, the 2% loss cap allows 2
    w2.enabled = True
    w2.on_clock(datetime(2026, 10, 28, 15, 16))
    q = reg2[0]
    assert q.spec.lots == 2 and (q.entry_price - q.stop) * q.spec.qty <= 200000 * 0.02 * 1.03


def test_red_day_does_not_start_a_new_buy():
    w, reg, _ = make_swing(trend=+10.0, spot=24000.0)  # yesterday 24190 → today 24000: trend still UP, day −0.8%
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    assert reg == [] and w.signal["trend"] == "UP" and w.signal["day_change_pct"] < 0
    assert "red day" in w.timeline[-1]["msg"]
    off, reg2, _ = make_swing(trend=+10.0, spot=24000.0, red_day_wait=False)
    off.enabled = True
    off.on_clock(datetime(2026, 10, 28, 15, 16))
    assert len(reg2) == 1


def test_check_now_before_decision_time_only_previews():
    w, reg, _ = make_swing(trend=+10.0)
    w.enabled = True
    r = w.check(datetime(2026, 10, 28, 11, 14), trade=w.in_window(datetime(2026, 10, 28, 11, 14)))
    assert r["preview"] and r["action"] == "would buy" and reg == [] and w.last_check is None
    w.on_clock(datetime(2026, 10, 28, 15, 16))  # the real decision still happens at 15:15
    assert len(reg) == 1


def test_swing_buy_carries_its_reasons():
    w, reg, _ = make_swing(trend=+10.0)
    w.enabled = True
    w.on_clock(datetime(2026, 10, 28, 15, 16))
    why = reg[0].stats["why"]
    assert all(isinstance(x, dict) and {"group", "ok", "label"} <= set(x) for x in why)
    text = " ".join(x["label"] for x in why)
    assert "10-day avg" in text and "today" in text and "monthly expiry" in text and "Stop" in text
