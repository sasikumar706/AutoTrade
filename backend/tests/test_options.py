"""Index-options features: the 20 test areas from the spec (section 14), with a fake Kite."""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("DATA_DIR", os.path.join(tempfile.gettempdir(), "kitealgo-test-data"))

from app import market_calendar as mc  # noqa: E402
from app.autotrade import AutoConfig, AutoTrader, Deps  # noqa: E402
from app.options import (bs_price, classify, estimated_expiry, expiries, pick_strike, pivots, quantity,  # noqa: E402
                         select_contract)
from app.options_backtest import OptionsBacktestConfig, RealPremiums, run_options_backtest  # noqa: E402
from app.positions import ManagedPosition, PositionSpec  # noqa: E402
from app.risk import RiskConfig, RiskError, compute_levels, trail_stop, validate_position  # noqa: E402
from app.store import Store  # noqa: E402
from tests.test_engine import intraday  # noqa: E402

DAY = date(2026, 10, 28)  # Wednesday, a trading day
EXP1, EXP2 = "2026-11-03", "2026-11-10"


def make_instruments(nifty_lot=65):
    rows = [
        dict(instrument_token=256265, exchange="NSE", tradingsymbol="NIFTY 50", name="NIFTY 50", instrument_type="EQ", segment="INDICES", expiry="", strike=0, lot_size=0, tick_size=0.05),
        dict(instrument_token=260105, exchange="NSE", tradingsymbol="NIFTY BANK", name="NIFTY BANK", instrument_type="EQ", segment="INDICES", expiry="", strike=0, lot_size=0, tick_size=0.05),
        dict(instrument_token=265, exchange="BSE", tradingsymbol="SENSEX", name="SENSEX", instrument_type="EQ", segment="INDICES", expiry="", strike=0, lot_size=0, tick_size=0.05),
        dict(instrument_token=9001, exchange="NFO", tradingsymbol="NIFTY26NOVFUT", name="NIFTY", instrument_type="FUT", segment="NFO-FUT", expiry="2026-11-24", strike=0, lot_size=nifty_lot, tick_size=0.1),
    ]
    tok = 10_000
    for exp in (EXP1, EXP2):
        for k in range(23000, 25001, 50):
            for ot in ("CE", "PE"):
                tok += 1
                rows.append(dict(instrument_token=tok, exchange="NFO", tradingsymbol=f"NIFTY{exp[2:4]}{exp[5:7]}{exp[8:]}{k}{ot}", name="NIFTY",
                                 instrument_type=ot, segment="NFO-OPT", expiry=exp, strike=float(k), lot_size=nifty_lot, tick_size=0.05))
    for k in range(50000, 54001, 100):
        for ot in ("CE", "PE"):
            tok += 1
            rows.append(dict(instrument_token=tok, exchange="NFO", tradingsymbol=f"BANKNIFTY26NOV{k}{ot}", name="BANKNIFTY", instrument_type=ot,
                             segment="NFO-OPT", expiry="2026-11-24", strike=float(k), lot_size=30, tick_size=0.05))
    for k in range(78000, 82001, 100):
        for ot in ("CE", "PE"):
            tok += 1
            rows.append(dict(instrument_token=tok, exchange="BFO", tradingsymbol=f"SENSEX2610{29}{k}{ot}", name="SENSEX", instrument_type=ot,
                             segment="BFO-OPT", expiry="2026-10-29", strike=float(k), lot_size=20, tick_size=0.05))
    return pd.DataFrame(rows)


INST = make_instruments()


class FakeKite:
    def __init__(self, premium=100.0, spread=0.5):
        self.premium, self.spread = premium, spread
        self.orders_placed, self.modified, self.cancelled = [], [], []
        self.book: dict[str, dict] = {}
        self.fail_next: Exception | None = None

    def quote(self, keys):
        p = self.premium
        return {k: {"last_price": p, "ohlc": {"close": p * 0.9}, "depth": {"buy": [{"price": p - self.spread / 2}], "sell": [{"price": p + self.spread / 2}]}} for k in keys}

    def ltp(self, keys):
        return {k: {"last_price": 24010.0} for k in keys}

    def place_order(self, **kw):
        if self.fail_next:
            e, self.fail_next = self.fail_next, None
            self.orders_placed.append(kw)  # the request may have reached Kite
            oid = f"O{len(self.orders_placed)}"
            self.book[oid] = {"order_id": oid, "status": "OPEN", **kw}
            raise e
        self.orders_placed.append(kw)
        oid = f"O{len(self.orders_placed)}"
        self.book[oid] = {"order_id": oid, "status": "OPEN", **kw}
        return oid

    def modify_order(self, **kw):
        self.modified.append(kw)

    def cancel_order(self, **kw):
        self.cancelled.append(kw)
        self.book[kw["order_id"]]["status"] = "CANCELLED"

    def orders(self):
        return list(self.book.values())

    def margins(self, seg=None):
        return {"net": 200000.0}

    def order_margins(self, orders):
        return [{"total": 7000.0}]


class TimeoutError_(Exception):
    pass


TimeoutError_.__name__ = "NetworkException"


def spec(side=1, lots=1, lot=65, sym="NIFTY26NOV0324000CE"):
    return PositionSpec(exchange="NFO", tradingsymbol=sym, instrument_token=111, lots=lots, lot_size=lot, side=side,
                        instrument_type="CE", strike=24000, expiry=EXP1)


RISK = RiskConfig(sl_mode="pct", sl_value=20, target_mode="rr", target_value=2.1)


# ------------------------------------------------------------------ 1. previous-day data
def _prior_days(n_days=3, start=24000.0):
    rows, p = [], start
    days, d = [], DAY
    while len(days) < n_days:
        d -= timedelta(days=1)
        if mc.is_trading_day(d):
            days.insert(0, d)
    for d in days:
        for k in range(75):
            t = datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=15 + 5 * k)
            o, c = p, p + np.sin(k / 6) * 5
            rows.append(dict(date=t, open=o, high=max(o, c) + 3, low=min(o, c) - 3, close=c, volume=0))
            p = c
    return rows


def make_auto(kite, **over):
    hist = _prior_days()
    deps = Deps(instruments=lambda: INST, quote=kite.quote, history=lambda tok, iv, a, b: hist,
                kite_getter=lambda: kite, margin_available=lambda: 200000.0, order_margin=lambda o: 7000.0, data_dir=None)
    cfg = AutoConfig(**{"strategy": "sma_cross", "params": {"fast": 2, "slow": 3}, "min_confidence": 0,
                        "max_open_gap_pct": 5.0, "trend_override_min": 0, "consensus_min": 0, "any_strategy_entry": False,
                        "max_vix": 0, "min_adx": 0, "skip_lunch": False, "time_stop_bars": 0, "midday_repick": "",
                        "strict_proof": False, "day_builder": "off", "cooldown_after_loss_bars": 0, "quick_target_pts": 0, "iv_mode": "off", "min_move_cost_ratio": 0, "same_direction_mode": "off",
                        "oi_walls": False, "stretch_mode": "off", "max_stop_pts": 0, "max_target_pts": 0, **over})
    registered = []
    a = AutoTrader(cfg, deps, registered.append)
    return a, registered, hist


def test_01_previous_day_levels_and_pivots():
    a, _, hist = make_auto(FakeKite())
    a.start_day(datetime.combine(DAY, datetime.min.time()) + timedelta(hours=9))
    prev = [r for r in hist if r["date"].date() == max(r["date"].date() for r in hist)]
    h, l, c = max(r["high"] for r in prev), min(r["low"] for r in prev), prev[-1]["close"]
    assert a.levels["prev_high"] == pytest.approx(h) and a.levels["prev_low"] == pytest.approx(l)
    assert a.levels["P"] == pivots(h, l, c)["P"]
    assert pivots(110, 90, 100) == {"P": 100.0, "R1": 110.0, "S1": 90.0, "R2": 120.0, "S2": 80.0}


# ------------------------------------------------------------------ 2. live market data updates
def test_02_live_ticks_build_candles_and_update_prices():
    a, _, _ = make_auto(FakeKite())
    a.start_day(datetime(2026, 10, 28, 9, 0))
    n0 = len(a.history)
    t = datetime(2026, 10, 28, 9, 15, 1)
    for i in range(12):
        a.on_index_tick(t + timedelta(minutes=i), 24000 + i)
    assert a.last_spot == 24011 and len(a.history) == n0 + 2  # two 5-min candles closed
    p = ManagedPosition(spec(), RISK, "paper")
    p.open(100.0)
    p.on_tick(104.0)
    assert p.ltp == 104.0 and p.unrealized() > 0


# ------------------------------------------------------------------ 3/4/5 contracts, strikes, expiries, lots
def test_03_ce_pe_selection_atm_itm_otm():
    spot = 24020
    ce_atm = select_contract(INST, "NIFTY", EXP1, "CE", spot, "ATM", 0)
    assert ce_atm.strike == 24000 and ce_atm.moneyness == "ATM"
    assert select_contract(INST, "NIFTY", EXP1, "CE", spot, "ITM", 2).strike == 23900
    assert select_contract(INST, "NIFTY", EXP1, "CE", spot, "OTM", 2).strike == 24100
    assert select_contract(INST, "NIFTY", EXP1, "PE", spot, "ITM", 1).strike == 24050
    assert select_contract(INST, "NIFTY", EXP1, "PE", spot, "OTM", 1).strike == 23950
    assert classify(24100, spot, "CE", 50) == "OTM" and classify(24100, spot, "PE", 50) == "ITM"
    assert select_contract(INST, "BANKNIFTY", "2026-11-24", "PE", 52040, "ATM", 0).lot_size == 30
    assert select_contract(INST, "SENSEX", "2026-10-29", "CE", 80010, "ATM", 0).exchange == "BFO"


def test_04_strike_and_expiry_changes():
    a = select_contract(INST, "NIFTY", EXP1, "CE", 24000, "ATM", 0)
    b = select_contract(INST, "NIFTY", EXP2, "CE", 24000, "ATM", 0)
    c = select_contract(INST, "NIFTY", EXP1, "CE", 24000, "24350", 0)
    assert a.tradingsymbol != b.tradingsymbol and b.expiry == EXP2 and c.strike == 24350
    with pytest.raises(ValueError):
        select_contract(INST, "NIFTY", EXP1, "CE", 24000, "24333", 0)  # not listed
    with pytest.raises(ValueError):
        select_contract(INST, "NIFTY", "2026-12-01", "CE", 24000, "ATM", 0)  # expiry not listed
    assert expiries(INST, "NIFTY", DAY) == [EXP1, EXP2]


def test_05_lot_size_from_instrument_list_and_quantity():
    inst75 = make_instruments(nifty_lot=75)
    assert select_contract(inst75, "NIFTY", EXP1, "CE", 24000).lot_size == 75
    assert select_contract(INST, "NIFTY", EXP1, "CE", 24000).lot_size == 65
    assert quantity(3, 65) == 195
    with pytest.raises(ValueError):
        quantity(0, 65)
    with pytest.raises(ValueError):
        quantity(1, 0)


# ------------------------------------------------------------------ 6/7/8 SL, target, R:R
def test_06_stop_loss_mandatory_and_validated():
    for bad in (RiskConfig(sl_value=0), RiskConfig(sl_mode="price", sl_value=120), RiskConfig(sl_mode="pct", sl_value=0.5),
                RiskConfig(sl_mode="pct", sl_value=90), RiskConfig(sl_mode="bogus")):
        with pytest.raises(RiskError):
            compute_levels(bad, 100.0, 1)
    lv = compute_levels(RiskConfig(sl_mode="points", sl_value=15, max_risk_per_trade=500), 100.0, 1)
    assert lv.stop == 85.0
    assert validate_position(RiskConfig(max_risk_per_trade=500), lv, 65)  # 15*65=975 > 500
    p = ManagedPosition(spec(), RiskConfig(sl_value=0), "paper")
    with pytest.raises(RiskError):
        p.open(100.0)  # entry rejected without a stop
    assert p.state == "NEW"


def test_07_target_modes_long_and_short():
    assert compute_levels(RiskConfig(sl_value=20, target_mode="rr", target_value=2.1), 100.0, 1).target == 142.0
    assert compute_levels(RiskConfig(sl_value=20, target_mode="pct", target_value=50), 100.0, 1).target == 150.0
    assert compute_levels(RiskConfig(sl_value=20, target_mode="points", target_value=30), 100.0, 1).target == 130.0
    assert compute_levels(RiskConfig(sl_value=20, target_mode="price", target_value=125), 100.0, 1).target == 125.0
    short = compute_levels(RiskConfig(sl_value=20, target_mode="rr", target_value=2.0), 100.0, -1)
    assert short.stop == 120.0 and short.target == 60.0
    with pytest.raises(RiskError):
        compute_levels(RiskConfig(sl_value=20, target_mode="price", target_value=90), 100.0, 1)


def test_08_risk_reward_is_ratio_not_win_rate():
    lv = compute_levels(RiskConfig(sl_value=10, target_mode="rr", target_value=2.1), 200.0, 1)
    assert lv.rr == 2.1 and lv.risk_per_unit == 20.0 and lv.reward_per_unit == pytest.approx(42.0)
    assert trail_stop(RiskConfig(trail_mode="points", trail_value=10, trail_activate_rr=1), 1, 200, 180, 20, 230) == 220
    assert trail_stop(RiskConfig(trail_mode="points", trail_value=10, trail_activate_rr=1), 1, 200, 180, 20, 210) == 180  # not active yet
    assert trail_stop(RiskConfig(trail_mode="points", trail_value=50, trail_activate_rr=0), 1, 200, 190, 20, 230) == 190  # never loosens


# ------------------------------------------------------------------ 9. backtest BUY / SELL
def _idx_rows(days=25, seed=3):
    return intraday(days, seed=seed, start_price=24000, vol=0.0012)


def test_09_backtest_buy_and_sell_sides():
    rows = _idx_rows()
    base = dict(strategy="sma_cross", params={"fast": 3, "slow": 8}, lot_size=65, max_trades_per_day=3)
    buy = run_options_backtest(rows, OptionsBacktestConfig(side="BUY", **base))
    assert buy["trades"] and all(t["side"] == "LONG" and t["action"].startswith("BUY") for t in buy["trades"])
    with pytest.raises(PermissionError):
        run_options_backtest(rows, OptionsBacktestConfig(side="SELL", **base))
    sell = run_options_backtest(rows, OptionsBacktestConfig(side="SELL", allow_option_selling=True, **base))
    assert sell["trades"] and all(t["side"] == "SHORT" for t in sell["trades"])
    for res in (buy, sell):
        for t in res["trades"]:
            assert t["entry_time"][:10] == t["exit_time"][:10]  # never overnight
            assert "09:30" <= t["entry_time"][11:16] < "10:30"  # entry window
            assert t["exit_time"][11:16] <= "10:30" or t["exit_reason"] in ("end_of_day",)
            assert t["stop_loss"] is not None  # mandatory SL
            assert t["qty"] % 65 == 0
        days = pd.Series([t["entry_time"][:10] for t in res["trades"]]).value_counts()
        assert days.max() <= 3
    # bullish signal -> CE when buying, PE when selling
    assert {t["option_type"] for t in buy["trades"]} <= {"CE", "PE"}
    m = buy["metrics"]
    assert m["winning_trades"] + m["losing_trades"] == m["trades"]
    assert m["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in buy["trades"]), abs=0.1)


def test_09b_no_accidental_reversal_and_rule_filter():
    rows = _idx_rows(seed=9)
    base = dict(strategy="sma_cross", params={"fast": 2, "slow": 5}, lot_size=65, max_trades_per_day=10, entry_end="15:00", exit_time="15:05")
    r = run_options_backtest(rows, OptionsBacktestConfig(**base))
    for a, b in zip(r["trades"], r["trades"][1:]):
        assert b["entry_time"] >= a["exit_time"]  # one position at a time
    ce_only = run_options_backtest(rows, OptionsBacktestConfig(option_rule="CE", **base))
    assert ce_only["trades"] and all(t["option_type"] == "CE" for t in ce_only["trades"])


def test_09c_real_premium_mode_uses_contract_candles():
    rows = _idx_rows(days=5)
    ch = INST[(INST["name"] == "NIFTY") & (INST["expiry"] == EXP1)][["strike", "instrument_type", "instrument_token", "tradingsymbol", "lot_size"]].to_dict("records")
    fetched = []

    def fetch(tok):
        fetched.append(tok)
        return [dict(date=r["date"], open=100.0, high=104.0, low=97.0, close=101.0) for r in rows]

    from app.backtest import prepare_candles
    cfg = OptionsBacktestConfig(strategy="sma_cross", params={"fast": 2, "slow": 5}, premium_source="real", expiry=EXP1, lot_size=65,
                                risk={"sl_mode": "pct", "sl_value": 5, "target_mode": "rr", "target_value": 2.1})
    r = run_options_backtest(rows, cfg, RealPremiums(cfg, prepare_candles(rows), ch, fetch))
    assert r["trades"] and fetched and all(not t["estimated"] for t in r["trades"])
    assert all(t["contract"].startswith("NIFTY") for t in r["trades"])
    # entry at the option candle open (100) + slippage; a 3% dip never hits a 5% stop
    assert all(99 < t["entry_price"] < 101 for t in r["trades"])


def test_09d_estimated_premiums_sane():
    ce_lo, ce_hi = bs_price(24000, 24000, 7 / 365, 12, "CE"), bs_price(24100, 24000, 7 / 365, 12, "CE")
    pe = bs_price(24000, 24000, 7 / 365, 12, "PE")
    assert ce_hi > ce_lo > 0 and abs(ce_lo - pe) < ce_lo * 0.2  # ~put-call parity at ATM
    assert bs_price(24000, 23900, 0, 12, "CE") == 100.0  # expiry: intrinsic
    assert estimated_expiry("NIFTY", date(2026, 10, 28)) == date(2026, 11, 3)  # next Tuesday
    assert estimated_expiry("SENSEX", date(2026, 10, 28)) == date(2026, 10, 29)  # Thursday
    assert estimated_expiry("BANKNIFTY", date(2026, 10, 28)) == date(2026, 11, 24)  # last Tuesday of Nov


# ------------------------------------------------------------------ 10. paper entry/exit
def test_10_paper_entry_target_and_stop():
    p = ManagedPosition(spec(), RISK, "paper", slippage_bps=0)
    p.open(100.0)
    assert p.state == "POSITION_OPEN" and p.stop == 80.0 and p.target == 142.0
    p.on_tick(150.0)
    assert p.state == "POSITION_CLOSED" and p.exit_reason == "target" and p.net > 0
    q = ManagedPosition(spec(), RISK, "paper", slippage_bps=0)
    q.open(100.0)
    q.on_tick(79.0)
    assert q.state == "POSITION_CLOSED" and q.exit_reason == "stop_loss" and q.exit_price == 79.0 and q.net < 0


# ------------------------------------------------------------------ 11. duplicates
def test_11_duplicate_prevention():
    p = ManagedPosition(spec(), RISK, "paper")
    p.open(100.0)
    with pytest.raises(RuntimeError):
        p.open(100.0)
    p.request_exit("manual")
    net = p.net
    p.request_exit("manual")  # second exit ignored
    assert p.net == net
    kite = FakeKite()
    a, reg, _ = make_auto(kite)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    candle = {"date": datetime(2026, 10, 28, 9, 35)}
    a.execute_signal(1, candle, datetime(2026, 10, 28, 9, 40, 1))
    a.execute_signal(1, candle, datetime(2026, 10, 28, 9, 40, 2))
    assert len(reg) == 1 and a.trades_today == 1


# ------------------------------------------------------------------ 12/13 live fills, partials, rejections, SL/target
def test_12_rejected_and_partial_fills():
    k = FakeKite()
    p = ManagedPosition(spec(lots=2), RISK, "live", lambda: k)
    p.open(100.0)
    assert p.state == "ENTRY_PENDING" and k.orders_placed[0]["market_protection"] == -1
    p.on_order_update({"order_id": p.entry_order_id, "status": "REJECTED", "status_message": "Insufficient funds"})
    assert p.state == "ORDER_REJECTED" and p.filled_qty == 0
    k2 = FakeKite()
    q = ManagedPosition(spec(lots=2), RISK, "live", lambda: k2)
    q.open(100.0)
    q.on_order_update({"order_id": q.entry_order_id, "status": "CANCELLED", "filled_quantity": 65, "average_price": 101.0})
    assert q.state == "PARTIALLY_FILLED" and q.filled_qty == 65
    sl = k2.orders_placed[-1]
    assert sl["order_type"] == "SL" and sl["quantity"] == 65 and sl["transaction_type"] == "SELL"


def test_13_live_stop_and_target_flow():
    k = FakeKite()
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.open(100.0)
    p.on_order_update({"order_id": p.entry_order_id, "status": "COMPLETE", "average_price": 100.0, "filled_quantity": 65})
    sl = k.orders_placed[-1]
    assert p.state == "POSITION_OPEN" and sl["order_type"] == "SL" and sl["trigger_price"] == 80.0 and sl["price"] < 80.0
    # target: cancel SL first, then MARKET exit with market protection
    p.on_tick(143.0)
    assert k.cancelled and k.cancelled[-1]["order_id"] == p.sl_order_id
    ex = k.orders_placed[-1]
    assert ex["order_type"] == "MARKET" and ex["transaction_type"] == "SELL" and ex["market_protection"] == -1
    assert p.state == "EXIT_PENDING"  # not assumed filled
    p.on_order_update({"order_id": p.exit_order_id, "status": "COMPLETE", "average_price": 142.5})
    assert p.state == "POSITION_CLOSED" and p.exit_reason == "target"
    # broker-side stop fill
    k2 = FakeKite()
    q = ManagedPosition(spec(), RISK, "live", lambda: k2)
    q.open(100.0)
    q.on_order_update({"order_id": q.entry_order_id, "status": "COMPLETE", "average_price": 100.0})
    q.on_order_update({"order_id": q.sl_order_id, "status": "COMPLETE", "average_price": 79.5})
    assert q.state == "POSITION_CLOSED" and q.exit_reason == "stop_loss" and q.exit_price == 79.5
    # trailing stop modifies the broker SL order
    k3 = FakeKite()
    r = ManagedPosition(spec(), RiskConfig(sl_value=20, target_mode="none", trail_mode="pct", trail_value=10, trail_activate_rr=0.5), "live", lambda: k3)
    r.open(100.0)
    r.on_order_update({"order_id": r.entry_order_id, "status": "COMPLETE", "average_price": 100.0})
    r.on_tick(130.0)
    assert k3.modified and k3.modified[-1]["trigger_price"] == 117.0 and r.stop == 117.0


def test_13b_stop_breached_without_fill_triggers_recovery():
    k = FakeKite()
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.open(100.0)
    p.on_order_update({"order_id": p.entry_order_id, "status": "COMPLETE", "average_price": 100.0})
    n = len(k.orders_placed)
    p.on_tick(75.0, now=1000.0)
    assert len(k.orders_placed) == n  # give the broker SL order a moment
    p.on_tick(74.0, now=1010.0)
    assert k.orders_placed[-1]["order_type"] == "MARKET" and p.stats["stop_recoveries"] == 1


# ------------------------------------------------------------------ 14/15/16 time windows
def _feed_to_signal(a, start=datetime(2026, 10, 28, 9, 15, 1), minutes=40, jump_at=None):
    t = start
    px = 24000.0
    for i in range(minutes * 2):
        ts = t + timedelta(seconds=30 * i)
        if jump_at and ts >= jump_at:
            px += 6
        a.on_index_tick(ts, px)
        a.on_clock(ts)


def test_14_no_orders_during_analysis_window():
    k = FakeKite()
    a, reg, _ = make_auto(k)
    a.enable(datetime(2026, 10, 28, 9, 10))
    _feed_to_signal(a, minutes=14, jump_at=datetime(2026, 10, 28, 9, 16))
    assert a.state in ("ANALYZING",) and not reg
    assert any("ignored (analysis phase)" in e["msg"] for e in a.timeline)


def test_15_entries_only_between_0930_and_1030():
    k = FakeKite()
    a, reg, _ = make_auto(k)
    a.enable(datetime(2026, 10, 28, 9, 10))
    _feed_to_signal(a, start=datetime(2026, 10, 28, 10, 30, 1), minutes=30, jump_at=datetime(2026, 10, 28, 10, 40))
    assert not reg
    b, reg2, _ = make_auto(FakeKite())
    b.enable(datetime(2026, 10, 28, 9, 10))
    _feed_to_signal(b, start=datetime(2026, 10, 28, 9, 15, 1), minutes=40, jump_at=datetime(2026, 10, 28, 9, 36))
    assert len(reg2) == 1
    pos = reg2[0]
    assert pos.spec.instrument_type == "CE" and pos.spec.expiry == EXP1 and pos.spec.qty == 65
    assert b.signals[-1]["risk_check"] == "PASS" and b.signals[-1]["action"] == "BUY_CE"
    assert b.signals[-1]["stop_loss"] < b.signals[-1]["entry_price"] < b.signals[-1]["target_price"]


def test_16_mandatory_cutoff_exit_even_when_paused():
    b, reg, _ = make_auto(FakeKite())
    b.enable(datetime(2026, 10, 28, 9, 10))
    _feed_to_signal(b, minutes=40, jump_at=datetime(2026, 10, 28, 9, 36))
    pos = reg[0]
    assert pos.is_open
    b.pause()
    pos.on_tick(pos.entry_price * 1.05)
    b.on_clock(datetime(2026, 10, 28, 10, 30, 0))
    assert pos.state == "POSITION_CLOSED" and pos.exit_reason == "time_cutoff"
    b.on_clock(datetime(2026, 10, 28, 10, 30, 5))
    assert b.state == "SESSION_COMPLETED"


# ------------------------------------------------------------------ 17. network failures
def test_17_uncertain_submission_reconciles_instead_of_resending():
    k = FakeKite()
    k.fail_next = TimeoutError_("Gateway timed out")
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.open(100.0)
    assert len(k.orders_placed) == 1  # NOT re-sent
    assert p.entry_order_id == "O1" and p.state in ("ENTRY_PENDING", "RECONCILIATION_REQUIRED")
    k.book["O1"].update(status="COMPLETE", average_price=100.5, filled_quantity=65, tag=p.spec.tag)
    p.reconcile()
    assert p.state == "POSITION_OPEN" and p.entry_price == 100.5
    # exit refused -> EXIT_FAILED -> retried later
    k.fail_next = RuntimeError("RMS: blocked")
    p.request_exit("manual")
    assert p.state == "EXIT_FAILED"
    p.on_tick(101.0, now=p.next_retry_at + 1)
    assert p.state == "EXIT_PENDING" and k.orders_placed[-1]["order_type"] == "MARKET"


# ------------------------------------------------------------------ 18. restart with open position
def test_18_restart_restores_open_positions(tmp_path):
    from app.options_manager import OptionsManager

    class KS:
        def __init__(self, k):
            self.kite = k

        def instruments(self):
            return INST

        def historical(self, *a, **kw):
            return []

    class Hub:
        def refresh_listener(self, l):
            pass

    store = Store(tmp_path / "s.db")
    k = FakeKite()
    om = OptionsManager(KS(k), store, Hub())
    p = om.manual_open("paper", "NIFTY", EXP1, "CE", "ATM", 0, 1, "BUY", "MIS", RISK.to_dict())
    assert p.is_open
    with pytest.raises(PermissionError):  # same contract again -> blocked
        om.manual_open("paper", "NIFTY", EXP1, "CE", "ATM", 0, 1, "BUY", "MIS", RISK.to_dict())
    lp = ManagedPosition(spec(sym="NIFTY26NOV0324100CE"), RISK, "live", lambda: k)
    om._register(lp)
    lp.open(100.0)
    om2 = OptionsManager(KS(k), store, Hub())  # "restart"
    assert om2.positions[p.id].is_open and om2.positions[p.id].stop == p.stop
    k.book[lp.entry_order_id].update(status="COMPLETE", average_price=100.0, filled_quantity=65, tag=lp.spec.tag, tradingsymbol=lp.spec.tradingsymbol)
    out = om2.reconcile_all()
    assert om2.positions[lp.id].state == "POSITION_OPEN" and out[lp.id] == "open"


# ------------------------------------------------------------------ 19. daily loss limit
def test_19_daily_loss_limit_blocks_new_entries():
    b, reg, _ = make_auto(FakeKite(), max_daily_loss=700, max_trades_per_day=5,
                          risk={"sl_mode": "pct", "sl_value": 10, "target_mode": "rr", "target_value": 2.1})
    b.enable(datetime(2026, 10, 28, 9, 10))
    _feed_to_signal(b, minutes=40, jump_at=datetime(2026, 10, 28, 9, 36))
    pos = reg[0]  # max loss ~ 10% x ~100 x 65 = ~650, inside the 700 budget
    pos.on_tick(pos.stop - 1)  # stop hit with slippage -> loss > 700
    b.on_clock(datetime(2026, 10, 28, 9, 50))
    assert b.day_blocked and "Daily loss" in b.day_blocked
    sig, ok, _ = b.build_signal(1, None, datetime(2026, 10, 28, 9, 55))
    assert ok is None and sig["risk_check"] == "FAIL"
    # risk budget also caps a single trade's max loss
    c, _, _ = make_auto(FakeKite(premium=300.0), max_daily_loss=1000)
    c.start_day(datetime(2026, 10, 28, 9, 0))
    c.last_spot = 24000.0
    sig2, ok2, _ = c.build_signal(1, None, datetime(2026, 10, 28, 9, 40))
    assert ok2 is None and any("risk budget" in x for x in sig2["risk_problems"])


# ------------------------------------------------------------------ 20. holidays / sessions
def test_20_holidays_weekends_and_extra_closures(tmp_path):
    assert not mc.is_trading_day(date(2026, 10, 2))  # Gandhi Jayanti
    assert not mc.is_trading_day(date(2026, 10, 31))  # Saturday
    assert mc.is_trading_day(date(2026, 10, 28))
    (tmp_path / "holidays.txt").write_text("2026-10-28\n")
    assert not mc.is_trading_day(date(2026, 10, 28), tmp_path)
    assert mc.phase(datetime(2026, 10, 2, 9, 45), "09:15", "09:30", "10:30", "10:30") == "CLOSED_DAY"
    assert mc.phase(datetime(2026, 10, 28, 9, 20), "09:15", "09:30", "10:30", "10:30") == "ANALYSIS"
    assert mc.phase(datetime(2026, 10, 28, 10, 0), "09:15", "09:30", "10:30", "10:30") == "ENTRY"
    assert mc.phase(datetime(2026, 10, 28, 10, 31), "09:15", "09:30", "10:30", "10:30") == "AFTER_CUTOFF"
    a, reg, _ = make_auto(FakeKite())
    a.enable(datetime(2026, 10, 2, 9, 10))
    a.on_clock(datetime(2026, 10, 2, 9, 45))
    assert a.state == "IDLE" and "closed" in a.message.lower() and not reg


def test_signal_json_shape_and_fail_safe():
    a, _, _ = make_auto(FakeKite())
    a.start_day(datetime(2026, 10, 28, 9, 0))
    sig, pos, _ = a.build_signal(1, None, datetime(2026, 10, 28, 9, 40))  # no live spot yet
    assert pos is None and sig["risk_check"] == "FAIL" and "No live index price" in sig["risk_problems"]
    keys = {"instrument", "market_direction", "action", "option_type", "expiry", "strike", "entry_price", "stop_loss",
            "target_price", "lots", "quantity", "risk_reward_ratio", "confidence", "reason", "valid_until", "risk_check"}
    assert keys <= set(sig)

    class Broken(FakeKite):
        def quote(self, keys):
            raise ConnectionError("feed down")

    b, _, _ = make_auto(Broken())
    b.start_day(datetime(2026, 10, 28, 9, 0))
    b.last_spot = 24000.0
    sig2, pos2, _ = b.build_signal(-1, None, datetime(2026, 10, 28, 9, 40))
    assert pos2 is None and sig2["risk_check"] == "FAIL"  # no order on data failure


def test_cutoff_cancels_pending_live_entry_once():
    k = FakeKite()
    b, reg, _ = make_auto(k, mode="paper")
    b.enable(datetime(2026, 10, 28, 9, 10))
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.open(100.0)  # entry sent, never filled
    b.position = p
    for sec in range(0, 30, 3):
        b.on_clock(datetime(2026, 10, 28, 10, 31, sec))
    assert len(k.cancelled) == 1


# ------------------------------------------------------------------ trading window, AI pick, why-list
def test_window_presets_and_validation():
    AutoConfig(entry_start="09:30", entry_end="14:45", exit_time="15:10").validate()  # full day
    for bad in ({"exit_time": "15:30", "entry_end": "15:00"}, {"entry_start": "9.30"}, {"entry_start": "11:00", "entry_end": "10:00"},
                {"analysis_start": "09:00"}):
        with pytest.raises(ValueError):
            AutoConfig(**bad).validate()
    with pytest.raises(ValueError):
        AutoConfig(strategy_source="ai", interval="60minute").validate()


def _fake_finder(robust):
    row = {"strategy": "sma_cross", "name": "SMA crossover", "params": {"fast": 2, "slow": 3}, "robust": robust,
           "test": {"trades": 9, "net_pnl": 1400.0 if robust else -300.0, "total_return_pct": 1.4, "win_rate_pct": 56.0}, "train": {"trades": 20, "total_return_pct": 2.0}}
    return lambda rows, base, **k: {"ranking": [row], "test_period": ["a", "b"], "combinations_tested": 42}


def test_ai_pick_sets_todays_strategy(monkeypatch):
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", _fake_finder(True))
    a, _, _ = make_auto(FakeKite(), strategy_source="ai", strategy="morning_momentum", params={})
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert a.pick["strategy"] == "sma_cross" and a.active_strategy == ("sma_cross", {"fast": 2, "slow": 3})
    assert a.day_blocked is None and any("AI picked" in e["msg"] for e in a.timeline)
    a.last_spot = 24000.0
    sig, _, _ = a.build_signal(1, None, datetime(2026, 10, 28, 9, 40))
    assert sig["strategy"] == "sma_cross" and "AI picked" in sig["why"][0]["label"]
    assert {w["group"] for w in sig["why"]} >= {"strategy", "indicators", "contract", "risk"}


def test_ai_pick_none_robust_blocks_day(monkeypatch):
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", _fake_finder(False))
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert a.active_strategy is None and "WAIT" in a.day_blocked
    monkeypatch.setattr(an, "find_strategies", _fake_finder(True))
    a.repick(datetime(2026, 10, 28, 9, 20))
    assert a.active_strategy is not None and a.day_blocked is None


def test_ai_pick_runs_real_finder():
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert a.picking is False
    assert a.pick is not None and (a.pick["strategy"] or "WAIT" in (a.day_blocked or ""))


def test_reversion_strategy_not_blocked_by_trend_confidence():
    a, _, _ = make_auto(FakeKite(), strategy="rsi_reversion", params={}, min_confidence=100)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.last_spot = 24000.0
    sig, _, _ = a.build_signal(1, None, datetime(2026, 10, 28, 9, 40))
    assert not any("Confidence" in p for p in sig["risk_problems"])
    b, _, _ = make_auto(FakeKite(), min_confidence=100)  # trend strategy still gated
    b.start_day(datetime(2026, 10, 28, 9, 0))
    b.last_spot = 24000.0
    sig2, _, _ = b.build_signal(1, None, datetime(2026, 10, 28, 9, 40))
    assert any("Confidence" in p for p in sig2["risk_problems"])


def test_settings_edit_rebuilds_candles_but_keeps_daily_counts():
    a, _, _ = make_auto(FakeKite())
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.trades_today, a.day_pnl = 1, -500.0
    a.cfg = AutoConfig(**{**a.cfg.__dict__, "interval": "3minute"})
    a.reset_for_new_settings()
    assert a.day is None and a.builder.minutes == 3
    a.start_day(datetime(2026, 10, 28, 11, 0))
    assert a.trades_today == 1 and a.day_pnl == -500.0
    a.start_day(datetime(2026, 10, 29, 9, 0))
    assert a.trades_today == 0


def test_unproven_pick_paper_only(monkeypatch):
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", _fake_finder(False))
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert a.pick["tested"][0]["why"] == "lost money on unseen data"
    a.use_best_unproven(datetime(2026, 10, 28, 9, 20))
    assert a.active_strategy[0] == "sma_cross" and a.pick["unproven"] and a.day_blocked is None
    b, _, _ = make_auto(FakeKite(), strategy_source="ai", mode="live")
    b.run_async = False
    b.start_day(datetime(2026, 10, 28, 9, 0))
    with pytest.raises(PermissionError):
        b.use_best_unproven(datetime(2026, 10, 28, 9, 20))


def test_backtest_fixed_cost_replaces_charges():
    from app.backtest import BacktestConfig, run_backtest
    rows = _prior_days()
    r1 = run_backtest(rows, BacktestConfig(strategy="sma_cross", params={"fast": 2, "slow": 3}, sizing="fixed_qty", quantity=37, fixed_cost=150.0))
    assert r1["trades"] and all(t["charges"] == 150.0 for t in r1["trades"])


def test_live_watch_prediction_moves_with_price():
    k = FakeKite()
    a, _, _ = make_auto(k)
    a.enabled = True
    a.start_day(datetime(2026, 10, 28, 9, 0))
    t = datetime(2026, 10, 28, 9, 31, 0)
    for i in range(3):
        a.on_index_tick(t + timedelta(seconds=20 * i), 24000 + i)
    a.update_watch(t + timedelta(minutes=1), force=True)
    w = a.watch
    assert "error" not in w, w
    assert w["price"] == 24002 and w["phase"] == "ENTRY" and w["strategy"]
    assert w["lean"] in ("BULLISH", "BEARISH", "NEUTRAL") and w["checks_total"] >= 5
    assert w["candle_closes_at"] == "09:35"
    if w["expected_direction"]:
        assert w["expected_order"]["tradingsymbol"] and w["expected_order"]["risk_check"] in ("PASS", "FAIL")
        assert w["option"]["ltp"] == k.premium and w["option"]["change_pct"] is not None
    assert not k.orders_placed  # prediction never trades
    a.on_index_tick(t + timedelta(seconds=90), 24100)
    a.update_watch(t + timedelta(minutes=2), force=True)
    assert a.watch["price"] == 24100


def test_clear_signal_history_and_log():
    a, _, _ = make_auto(FakeKite())
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.signals = [{"position_id": "x"}, {"position_id": None}]
    assert a.timeline
    a.clear_signals()
    a.clear_log()
    assert a.signals == [] and a.timeline == []


def test_option_live_premium():
    k = FakeKite()
    a, _, _ = make_auto(k)
    o = a._option_live("NFO:NIFTY26N0324000CE")
    assert o["symbol"] == "NIFTY26N0324000CE" and o["ltp"] == k.premium
    assert o["change"] == round(k.premium - k.premium * 0.9, 2) and o["bid"] and o["ask"]
    a.watch = {"option": o}
    k.premium += 2
    assert a._option_live("NFO:NIFTY26N0324000CE")["tick"] == 2


def test_clear_history_keeps_open_positions(tmp_path):
    from app.options_manager import OptionsManager
    from app.store import Store
    import inspect
    assert "source" in inspect.signature(OptionsManager.clear_history).parameters
    st = Store(tmp_path / "t.db")
    st.upsert_session("s1", {"mode": "paper"}, "stopped", {})
    st.upsert_session("s2", {"mode": "live"}, "stopped", {})
    st.upsert_session("s3", {"mode": "replay"}, "running", {})
    assert st.delete_sessions(("paper", "replay"), keep={"s3"}) == 1
    assert {r["id"] for r in st.list_sessions()} == {"s2", "s3"}


# ------------------------------------------------------------------ entry tolerance (0.3%)
def _orb_day(high_after):
    t0 = datetime(2026, 10, 28, 9, 15)
    rows = []
    for k in range(6):  # 30 min of 5-min candles
        c = 24000 + (5 if k < 3 else 0)
        hi = 24010 if k < 3 else high_after
        rows.append(dict(date=t0 + timedelta(minutes=5 * k), open=24000, high=max(hi, c), low=23950, close=c if k < 3 else high_after, volume=0))
    return pd.DataFrame(rows)


def test_orb_breakout_tolerance():
    from app.strategies import get_strategy
    orb = get_strategy("orb")
    df = _orb_day(24000.0)  # closes 10 pts below the 24010 range high (0.04%)
    assert not orb.signals(df, {"range_minutes": 15})["entry_long"].any()
    assert orb.signals(df, {"range_minutes": 15, "level_tol_pct": 0.3})["entry_long"].any()
    sig = orb.signals(df, {"range_minutes": 15, "level_tol_pct": 0.3})
    assert not (sig["entry_long"] & sig["entry_short"]).any()  # capped at half the range


def test_limit_entry_band_paper_and_live():
    k = FakeKite()
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.entry_band_pts = 0.5
    p.open(74.15)
    o = k.orders_placed[-1]
    assert o["order_type"] == "LIMIT" and o["price"] == 74.65 and p.entry_limit == 74.65
    q = ManagedPosition(spec(), RISK, "paper", slippage_bps=100)  # 1% simulated slippage = ₹0.74
    q.entry_band_pts = 0.5
    q.open(74.15)
    assert q.entry_price == pytest.approx(74.65)  # never filled worse than quote + ₹0.5


def test_unfilled_limit_entry_cancelled_after_validity():
    k = FakeKite()
    a, _, _ = make_auto(k, mode="paper")
    a.start_day(datetime(2026, 10, 28, 9, 0))
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.entry_band_pts = 0.5
    p.open(100.0)
    p.created = datetime(2026, 10, 28, 9, 40).isoformat()
    a.position, a.enabled = p, True
    a.on_clock(datetime(2026, 10, 28, 9, 40, 30))
    assert not getattr(p, "cancel_sent", False)
    a.on_clock(datetime(2026, 10, 28, 9, 42))
    assert getattr(p, "cancel_sent", False)



def test_old_saved_config_with_renamed_field_still_loads():
    from dataclasses import asdict
    d = {**asdict(AutoConfig()), "entry_tolerance_pct": 0.3}
    known = {f.name for f in __import__("dataclasses").fields(AutoConfig)}
    cfg = AutoConfig(**{k: v for k, v in d.items() if k in known})
    assert cfg.entry_tolerance_pts == 1.0 and cfg.level_tolerance_pct == 0.0


def test_midday_start_reads_todays_completed_candles():
    k = FakeKite()
    a, _, hist = make_auto(k)
    day = DAY
    # today 09:15-11:05 on 5-min candles (22 completed + one forming at 11:05)
    today = []
    for i in range(23):
        t = datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=15 + 5 * i)
        today.append(dict(date=t, open=24100 + i, high=24105 + i, low=24095 + i, close=24102 + i, volume=0))
    a.deps.history = lambda tok, iv, x, y: hist + today
    now = datetime.combine(day, datetime.min.time()) + timedelta(hours=11, minutes=7)
    a.start_day(now)
    td = a.history[a.history["date"].dt.date == day]
    assert len(td) == 22 and td["date"].iloc[-1].strftime("%H:%M") == "11:00"
    assert a.builder.current is not None and a.builder.current.date.strftime("%H:%M") == "11:05"
    assert a.builder.current.open == 24122 and a.today_open == 24100
    assert a.last_spot == 24124 and any("Read today's 22" in e["msg"] for e in a.timeline)
    assert any("Back-check" in e["msg"] for e in a.timeline)


def test_enable_after_restart_reloads_candles():
    a, _, _ = make_auto(FakeKite())
    a.day = DAY  # restored state: same day but no candles in memory
    a.counts_for, a.trades_today = DAY, 1
    a.enable(datetime.combine(DAY, datetime.min.time()) + timedelta(hours=10))
    assert len(a.history) >= 60 and a.trades_today == 1


def test_watch_while_ai_choosing_shows_indicators_and_progress():
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.pick, a.picking = None, True
    a.pick_progress = {"stage": "Testing 3 of 7: Supertrend", "started": datetime(2026, 10, 28, 11, 0).isoformat()}
    a.last_spot = 24000.0
    a.update_watch(datetime(2026, 10, 28, 11, 0, 20), force=True)
    w = a.watch
    assert w["lean"] in ("BULLISH", "BEARISH", "NEUTRAL") and w["checks_total"] >= 5
    assert "Testing 3 of 7" in w["status"] and "20s" in w["status"]


def test_restart_reuses_todays_ai_pick(monkeypatch):
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", _fake_finder(True))
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    calls = []
    monkeypatch.setattr(an, "find_strategies", lambda *x, **k: calls.append(1))
    a.history = a.history.iloc[0:0]  # restart: no candles in memory
    a.enable(datetime(2026, 10, 28, 11, 0))
    assert not calls and a.active_strategy[0] == "sma_cross"


def test_lots_reduced_to_fit_limits():
    k = FakeKite()
    a, _, _ = make_auto(k, lots=5, max_capital=30000.0, max_daily_loss=100000.0)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.last_spot = 24000.0
    sig, pos, _ = a.build_signal(1, None, datetime(2026, 10, 28, 9, 40))
    per_lot = sig["entry_price"] * 65
    expect = int(30000 // per_lot)
    assert 1 <= expect < 5
    assert sig["risk_check"] == "PASS" and sig["lots"] == expect and pos.spec.lots == expect
    assert any("Lots reduced 5" in w["label"] for w in sig["why"])
    b, _, _ = make_auto(k, lots=5, max_capital=30000.0, fit_lots_to_limits=False)
    b.start_day(datetime(2026, 10, 28, 9, 0))
    b.last_spot = 24000.0
    sig2, _, _ = b.build_signal(1, None, datetime(2026, 10, 28, 9, 40))
    assert sig2["risk_check"] == "FAIL"


def test_late_join_recent_signal_once(monkeypatch):
    import app.analysis as an
    k = FakeKite()
    a, reg, _ = make_auto(k, max_daily_loss=100000.0)
    now = datetime(2026, 10, 28, 9, 50)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled = True
    since = a.history["date"].iloc[-1].to_pydatetime().replace(year=2026, month=10, day=28, hour=9, minute=35)
    row = a.history.iloc[[-1]].copy()
    row["date"] = pd.Timestamp(since)
    a.history = pd.concat([a.history, row], ignore_index=True)
    import app.indicators as ind_
    atr_now = float(ind_.atr(a.history, 14).iloc[-1])
    a.last_spot = float(row["close"].iloc[0]) + 0.3 * atr_now  # within half a candle's range -> fine to join
    monkeypatch.setattr(an, "current_signal", lambda *x, **kw: {"action": "HOLD LONG", "position": "LONG", "since": since.isoformat()})
    a.try_late_join(now)
    assert len(a.signals) == 1 and any("Late entry" in w["label"] for w in a.signals[0]["why"])
    a.position = None
    a.try_late_join(now)
    assert len(a.signals) == 1  # only once per signal
    c, _, _ = make_auto(k)
    c.start_day(datetime(2026, 10, 28, 9, 0))
    c.enabled = True
    c.history = pd.concat([c.history, row], ignore_index=True)
    c.last_spot = float(row["close"].iloc[0]) * 1.01  # ran 1% away -> don't chase
    c.try_late_join(now)
    assert c.signals == [] and any("too late to chase" in e["msg"] for e in c.timeline)
    d, _, _ = make_auto(k)
    d.start_day(datetime(2026, 10, 28, 9, 0))
    d.enabled = True
    d.history = pd.concat([d.history, row], ignore_index=True)
    d.last_spot = float(row["close"].iloc[0]) + 0.8 * float(ind_.atr(d.history, 14).iloc[-1])  # < 0.3% but > half a candle
    d.try_late_join(now)
    assert d.signals == [] and any("candle's range" in e["msg"] for e in d.timeline)


def test_autopilot_chooses_index_interval_and_risk(monkeypatch):
    import app.analysis as an

    def finder(rows, base, **k):
        # make BANKNIFTY 15-min the best robust result
        best = base.stop_loss_pct is not None and base.exchange == "NSE" and len(rows) > 0
        row = {"strategy": "supertrend", "name": "Supertrend", "params": {"n": 10, "mult": 3.0}, "robust": True,
               "score": 1.0, "test": {"trades": 9, "net_pnl": 900.0, "total_return_pct": 0.9, "win_rate_pct": 55.0},
               "train": {"trades": 20, "total_return_pct": 2.0}}
        return {"ranking": [dict(row)], "test_period": ["a", "b"], "combinations_tested": 10}

    calls = []
    real_eval = __import__("app.autotrade", fromlist=["AutoTrader"]).AutoTrader._evaluate

    def spy(self, index, interval, now, label):
        ev = real_eval(self, index, interval, now, label)
        calls.append((index, interval))
        if (index, interval) == ("BANKNIFTY", "15minute"):
            ev["res"]["ranking"][0]["score"] = 5.0
        return ev

    monkeypatch.setattr(an, "find_strategies", finder)
    from app.autotrade import AutoTrader
    monkeypatch.setattr(AutoTrader, "_evaluate", spy)
    a, _, _ = make_auto(FakeKite(), autopilot=True, max_daily_loss=6000.0)
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert len(calls) == 6
    c = a.cfg
    assert (c.index, c.interval, c.strategy) == ("BANKNIFTY", "15minute", "supertrend")
    assert c.side == "BUY" and c.option_rule == "AUTO" and c.risk["sl_mode"] == "points" and c.risk["sl_value"] >= 1
    assert a.pick["index"] == "BANKNIFTY" and a.builder.minutes == 15 and a.spot["tradingsymbol"] == "NIFTY BANK"


def test_autopilot_fixed_index_only_tests_that_index(monkeypatch):
    import app.analysis as an
    from app.autotrade import AutoConfig, AutoTrader

    def finder(rows, base, **k):
        row = {"strategy": "supertrend", "name": "Supertrend", "params": {"n": 10, "mult": 3.0}, "robust": True,
               "score": 1.0, "test": {"trades": 9, "net_pnl": 900.0, "total_return_pct": 0.9, "win_rate_pct": 55.0},
               "train": {"trades": 20, "total_return_pct": 2.0}}
        return {"ranking": [dict(row)], "test_period": ["a", "b"], "combinations_tested": 10}

    calls = []
    real_eval = AutoTrader._evaluate

    def spy(self, index, interval, now, label):
        calls.append(index)
        return real_eval(self, index, interval, now, label)

    monkeypatch.setattr(an, "find_strategies", finder)
    monkeypatch.setattr(AutoTrader, "_evaluate", spy)
    a, _, _ = make_auto(FakeKite(), autopilot=True, autopilot_index="BANKNIFTY", max_daily_loss=6000.0)
    assert a.cfg.index == "BANKNIFTY"
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert set(calls) == {"BANKNIFTY"} and len(calls) == 2
    assert a.cfg.index == "BANKNIFTY" and a.spot["tradingsymbol"] == "NIFTY BANK"
    d = AutoConfig()
    assert d.max_trades_per_day == 10 and d.paper_capital == 300_000.0 and d.max_auto_lots == 10 and d.autopilot_index == "ANY"
    with pytest.raises(ValueError):
        AutoConfig(autopilot=True, autopilot_index="FINNIFTY").validate()


def test_ai_pick_uses_combo_when_it_tests_better(monkeypatch):
    import app.analysis as an

    def finder(rows, base, strategies=None, grids=None, **k):
        mk = lambda key, pnl: {"strategy": key, "name": key, "params": {}, "robust": True, "score": pnl / 100,
                               "test": {"trades": 9, "net_pnl": pnl, "total_return_pct": 1.0, "win_rate_pct": 55.0},
                               "train": {"trades": 20, "total_return_pct": 2.0}}
        if strategies == ["combo"]:
            return {"ranking": [{**mk("combo", 1500.0), "params": grids["combo"][0]}], "test_period": ["a", "b"], "combinations_tested": 1}
        return {"ranking": [mk("supertrend", 1000.0), mk("trend_pullback", 800.0), mk("macd", 600.0), {**mk("orb", -5.0), "robust": False}],
                "test_period": ["a", "b"], "combinations_tested": 30}

    monkeypatch.setattr(an, "find_strategies", finder)
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert a.pick["strategy"] == "combo" and a.pick["combo"]
    assert [m["strategy"] for m in a.pick["params"]["members"]] == ["supertrend", "trend_pullback", "macd"]
    assert a.active_strategy[0] == "combo" and "Combo" in a.pick["name"]
    a.last_spot = 24000.0
    sig, _, _ = a.build_signal(1, None, datetime(2026, 10, 28, 9, 40))
    assert sig["strategy"] == "combo"


def test_combo_real_finder_runs():
    from app.analysis import find_strategies
    from app.backtest import BacktestConfig
    rows = _prior_days(n_days=5)
    p = {"members": [{"strategy": "sma_cross", "params": {"fast": 5, "slow": 21}}, {"strategy": "supertrend", "params": {}}]}
    base = BacktestConfig(strategy="sma_cross", sizing="fixed_qty", quantity=37, fixed_cost=150.0)
    res = find_strategies(rows, base, strategies=["combo"], grids={"combo": [p]})
    assert res["ranking"] and res["ranking"][0]["strategy"] == "combo"


def test_orb_trigger_hint_while_range_forms_and_after():
    a, _, hist = make_auto(FakeKite(), strategy="orb", params={"range_minutes": 45})
    day = DAY
    today = [dict(date=datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=15 + 5 * i),
                  open=24000, high=24010 + i, low=23990 - i, close=24000, volume=0) for i in range(4)]
    df = pd.concat([a.history, pd.DataFrame(today)], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    a.last_spot = 24000.0
    h = a._trigger_hint(df, "orb", {"range_minutes": 45}, datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=35))
    assert h["forming"] and h["range_high"] == 24013 and h["range_low"] == 23987 and "until 10:00" in h["text"]
    h2 = a._trigger_hint(df, "orb", {"range_minutes": 15}, datetime.combine(day, datetime.min.time()) + timedelta(hours=10))
    assert not h2["forming"] and "Breakout watch" in h2["text"]
    assert a._trigger_hint(df, "supertrend", {}, datetime.combine(day, datetime.min.time()) + timedelta(hours=10)) is None


def test_strong_trend_override_enters_once_and_exits_when_trend_fades(monkeypatch):
    import app.autotrade as at
    k = FakeKite()
    a, reg, _ = make_auto(k, max_daily_loss=100000.0, trend_override_min=6)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24000.0
    up6 = [{"group": "indicators", "ok": True, "label": "x"}] * 6
    no6 = [{"group": "indicators", "ok": False, "label": "x"}] * 6
    monkeypatch.setattr(at, "confirmations", lambda df, d: up6 if d > 0 else no6)
    candle = {"date": datetime(2026, 10, 28, 9, 40)}
    a.try_trend_override(candle, datetime(2026, 10, 28, 9, 45, 10))
    assert len(a.signals) == 1 and a.signals[0]["override"] and a.pos_strategy[0] == "trend_override"
    assert any("Strong-trend override" in w["label"] for w in a.signals[0]["why"])
    a.position = None
    a.try_trend_override(candle, datetime(2026, 10, 28, 9, 50, 10))
    assert len(a.signals) == 1  # same push: no second entry
    three = [{"group": "indicators", "ok": i < 3, "label": "x"} for i in range(6)]
    monkeypatch.setattr(at, "confirmations", lambda df, d: three if d > 0 else no6)
    a.try_trend_override({"date": datetime(2026, 10, 28, 9, 50)}, datetime(2026, 10, 28, 9, 55, 10))  # cools off -> re-armed
    monkeypatch.setattr(at, "confirmations", lambda df, d: up6 if d > 0 else no6)
    a.try_trend_override({"date": datetime(2026, 10, 28, 10, 0)}, datetime(2026, 10, 28, 10, 5, 10))
    assert len(a.signals) == 2  # fresh push -> second override entry
    off, _, _ = make_auto(k, trend_override_min=0)
    off.start_day(datetime(2026, 10, 28, 9, 0))
    off.enabled = True
    off.try_trend_override(candle, datetime(2026, 10, 28, 9, 45, 10))
    assert off.signals == []


def test_auto_position_gets_target_exit_via_quote_poll():
    from app.options_manager import OptionsManager
    import inspect
    assert "_poll_stale_positions" in dict(inspect.getmembers(OptionsManager))
    k = FakeKite()
    p = ManagedPosition(spec(), RISK, "paper", lambda: k)
    p.open(100.0)
    assert p.is_open and p.target
    class Mgr:
        POLL_AFTER_S = 8
        positions = {p.id: p}
        class deps:
            @staticmethod
            def quote(keys):
                return {kk: {"last_price": p.target + 1} for kk in keys}
    OptionsManager._poll_stale_positions(Mgr)
    assert p.state == "POSITION_CLOSED" and p.exit_reason == "target"


def test_supertrend_hint_gives_flip_level():
    a, _, hist = make_auto(FakeKite(), strategy="supertrend", params={"period": 10, "multiplier": 3.0})
    a.start_day(datetime(2026, 10, 28, 9, 0))
    h = a._trigger_hint(a.history, "supertrend", {"period": 10, "multiplier": 3.0}, datetime(2026, 10, 28, 10, 0))
    assert h and h["direction"] in ("UP", "DOWN") and h["level"] > 0 and "turns it" in h["text"]



def test_strategy_agreement_enters_when_enough_agree(monkeypatch):
    k = FakeKite()
    a, reg, _ = make_auto(k, max_daily_loss=100000.0, consensus_min=3)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24000.0
    monkeypatch.setattr(a, "consensus", lambda df=None: {"up": ["Supertrend", "MACD", "Trend Pullback"], "down": ["RSI"], "total": 8})
    candle = {"date": datetime(2026, 10, 28, 9, 40)}
    a.try_consensus(candle, datetime(2026, 10, 28, 9, 45, 10))
    assert len(a.signals) == 1 and a.pos_strategy[0] == "consensus"
    assert any("3 of 8 strategies point UP" in w["label"] for w in a.signals[0]["why"])
    a.position = None
    a.try_consensus(candle, datetime(2026, 10, 28, 9, 50, 10))
    assert len(a.signals) == 1  # same push
    monkeypatch.setattr(a, "consensus", lambda df=None: {"up": ["Supertrend"], "down": [], "total": 8})
    a.try_consensus(candle, datetime(2026, 10, 28, 9, 55, 10))  # re-arms
    monkeypatch.setattr(a, "consensus", lambda df=None: {"up": ["Supertrend", "MACD", "ORB", "SMA"], "down": [], "total": 8})
    a.try_consensus({"date": datetime(2026, 10, 28, 10, 0)}, datetime(2026, 10, 28, 10, 5, 10))
    assert len(a.signals) == 2


def test_consensus_counts_real_strategies():
    a, _, _ = make_auto(FakeKite(), consensus_min=3)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    c = a.consensus()
    assert c["total"] >= 6 and len(c["up"]) + len(c["down"]) <= c["total"]



def test_any_strategy_fresh_signal_enters_and_uses_its_exit(monkeypatch):
    from app import strategies as st
    k = FakeKite()
    a, reg, _ = make_auto(k, max_daily_loss=100000.0, any_strategy_entry=True)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24000.0

    real = {key: s.signals for key, s in st.REGISTRY.items()}
    def fake_for(key):
        def f(df, p=None):
            out = real[key](df, p).copy()
            out[:] = False
            if key == "macd":
                out.iloc[-1, out.columns.get_loc("entry_long")] = True
            return out
        return f
    for key, s in st.REGISTRY.items():
        monkeypatch.setattr(s, "signals", fake_for(key))
    ok = a.try_any_strategy({"date": datetime(2026, 10, 28, 9, 40)}, datetime(2026, 10, 28, 9, 45, 10))
    assert ok and len(a.signals) == 1 and a.pos_strategy[0] == "macd"
    assert any("Any-strategy entry" in w["label"] for w in a.signals[0]["why"])


def test_enable_acts_on_existing_agreement(monkeypatch):
    k = FakeKite()
    a, reg, _ = make_auto(k, max_daily_loss=100000.0, consensus_min=3)
    monkeypatch.setattr(type(a), "consensus", lambda self, df=None: {"up": ["A", "B", "C"], "down": [], "total": 8})
    a.last_spot = 24000.0
    a.enable(datetime(2026, 10, 28, 9, 47, 30))
    assert len(a.signals) == 1 and a.signals[0]["risk_check"] == "PASS"


def test_autopilot_risk_gets_trailing_stop():
    c = AutoConfig(autopilot=True, risk={"sl_mode": "points", "sl_value": 12.0, "target_mode": "rr", "target_value": 2.0, "trail_mode": "none"})
    c.validate()
    assert c.risk["trail_mode"] == "points" and c.risk["trail_value"] == 12.0 and c.risk["trail_activate_rr"] == 1.0


def test_risk_tuning_picks_best_numbers_and_needs_unseen_profit(monkeypatch):
    import app.analysis as an
    from app.backtest import BacktestConfig

    def finder(rows, base, strategies=None, grids=None, **k):
        rr = round(base.target_pct / base.stop_loss_pct, 2)
        win = {0.75: 72.0, 1.0: 62.0, 1.5: 55.0, 2.0: 41.0}[rr]
        ret = {0.75: 1.1, 1.0: 1.2, 1.5: 1.5, 2.0: 1.0}[rr]
        return {"ranking": [{"strategy": "supertrend", "params": {}, "train": {"win_rate_pct": win, "total_return_pct": ret, "trades": 20},
                             "test": {"win_rate_pct": win, "net_pnl": 500.0, "trades": 8}}]}
    monkeypatch.setattr(an, "find_strategies", finder)
    a, _, _ = make_auto(FakeKite(), autopilot=True)
    ev = {"atr": 20.0, "price": 24000.0, "rows": [], "base": BacktestConfig(strategy="supertrend")}
    t = a._tune_risk({"strategy": "supertrend", "params": {}, "name": "Supertrend"}, ev)
    assert len(t["table"]) == 24 and t["best"]["kept"]
    assert t["best"]["target_rr"] == 0.75  # 1.1 x 0.72^1.5 = 0.67 beats 1.5 x 0.55^1.5 = 0.61: higher win rate preferred
    ev["tuned"] = t
    a._apply_autopilot({"index": "NIFTY", "interval": "5minute", "strategy": "supertrend", "params": {}}, ev)
    assert a.cfg.risk["target_value"] == 0.75 and "breakeven_rr" in a.cfg.risk


def test_breakeven_stop_in_backtest_and_live_rules():
    from app.backtest import BacktestConfig, run_backtest
    from app.risk import RiskConfig, trail_stop
    cfg = RiskConfig(breakeven_rr=0.5)
    assert trail_stop(cfg, 1, 100.0, 90.0, 10.0, 104.0) == 90.0
    assert trail_stop(cfg, 1, 100.0, 90.0, 10.0, 105.0) == 100.0
    rows = intraday(days=6)
    base = dict(strategy="sma_cross", params={"fast": 3, "slow": 8}, sizing="fixed_qty", quantity=10, lot_size=1,
                fixed_cost=0.0, segment="equity_intraday", stop_loss_pct=0.3, target_pct=0.6)
    a, b = run_backtest(rows, BacktestConfig(**base)), run_backtest(rows, BacktestConfig(**base, breakeven_at_pct=0.15))
    assert len(b["trades"]) >= 1 and a["trades"]


# ------------------------------------------------------------------ filters, time-stop, entry types, journal
def test_entry_type_is_tagged_on_position():
    from app.autotrade import AutoTrader as AT
    assert AT.entry_type_for(None, False, None) == "strategy"
    assert AT.entry_type_for("any:x", False, ("rsi_reversion", {})) == "any"
    assert AT.entry_type_for("c", "consensus", None) == "agreement"
    assert AT.entry_type_for("t", True, None) == "override"
    assert AT.entry_type_for("late", False, None) == "late"
    a, reg, _ = make_auto(FakeKite())
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    assert reg and reg[0].stats["entry_type"] == "strategy" and a.signals[-1]["entry_type"] == "strategy"


def test_lunch_and_vix_filters_block_new_entries():
    kite = FakeKite()
    a, reg, _ = make_auto(kite, skip_lunch=True, entry_end="14:00", exit_time="15:00")
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 12, 35)}, datetime(2026, 10, 28, 12, 40, 1))
    assert not reg and any("Lunch" in p for p in a.signals[-1]["risk_problems"])
    a.cfg.skip_lunch, a.cfg.max_vix = False, 20
    a._vix_cache = (__import__("time").monotonic(), 25.0)
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 12, 45)}, datetime(2026, 10, 28, 12, 50, 1))
    assert not reg and any("VIX" in p for p in a.signals[-1]["risk_problems"])
    a._vix_cache = (__import__("time").monotonic(), 15.0)
    assert a.entry_filters(datetime(2026, 10, 28, 12, 50), ["sma_cross"]) == []


def test_chop_filter_exempts_reversion():
    a, _, _ = make_auto(FakeKite(), min_adx=60)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert any("Chop" in x for x in a.entry_filters(datetime(2026, 10, 28, 10, 0), ["sma_cross"]))
    assert a.entry_filters(datetime(2026, 10, 28, 10, 0), ["rsi_reversion"]) == []


def test_time_stop_exits_a_flat_trade():
    a, reg, _ = make_auto(FakeKite(), time_stop_bars=3)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    pos = reg[0]
    assert pos.is_open
    pos.entry_time = datetime(2026, 10, 28, 9, 40).isoformat()
    pos.ltp = pos.entry_price  # going nowhere
    assert not a.check_time_stop({"date": datetime(2026, 10, 28, 9, 45)})  # only 2 candles
    assert a.check_time_stop({"date": datetime(2026, 10, 28, 9, 50)})
    assert pos.state == "POSITION_CLOSED" and pos.exit_reason == "time_stop"


def test_quick_target_on_slow_trade():
    a, reg, _ = make_auto(FakeKite(), quick_target_pts=20, quick_target_after_bars=3, time_stop_bars=0, quick_target_auto=False)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    pos = reg[0]
    pos.entry_time = datetime(2026, 10, 28, 9, 40).isoformat()
    pos.target = pos.entry_price + 60  # far target
    pos.ltp = pos.entry_price + 5
    assert a.check_quick_target({"date": datetime(2026, 10, 28, 9, 45)}) is None  # only 2 candles
    assert a.check_quick_target({"date": datetime(2026, 10, 28, 9, 50)}) == "lowered"
    assert pos.target == pytest.approx(pos.entry_price + 20, abs=0.05) and pos.is_open
    assert a.check_quick_target({"date": datetime(2026, 10, 28, 9, 55)}) is None  # once per trade
    pos.on_tick(pos.entry_price + 21)
    assert pos.state == "POSITION_CLOSED" and pos.exit_reason == "quick_target"

    # already beyond +20 after the slow candles -> book straight away; a near target is left alone
    b, reg2, _ = make_auto(FakeKite(), quick_target_pts=20, quick_target_after_bars=3, time_stop_bars=0, quick_target_auto=False)
    b.start_day(datetime(2026, 10, 28, 9, 0))
    b.enabled, b.last_spot = True, 24010.0
    b.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    q = reg2[0]
    q.entry_time = datetime(2026, 10, 28, 9, 40).isoformat()
    q.target, q.ltp = q.entry_price + 15, q.entry_price + 10
    assert b.check_quick_target({"date": datetime(2026, 10, 28, 9, 50)}) is None
    q.target, q.ltp = q.entry_price + 60, q.entry_price + 25
    assert b.check_quick_target({"date": datetime(2026, 10, 28, 9, 50)}) == "booked"
    assert q.exit_reason == "quick_target"


def test_theta_cost_and_strict_proof():
    from app.autotrade import option_theta_per_bar, strict_robust
    from app.backtest import BacktestConfig, run_backtest
    t5 = option_theta_per_bar(24000, 5, 75, 14)
    assert 10 < t5 < 40 and option_theta_per_bar(24000, 15, 75, 14) == pytest.approx(3 * t5, rel=0.02)
    rows = intraday(days=5)
    base = dict(strategy="sma_cross", params={"fast": 3, "slow": 8}, sizing="fixed_qty", quantity=38, lot_size=1,
                fixed_cost=150.0, segment="equity_intraday")
    a, b = run_backtest(rows, BacktestConfig(**base)), run_backtest(rows, BacktestConfig(**base, bar_cost=20.0))
    ta, tb = a["trades"], b["trades"]
    assert ta and sum(t["charges"] for t in tb) > sum(t["charges"] for t in ta)
    good = {"robust": True, "test": {"trades": 20, "profit_factor": 1.5}}
    assert strict_robust(good)
    assert not strict_robust({"robust": True, "test": {"trades": 4, "profit_factor": 2.0}})
    assert not strict_robust({"robust": True, "test": {"trades": 25, "profit_factor": 1.05}})
    assert not strict_robust({"robust": True, "test": {"trades": 10, "profit_factor": 2.0}})


def test_breakdown_by_entry_type_drift_and_daily():
    from app.options_manager import performance_breakdown
    ps = []
    for i, (et, net) in enumerate([("strategy", 300), ("strategy", -100), ("agreement", 200), ("any", -50)]):
        p = ManagedPosition(spec(), RISK, "paper")
        p.open(100.0)
        p.request_exit("target")
        p.net, p.stats["entry_type"], p.stats["expected_win_pct"] = float(net), et, 60.0
        p.exit_time = f"2026-10-2{7 + i % 2}T10:00:00"
        ps.append(p)
    out = performance_breakdown(ps)
    assert out["by_entry_type"]["strategy"]["trades"] == 2 and out["by_entry_type"]["strategy"]["net"] == 200
    assert out["drift"]["expected_win_pct"] == 60 and out["drift"]["actual_win_pct"] == 50
    assert [d["day"] for d in out["daily"]] == ["2026-10-28", "2026-10-27"] and out["daily"][0]["trades"] == 2


def test_midday_repick_runs_once_when_flat():
    a, _, _ = make_auto(FakeKite(), strategy_source="ai", midday_repick="11:45", entry_end="14:00", exit_time="15:00")
    calls = []
    a.start_pick = lambda now: calls.append(now)
    a.pick = {"day": "2026-10-28"}
    a.maybe_midday_repick(datetime(2026, 10, 28, 11, 30), "ENTRY")
    assert not calls
    a.maybe_midday_repick(datetime(2026, 10, 28, 11, 46), "ENTRY")
    a.maybe_midday_repick(datetime(2026, 10, 28, 11, 50), "ENTRY")
    assert len(calls) == 1


def test_real_cost_model_and_luck_check():
    from app.autotrade import AutoTrader as AT, luck_threshold, option_round_trip_cost
    c = option_round_trip_cost("NIFTY", 25000, 65, 14)
    assert 180 < c < 320  # ~₹70 charges + ~₹160 slippage on a ~₹127 premium
    assert option_round_trip_cost("NIFTY", 25000, 650, 14) > 7 * c  # scales with size (a flat fee did not)
    assert luck_threshold(66) == pytest.approx(2.89, abs=0.01) and luck_threshold(2) < luck_threshold(200)
    assert AT._luck({"test": {"t_stat": 3.2}}, 66)["passed"] and not AT._luck({"test": {"t_stat": 1.5}}, 66)["passed"]
    assert not AT._luck({"test": {}}, 66)["passed"]


def test_finder_reports_t_stat():
    from app.analysis import find_strategies
    from app.backtest import BacktestConfig
    res = find_strategies(intraday(days=20), BacktestConfig(strategy="sma_cross", sizing="fixed_qty", quantity=10, lot_size=1),
                          strategies=["sma_cross"])
    assert all("t_stat" in r["test"] for r in res["ranking"])


def test_streak_weekly_and_capital_limits():
    a, reg, _ = make_auto(FakeKite(), max_consecutive_losses=2, max_weekly_loss=500, max_daily_loss=100000)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    for i in range(2):
        p = ManagedPosition(spec(), RISK, "paper")
        p.open(100.0)
        p.on_tick(70.0)  # stop-loss
        a.position = p
        a._account_closed()
    assert a.loss_streak == 2 and "losing trades in a row" in (a.day_blocked or "")
    a.pnl_days = {"2026-10-26": -300.0, "2026-10-27": -250.0, "2026-10-20": -9999.0}
    assert any("Weekly loss limit" in x for x in a.period_limits(date(2026, 10, 28)))
    assert not a.period_limits(date(2026, 11, 2))  # new week
    a.cfg.max_monthly_loss = 1000
    assert any("Monthly" in x for x in a.period_limits(date(2026, 10, 30)))
    b, reg2, _ = make_auto(FakeKite(), account_capital=10000, risk_per_trade_pct=1.0)
    b.start_day(datetime(2026, 10, 28, 9, 0))
    b.enabled, b.last_spot = True, 24010.0
    b.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    assert not reg2 and any("1 lot is too big" in x for x in b.signals[-1]["risk_problems"])


def test_new_safer_defaults():
    c = AutoConfig()
    assert (c.any_strategy_entry, c.consensus_min, c.trend_override_min, c.max_vix) == (False, 0, 0, 30.0)


def test_morning_readiness_checklist():
    from app.options_manager import OptionsManager

    class KS:
        kite = FakeKite()

        def instruments(self):
            return INST

    class Hub:
        def __getattr__(self, name):
            return lambda *a, **k: None
    with tempfile.TemporaryDirectory() as d:
        om = OptionsManager(KS(), Store(os.path.join(d, "s.db")), Hub())
        r = om.readiness(datetime(2026, 10, 28, 9, 5))
        names = {c["name"]: c["ok"] for c in r["checks"]}
        assert names["Market open today"] and "Kite login" in names and names["Auto Trade enabled"] is False
        assert om.readiness(datetime(2026, 11, 1, 9, 5))["checks"][0]["ok"] is False  # Sunday


def test_expiry_and_event_day_rules():
    from app import events
    a, reg, _ = make_auto(FakeKite(), expiry_day_mode="skip")
    a.start_day(datetime(2026, 11, 3, 9, 0))
    assert a.is_expiry_day(date(2026, 11, 3)) and not a.is_expiry_day(date(2026, 11, 4))
    assert any("expiry day" in x for x in a.entry_filters(datetime(2026, 11, 3, 10, 0), ["sma_cross"]))
    a.cfg.expiry_day_mode = "careful"
    assert not a.entry_filters(datetime(2026, 11, 3, 10, 0), ["sma_cross"])
    assert any("13:30" in x for x in a.entry_filters(datetime(2026, 11, 3, 13, 40), ["sma_cross"]))
    assert events.event_on(date(2026, 12, 4)) == "RBI policy"
    a.cfg.event_day_mode = "skip"
    assert any("RBI policy" in x for x in a.entry_filters(datetime(2026, 12, 4, 10, 0), ["sma_cross"]))
    with tempfile.TemporaryDirectory() as d:
        events.save_user_events(d, [{"day": "2026-11-20", "event": "My event"}])
        assert events.event_on(date(2026, 11, 20), d) == "My event"


def test_itm_autopilot_delta_and_cost():
    from app.autotrade import delta_for, option_round_trip_cost
    assert delta_for("ITM") > delta_for("ATM") > delta_for("OTM")
    assert option_round_trip_cost("NIFTY", 25000, 65, 14, "ITM") > option_round_trip_cost("NIFTY", 25000, 65, 14, "ATM")


def test_engine_replay_runs_and_compares_with_random():
    from app.replay import replay_engine
    rows = intraday(days=30, start_price=24000, vol=0.0015)
    cfg = AutoConfig(strategy="sma_cross", params={"fast": 5, "slow": 20}, min_confidence=0, entry_end="14:30", exit_time="15:10",
                     max_trades_per_day=3, max_daily_loss=20000, max_capital=100000, max_vix=0, min_adx=0, skip_lunch=False,
                     risk={"sl_mode": "pct", "sl_value": 25, "target_mode": "rr", "target_value": 2})
    r = replay_engine(cfg, rows, days=8)
    assert r["config"]["days"] == 8 and r["summary"]["trades"] > 0
    assert r["random"]["runs"] > 0 and 0 <= r["random"]["beats_pct"] <= 100
    assert r["verdict"]["level"] in ("unknown", "bad", "weak", "promising", "good")
    assert all(t["entry_type"] for t in r["trades"])


def test_paper_auto_resumes_after_restart_live_does_not():
    from app.options_manager import OptionsManager

    class KS:
        kite = FakeKite()

        def instruments(self):
            return INST

        def historical(self, *a, **k):
            return _prior_days()

    class Hub:
        def __getattr__(self, name):
            return lambda *a, **k: None
    with tempfile.TemporaryDirectory() as d:
        st = Store(os.path.join(d, "s.db"))
        KS.kite.profile = lambda self=None: {"user_name": "T"}
        om = OptionsManager(KS(), st, Hub())
        p = om.create_auto(AutoConfig(strategy="sma_cross", params={"fast": 2, "slow": 3}, min_confidence=0))
        p.enabled = True
        om._save_auto(p)
        om2 = OptionsManager(KS(), st, Hub())
        a = om2.autos[p.id]
        assert not a.enabled and a.resume_pending
        om2._resume_after_restart(datetime(2026, 10, 28, 9, 20))
        assert a.enabled and not a.resume_pending
        a.cfg.mode = "live"
        a.enabled = True
        om2._save_auto(a)
        om3 = OptionsManager(KS(), st, Hub())
        assert not om3.autos[p.id].resume_pending


def test_spread_calibration_and_skip_categories():
    from app.quality import calibrated_slippage_pct, measured_spreads, skip_category
    assert skip_category("India VIX 25.0 is above your limit 22") == "vix"
    assert skip_category("Max 3 trades/day reached") == "max trades"
    assert skip_category("something else") == "other"
    with tempfile.TemporaryDirectory() as d:
        assert calibrated_slippage_pct(d, 1.0) == (1.0, "standard estimate (not enough saved quotes yet)")
        q = os.path.join(d, "option_quotes")
        os.makedirs(q)
        for day in ("2026-10-26", "2026-10-27", "2026-10-28"):
            with open(os.path.join(q, f"{day}.csv"), "w") as f:
                f.write("time,symbol,ltp,bid,ask,spot\n")
                for i in range(100):
                    f.write(f"10:{i % 60:02d}:00,NIFTYX,100,99.6,100.4,24000\n")
        m = measured_spreads(d)
        assert m["days"] == 3 and m["median_spread_pct"] == pytest.approx(0.8, abs=0.01)
        slip, src = calibrated_slippage_pct(d, 1.0)
        assert slip == pytest.approx(0.65, abs=0.01) and "measured" in src


def test_rejected_signal_counts_reason_and_position_path_and_backup():
    a, reg, _ = make_auto(FakeKite(), skip_lunch=True, entry_end="14:00", exit_time="15:00")
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 12, 35)}, datetime(2026, 10, 28, 12, 40, 1))
    assert a.skips["2026-10-28"]["lunch"] == 1
    p = ManagedPosition(spec(), RISK, "paper")
    p.open(100.0)
    p.on_tick(101.0, now=1000.0)
    p.on_tick(102.0, now=1005.0)
    p.on_tick(103.0, now=1030.0)
    assert [x[1] for x in p.path] == [101.0, 103.0]
    assert ManagedPosition.restore(p.snapshot()).path == p.path
    with tempfile.TemporaryDirectory() as d:
        st = Store(os.path.join(d, "s.db"))
        st.set("k", {"v": 1})
        st.backup(os.path.join(d, "b.db"))
        assert Store(os.path.join(d, "b.db")).get("k") == {"v": 1}


def test_opening_gap_limit_setting_and_reset():
    a, _, _ = make_auto(FakeKite(), max_open_gap_pct=1.5)
    a.levels = {"prev_close": 24000.0}
    a._set_open(24000 * 1.0174)
    assert a.day_blocked and a.day_blocked.startswith("Opening gap")
    a.cfg.max_open_gap_pct = 2.5
    a.reset_for_new_settings()
    assert a.day_blocked is None
    a._set_open(24000 * 1.0174)
    assert a.day_blocked is None
    a.cfg.max_open_gap_pct = 0  # off
    a._set_open(24000 * 1.05)
    assert a.day_blocked is None


def _finder_weak_profit():
    # profitable on unseen data but too thin for the strict proof (only 7 trades)
    row = {"strategy": "sma_cross", "name": "SMA crossover", "params": {"fast": 2, "slow": 3}, "robust": True,
           "test": {"trades": 7, "net_pnl": 900.0, "total_return_pct": 0.9, "win_rate_pct": 57.0, "profit_factor": 1.6, "t_stat": 1.1},
           "train": {"trades": 20, "total_return_pct": 2.0}}
    return lambda rows, base, **k: {"ranking": [dict(row)], "test_period": ["a", "b"], "combinations_tested": 42}


def test_paper_practice_pick_when_strict_finds_nothing(monkeypatch):
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", _finder_weak_profit())
    a, reg, _ = make_auto(FakeKite(), strategy_source="ai", strict_proof=True)
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert a.pick["practice"] and a.active_strategy[0] == "sma_cross" and a.day_blocked is None
    assert a.pick["tested"][0]["why"].startswith("PRACTICE")
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    assert reg and reg[0].stats["proof"] == "practice"
    b, _, _ = make_auto(FakeKite(), strategy_source="ai", strict_proof=True, paper_practice=False)
    b.run_async = False
    b.start_day(datetime(2026, 10, 28, 9, 0))
    assert b.active_strategy is None and "WAIT" in b.day_blocked


def test_midday_repick_keeps_morning_pick_when_nothing_better(monkeypatch):
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", _fake_finder(True))
    a, _, _ = make_auto(FakeKite(), strategy_source="ai", entry_end="14:00", exit_time="15:00", midday_repick="11:45")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled = True
    assert a.pick["strategy"] == "sma_cross"
    monkeypatch.setattr(an, "find_strategies", _fake_finder(False))
    a.maybe_midday_repick(datetime(2026, 10, 28, 11, 46), "ENTRY")
    assert a.pick["strategy"] == "sma_cross" and a.day_blocked is None
    assert any("keeping this morning's pick" in e["msg"] for e in a.timeline)


def test_repick_keeps_trading_current_strategy_while_testing(monkeypatch):
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", _fake_finder(True))
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    seen = []
    real = a._pick
    a._pick = lambda now: (seen.append(a.active_strategy), real(now))
    a.repick(datetime(2026, 10, 28, 10, 0))
    assert seen[0] == ("sma_cross", {"fast": 2, "slow": 3})  # still active during the re-test


def _trend_day_rows():
    rows = _prior_days(n_days=5)
    p = rows[-1]["close"]
    for k in range(60):  # today: +2% grind up with small dips
        t = datetime(2026, 10, 28, 9, 15) + timedelta(minutes=5 * k)
        drift = 15.0 if k % 6 else -20.0
        o, c = p, p + drift
        rows.append(dict(date=t, open=o, high=max(o, c) + 2, low=min(o, c) - 8, close=c, volume=0))
        p = c
    return rows


def test_trend_day_strategy_fires_on_strong_up_day():
    from app.strategies import REGISTRY
    df = pd.DataFrame(_trend_day_rows())
    sig = REGISTRY["trend_day"].signals(df, REGISTRY["trend_day"].resolve({"adx_min": 20}))
    today = df["date"].dt.date == DAY
    assert sig["entry_long"][today].sum() >= 1 and sig["entry_long"][today].sum() <= 2
    assert not sig["entry_short"][today].any()


def test_trend_day_entry_trades_even_without_ai_pick():
    a, reg, _ = make_auto(FakeKite(), strategy_source="ai", entry_end="14:45", exit_time="15:10", trend_day_entry="paper")
    rows = _trend_day_rows()
    a.history = pd.DataFrame(rows[:-1])
    a.history["date"] = pd.to_datetime(a.history["date"])
    a.day, a.enabled, a.last_spot = DAY, True, rows[-1]["close"]
    a.pick = {"strategy": None, "day": DAY.isoformat()}
    a.day_blocked = "AI found no strategy that made money on unseen data — WAIT today"
    a.levels = {"prev_close": rows[-61]["close"]}
    fired = False
    for r in rows[-40:]:
        a.history = a.history[a.history["date"] < pd.Timestamp(r["date"])]
        a._on_candle(r, r["date"] + timedelta(minutes=5, seconds=1))
        if reg:
            fired = True
            break
    assert fired and reg[0].stats["entry_type"] == "trendday"
    assert a.day_blocked.startswith("AI found")  # the AI's WAIT stays for its own pick
    b, reg2, _ = make_auto(FakeKite(), strategy_source="ai", trend_day_entry="paper", mode="paper")
    b.cfg.mode = "live"
    assert not b.try_trend_day(rows[-1], datetime(2026, 10, 28, 11, 0))


def test_day_builder_profiles_today_and_finds_similar_days():
    from app.backtest import BacktestConfig
    from app.daybuilder import build_for_today, label_day
    rows = intraday(days=45, start_price=24000, vol=0.0015)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    today = df["date"].dt.date.max()
    base = BacktestConfig(strategy="sma_cross", product="MIS", segment="equity_intraday", lot_size=1, sizing="fixed_qty",
                          quantity=39, fixed_cost=100.0, bar_cost=5.0, slippage_bps=1.0)
    res = build_for_today(df, today, datetime.strptime("10:15", "%H:%M").time(), base)
    assert res["profile"]["label"] in ("TREND UP", "TREND DOWN", "RANGE", "VOLATILE", "MIXED")
    assert len(res["similar_days"]) == 15 and all(d["day"] < today.isoformat() for d in res["similar_days"])
    assert res["tested"] > 0 and isinstance(res["why"], str)
    if res["best"]:
        b = res["best"]
        assert b["older_half"] > 0 and b["newer_half"] > 0 and b["double_cost_net"] > 0
    assert label_day({"move_pct": 1.2, "above_vwap": 0.9, "efficiency": 0.5, "vwap_crosses": 1, "range_pct": 1.3, "gap_pct": 1, "adx": 40}) == "TREND UP"


def test_day_built_strategy_trades_with_its_own_tag():
    a, reg, _ = make_auto(FakeKite(), strategy_source="ai", entry_end="14:45", exit_time="15:10", day_builder="paper")
    rows = _trend_day_rows()
    a.history = pd.DataFrame(rows[:-1])
    a.history["date"] = pd.to_datetime(a.history["date"])
    a.day, a.enabled, a.last_spot = DAY, True, rows[-1]["close"]
    a.pick = {"strategy": None, "day": DAY.isoformat()}
    a.day_build = {"day": DAY.isoformat(), "index": "NIFTY", "interval": "5minute", "checkpoint": "10:15",
                   "profile": {"label": "TREND UP"}, "why": "test",
                   "best": {"strategy": "trend_day", "name": "Trend Day", "params": {"adx_min": 20}}}
    for r in rows[-40:]:
        a.history = a.history[a.history["date"] < pd.Timestamp(r["date"])]
        before = len(reg)
        a.cfg.trend_day_entry = "off"  # make sure the Day-built path is what trades
        a._on_candle(r, r["date"] + timedelta(minutes=5, seconds=1))
        if len(reg) > before:
            break
    assert reg and reg[0].stats["entry_type"] == "daybuilt"


def test_auto_money_sizes_everything_from_capital_and_conditions():
    a, reg, _ = make_auto(FakeKite(), autopilot=True, strategy_source="ai", auto_money=True, paper_capital=200000)
    a._vix_cache = (__import__("time").monotonic(), 14.0)
    m = a.apply_auto_money(datetime(2026, 10, 28, 9, 0))
    assert m["capital"] == 200000 and m["risk_per_trade"] == 2000 and a.cfg.max_daily_loss == 4000
    assert a.cfg.max_capital == 40000 and not m["why_lower"]
    trades_before = a.cfg.max_trades_per_day
    a.apply_auto_money(datetime(2026, 10, 28, 9, 5))
    assert a.cfg.max_trades_per_day == trades_before  # your own trades/day is kept
    a.loss_streak = 2
    a._vix_cache = (__import__("time").monotonic(), 24.0)
    m = a.apply_auto_money(datetime(2026, 10, 28, 11, 0))
    assert m["risk_pct"] == 0.25 and len(m["why_lower"]) == 2  # 1% x 0.5 x 0.5
    assert a._entry_band(100.0) == 0.5 and a._entry_band(300.0) == 1.5 and a._entry_band(900.0) == 4.5 and a._entry_band(2000.0) == 5.0
    b, _, _ = make_auto(FakeKite(), autopilot=False)
    assert b.apply_auto_money(datetime(2026, 10, 28, 9, 0)) is None and b._entry_band(300.0) == b.cfg.entry_tolerance_pts


def test_setup_quality_scales_risk_and_lots():
    a, reg, _ = make_auto(FakeKite(), autopilot=True, strategy_source="ai", auto_money=True, paper_capital=200000)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a._vix_cache = (__import__("time").monotonic(), 14.0)
    a.apply_auto_money(datetime(2026, 10, 28, 10, 0))
    a.pick = {"strategy": "sma_cross", "params": {}, "luck": {"passed": True}, "day": "2026-10-28"}
    hi = a._size_for_setup(1, "strategy")
    a.pick = {"strategy": "sma_cross", "params": {}, "practice": True, "day": "2026-10-28"}
    lo = a._size_for_setup(1, "strategy")
    assert hi["risk_pct"] > lo["risk_pct"] and hi["risk_pct"] <= 1.5 and lo["risk_pct"] >= 0.25
    assert a._size_for_setup(1, "trendday")["setup"] == "trendday"


def test_cpr_hint_explains_wide_cpr_and_levels():
    from app.strategies import REGISTRY
    a, _, _ = make_auto(FakeKite(), strategy="cpr_breakout", params={"max_width_pct": 0.25})
    df = pd.DataFrame(_prior_days(n_days=3))
    df["date"] = pd.to_datetime(df["date"])
    h = a._cpr_hint(df, {"max_width_pct": 0.25}, df["date"].iloc[-1].to_pydatetime())
    assert h and "cpr_top" in h and ("NOT trade today" in h["text"] or "Waiting for" in h["text"] or "already" in h["text"])
    h2 = a._cpr_hint(df, {"max_width_pct": 0.0001 + 0.05}, df["date"].iloc[-1].to_pydatetime())
    assert h2["width_pct"] >= 0


def test_second_position_while_first_is_open_and_shared_risk():
    a, reg, _ = make_auto(FakeKite(), max_open_positions=2, max_trades_per_day=5, max_daily_loss=100000, max_capital=200000)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    assert len(reg) == 1 and reg[0].is_active
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 50)}, datetime(2026, 10, 28, 9, 55, 1))
    assert len(reg) == 2 and len(a.active_positions()) == 2 and a.others == [reg[0]]
    assert a.open_risk() > 0 and not a.slot_free()
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 10, 5)}, datetime(2026, 10, 28, 10, 10, 1))
    assert len(reg) == 2  # max 2 open
    for p in reg:
        if p.is_open:
            p.request_exit("manual_close")
    a._account_closed()
    assert a.slot_free() and a.others == []


def test_one_position_mode_still_blocks():
    a, reg, _ = make_auto(FakeKite(), max_open_positions=1, max_trades_per_day=5)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 50)}, datetime(2026, 10, 28, 9, 55, 1))
    assert len(reg) == 1


def test_wide_stop_is_never_squeezed_to_fit_money():
    a, reg, _ = make_auto(FakeKite(), autopilot=True, strategy_source="ai", auto_money=True, paper_capital=100000,
                          risk={"sl_mode": "points", "sl_value": 60.0, "target_mode": "rr", "target_value": 2.0,
                                "trail_mode": "points", "trail_value": 60.0, "min_sl_pct": 1.0, "max_sl_pct": 90.0})
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a._vix_cache = (__import__("time").monotonic(), 14.0)
    a.enabled, a.last_spot = True, 24010.0
    a.pick = {"strategy": "sma_cross", "name": "SMA", "params": {}, "day": "2026-10-28", "atr": 5.0, "luck": {"passed": False},
              "test": {"net_pnl": 1000, "trades": 20, "win_rate_pct": 55}, "train": {"total_return_pct": 1}}
    a.cfg.strike_method = "ATM"
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    assert not reg and any("market-based stop" in x for x in a.signals[-1]["risk_problems"])
    assert not any("Stop tightened" in w["label"] for w in a.signals[-1]["why"])


def test_stop_moved_beyond_a_nearby_level():
    a, _ = _ready(ChainKite())
    import app.indicators as ind_
    atr_now = float(ind_.atr(a.history, 14).iloc[-1])
    spot, delta, stop_pts = 24010.0, 0.5, 10.0  # index stop 20 pts below -> 23990
    a.levels = {"P": 23990.0 + 0.2 * atr_now}  # pivot just above the stop: a retest would wick into it
    r = a._structure_stop(spot, 1, stop_pts, delta)
    assert r is not None and r[0] > stop_pts and "pivot" in r[1]
    assert r[0] == pytest.approx((spot - (a.levels["P"] - 0.4 * atr_now)) * delta, abs=0.11)
    a.levels = {"P": 23000.0}  # far away -> unchanged
    a.history = a.history.copy()
    assert a._structure_stop(spot, 1, stop_pts, delta) is None or "pivot" not in a._structure_stop(spot, 1, stop_pts, delta)[1]
    # PE (bearish): stop above, level just below it
    a.levels = {"P": 24030.0 - 0.3 * atr_now}
    r = a._structure_stop(spot, -1, stop_pts, delta)
    assert r is not None and r[0] > stop_pts


def test_money_setting_edit_keeps_todays_pick(monkeypatch):
    import app.analysis as an
    from dataclasses import replace as dc_replace
    monkeypatch.setattr(an, "find_strategies", _fake_finder(True))
    a, _, _ = make_auto(FakeKite(), strategy_source="ai")
    a.run_async = False
    a.start_day(datetime.now().replace(hour=9, minute=0))
    a.pick["day"] = datetime.now().date().isoformat()
    old = a.cfg
    a.cfg = dc_replace(old, max_daily_loss=old.max_daily_loss + 500, params=dict(old.params), risk=dict(old.risk))
    a.reset_for_new_settings(old)
    assert a.pick and a.pick["strategy"] == "sma_cross"
    old = a.cfg
    a.cfg = dc_replace(old, interval="15minute", params=dict(old.params), risk=dict(old.risk))
    a.reset_for_new_settings(old)
    assert a.pick is None


def test_trade_report_rows_summary_csv_and_html():
    from app.report import summarise, to_csv, to_html, trade_rows
    ps = []
    for i, net in enumerate([500.0, -200.0, 300.0]):
        p = ManagedPosition(spec(), RISK, "paper")
        p.open(100.0)
        p.request_exit("target" if net > 0 else "stop_loss")
        p.net, p.gross, p.charges = net, net + 50, 50.0
        p.entry_time = f"2026-10-2{7 + i}T10:0{i}:00"
        p.exit_time = f"2026-10-2{7 + i}T10:3{i}:00"
        p.stats.update(entry_type="strategy" if i else "trendday", why=["AI picked X", "BUY CE"], quote_at_entry=99.5,
                       strategy_name="SMA crossover", index="NIFTY", spot_at_entry=24000.0)
        ps.append(p)
    rows = trade_rows(ps, days=30, today=date(2026, 10, 30))
    assert len(rows) == 3 and rows[0]["entry_slippage"] is not None and rows[0]["why"] == "AI picked X | BUY CE"
    s = summarise(rows)
    assert s["trades"] == 3 and s["net"] == 600.0 and s["win_rate_pct"] == pytest.approx(66.7, abs=0.1)
    assert s["by_entry_type"]["trendday"]["trades"] == 1 and s["profit_factor"] == 4.0
    csv_text = to_csv(rows)
    assert csv_text.splitlines()[0].startswith("date,entry_time") and len(csv_text.splitlines()) == 4
    h = to_html(rows, 30, "all", "all", {"lunch": 2})
    assert "trading report" in h and "Trend Day" in h and "Skipped signals" in h
    assert trade_rows(ps, days=30, mode="live", today=date(2026, 10, 30)) == []


def test_backtest_applies_live_rules_time_stop_lunch_and_chop():
    from app.backtest import BacktestConfig, run_backtest
    rows = intraday(days=8)
    base = dict(strategy="sma_cross", params={"fast": 3, "slow": 8}, sizing="fixed_qty", quantity=10, lot_size=1,
                fixed_cost=0.0, segment="equity_intraday", stop_loss_pct=0.4, target_pct=0.8)
    plain = run_backtest(rows, BacktestConfig(**base))
    ts = run_backtest(rows, BacktestConfig(**base, time_stop_bars=3, time_stop_min_r=0.25))
    assert any(t["exit_reason"] == "time_stop" for t in ts["trades"])
    assert all(t["bars_held"] >= 2 for t in ts["trades"] if t["exit_reason"] == "time_stop")
    lunch = run_backtest(rows, BacktestConfig(**base, no_entry_window=("12:30", "13:15")))
    assert not any("12:30" <= t["entry_time"][11:16] < "13:15" for t in lunch["trades"])
    chop = run_backtest(rows, BacktestConfig(**base, min_adx=99.0))
    assert len(chop["trades"]) < len(plain["trades"])


def test_cooldown_after_loss_and_same_candle_accounting():
    a, reg, _ = make_auto(FakeKite(), cooldown_after_loss_bars=2, max_open_positions=2, max_trades_per_day=5,
                          max_daily_loss=100000, max_consecutive_losses=0)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    p = reg[0]
    p.on_tick(60.0)  # stop-loss → losing close
    p.exit_time = datetime(2026, 10, 28, 9, 42).isoformat()
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 40)}, datetime(2026, 10, 28, 9, 45, 1))
    assert len(reg) == 1 and any("Cooling down" in x for x in a.signals[-1]["risk_problems"])
    assert a.day_pnl < 0  # the losing trade was counted before sizing the new one
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 50)}, datetime(2026, 10, 28, 9, 55, 1))
    assert len(reg) == 2


def _open_ce(a, reg):
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    return reg[0]


def test_smart_exit_closes_when_market_turns(monkeypatch):
    import app.autotrade as at
    a, reg, _ = make_auto(FakeKite(), smart_exit=True)
    p = _open_ce(a, reg)
    monkeypatch.setattr(at, "confidence_for", lambda df, d: (17, []))
    p.ltp = p.entry_price - 0.2 * p.risk_unit
    vw = float(at.ind.session_vwap(a.history).iloc[-1])
    assert a.smart_manage(p, {"close": vw - 50}) == "exit_turned" and p.exit_reason == "ai_exit"


def test_smart_exit_locks_profit_and_lifts_target(monkeypatch):
    import app.autotrade as at
    a, reg, _ = make_auto(FakeKite(), smart_exit=True)
    p = _open_ce(a, reg)
    vw = float(at.ind.session_vwap(a.history).iloc[-1])
    monkeypatch.setattr(at, "confidence_for", lambda df, d: (67, []))
    p.ltp = p.entry_price + 1.1 * p.risk_unit
    assert a.smart_manage(p, {"close": vw + 50}) == "locked"
    assert p.stop == pytest.approx(p.entry_price + 0.5 * p.risk_unit, abs=0.06)
    monkeypatch.setattr(at, "confidence_for", lambda df, d: (100, []))
    p.ltp = p.target - 0.1 * p.risk_unit
    assert a.smart_manage(p, {"close": vw + 80}) == "lifted" and p.target is None and p.is_open
    b, reg2, _ = make_auto(FakeKite(), smart_exit=False)
    q = _open_ce(b, reg2)
    assert b.smart_manage(q, {"close": 0}) is None


class ChainKite(FakeKite):
    """FakeKite with India VIX and open interest per strike."""

    def __init__(self, premium=100.0, vix=14.0, oi=None):
        super().__init__(premium)
        self.vix, self.oi = vix, oi or {}

    def quote(self, keys):
        out = super().quote(keys)
        for k in keys:
            if k == "NSE:INDIA VIX":
                out[k] = {"last_price": self.vix}
            else:
                for strike, oi in self.oi.items():
                    if k.endswith(f"{strike}CE") or k.endswith(f"{strike}PE"):
                        out[k]["oi"] = oi
                out[k].setdefault("oi", 1000)
        return out


def _ready(kite, **over):
    a, reg, _ = make_auto(kite, **over)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    return a, reg


def test_iv_check_expensive_option_is_small_or_skipped():
    from app.options import bs_price, years_to_expiry
    now = datetime(2026, 10, 28, 9, 40, 1)
    yrs = years_to_expiry(now, date(2026, 11, 3))
    fair = bs_price(24010, 24000, yrs, 14.0, "CE")
    high = bs_price(24010, 24000, yrs, 14.0 * 1.45, "CE")
    crazy = bs_price(24010, 24000, yrs, 14.0 * 1.8, "CE")
    for prem, action in ((fair, "ok"), (high, "small"), (crazy, "skip")):
        a, _ = _ready(ChainKite(premium=prem), iv_mode="careful")
        from app.options import select_contract
        c = select_contract(INST, "NIFTY", EXP1, "CE", 24010.0, "ATM", 1)
        r = a._iv_check(c, prem, 24010.0, now, "CE")
        assert r["action"] == action, (prem, r)
        assert 0.3 < r["delta"] < 0.7
    a, _ = _ready(ChainKite(premium=high), iv_mode="skip")
    assert a._iv_check(c, high, 24010.0, now, "CE")["action"] == "skip"
    # careful + high IV -> the order is 1 lot
    a, _ = _ready(ChainKite(premium=high), iv_mode="careful", lots=3)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 35), now)
    assert sig["iv_level"] == "high" and sig["lots"] == 1


def test_iv_uses_own_history_once_available():
    a, _ = _ready(ChainKite(), iv_mode="careful")
    a.iv_hist["NIFTY"] = [[f"2026-10-{d:02d}", 10.0 + (i % 5)] for i, d in enumerate(range(20, 27)) for _ in range(4)]
    from app.options import bs_price, select_contract, years_to_expiry
    now = datetime(2026, 10, 28, 9, 40, 1)
    c = select_contract(INST, "NIFTY", EXP1, "CE", 24010.0, "ATM", 1)
    prem = bs_price(24010, c.strike, years_to_expiry(now, date(2026, 11, 3)), 13.0, "CE")
    r = a._iv_check(c, prem, 24010.0, now, "CE")
    assert r["pct"] is not None and "recent readings" in r["label"]


def test_cost_to_move_rule_skips_tiny_targets():
    a, _ = _ready(ChainKite(), min_move_cost_ratio=20)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 35), datetime(2026, 10, 28, 9, 40, 1))
    assert pos is None and any("x its costs" in p for p in sig["risk_problems"]) and sig["cost_pts"] > 0
    a, _ = _ready(ChainKite(), min_move_cost_ratio=3)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 35), datetime(2026, 10, 28, 9, 40, 1))
    assert pos is not None


def test_same_direction_guard():
    now = datetime(2026, 10, 28, 9, 40, 1)
    a, reg = _ready(ChainKite(), same_direction_mode="block", max_open_positions=3, max_daily_loss=10000.0)
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, now)
    assert len(reg) == 1
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 40), now)
    assert pos is None and any("same way" in p for p in sig["risk_problems"])
    sig, pos, _ = a.build_signal(-1, datetime(2026, 10, 28, 9, 40), now)  # opposite bet is fine
    assert pos is not None
    a, reg = _ready(ChainKite(), same_direction_mode="small", max_open_positions=3, lots=3, max_daily_loss=20000.0, max_capital=200000.0)
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, now)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 40), now)
    assert pos is not None and sig["lots"] == 1 and not a._careful


def test_oi_wall_caps_target_or_skips():
    now = datetime(2026, 10, 28, 9, 40, 1)
    # wall 240 index pts above spot: room ~ 0.5*240*0.9 = ~108 > stop (20) but < target (42) ? -> capped only if smaller
    a, _ = _ready(ChainKite(oi={24150: 9000}), oi_walls=True)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 35), now)
    assert sig["oi_wall"]["strike"] == 24150 and pos is not None
    room = sig["oi_wall"]["room_pts"]
    assert sig["target_price"] == pytest.approx(min(sig["entry_price"] + room, sig["entry_price"] + 42), abs=1)
    # wall right next to spot -> no room -> skipped
    # the at-the-money strike (24050, 40 pts away) is never a wall
    a, _ = _ready(ChainKite(premium=200.0, oi={24050: 9000}), oi_walls=True, max_daily_loss=20000.0, max_capital=200000.0)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 35), now)
    assert "oi_wall" not in sig and pos is not None
    # a real wall one strike out with a wide stop -> too little room -> skipped
    a, _ = _ready(ChainKite(premium=400.0, oi={24100: 9000}), oi_walls=True, max_daily_loss=40000.0, max_capital=400000.0)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 35), now)
    assert pos is None and any("open-interest wall" in p for p in sig["risk_problems"])
    # no clear wall -> unchanged
    a, _ = _ready(ChainKite(), oi_walls=True)
    sig, pos, _ = a.build_signal(1, datetime(2026, 10, 28, 9, 35), now)
    assert "oi_wall" not in sig and pos is not None


def test_auto_quick_target_scales_with_trade():
    a, reg = _ready(ChainKite(), quick_target_pts=20, quick_target_auto=True, time_stop_bars=0)
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    pos = reg[0]
    pts = a.quick_target_points(pos)
    assert pts == pytest.approx(max(min(0.8 * pos.risk_unit, 0.15 * pos.entry_price), 2.5 * (pos.stats.get("cost_pts") or 0), 1.0), abs=0.1)
    assert pts < 20  # ₹100 option, 20% stop -> quick target ~15 pts, not a flat 20


def test_mfe_mae_tracked_and_settings_version():
    p = ManagedPosition(spec(), RISK, "paper")
    p.open(100.0)
    for px in (97, 104, 95, 108, 101):
        p.on_tick(float(px))
    e = p.entry_price
    assert p.stats["mfe_pts"] == pytest.approx(108 - e, abs=0.01) and p.stats["mae_pts"] == pytest.approx(95 - e, abs=0.01)
    assert p.stats["risk_pts"] > 0
    a, reg = _ready(ChainKite())
    assert a.settings_v == 1
    old = AutoConfig(**{**a.cfg.__dict__})
    a.cfg.max_vix = 25
    assert a.bump_version(old) and a.settings_v == 2 and "max_vix" in a.versions[-1]["changes"]
    assert not a.bump_version(AutoConfig(**{**a.cfg.__dict__}))
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    assert reg[0].stats["settings_v"] == 2


def test_scorecard_tuning_and_versions_in_stats():
    from app.options_manager import golive_scorecard, performance_breakdown, tuning_suggestions
    ps = []
    for i in range(70):
        p = ManagedPosition(spec(), RISK, "paper")
        p.open(100.0)
        win = i % 3 != 0
        p.on_tick(112.0 if win else 96.0)
        p.on_tick(108.0 if win else 85.0)
        p.request_exit("manual_close")
        p.exit_time = f"2026-10-{1 + i % 25:02d}T10:00:00"
        p.stats["settings_v"] = 2 if i >= 10 else 1
        ps.append(p)
    sc = golive_scorecard(ps, 150000.0)
    keys = {i["key"]: i for i in sc["items"]}
    assert keys["trades"]["ok"] and keys["days"]["ok"] and "ready" in sc
    t = tuning_suggestions(ps)
    assert t["ready"] and {i["key"] for i in t["items"]} >= {"target", "quick"}
    br = performance_breakdown(ps)
    assert set(br["by_version"]) == {"1", "2"} and br["by_version"]["2"]["trades"] == 60
    assert "charges" in br["daily"][0]
    few = golive_scorecard(ps[:5], 150000.0)
    assert not few["ready"] and few["verdict"].startswith("Not ready")


def test_login_reminder_before_open(monkeypatch):
    from app import notify
    from app.options_manager import OptionsManager
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a) or True)

    class KS:
        class kite:
            @staticmethod
            def profile():
                raise RuntimeError("no token")

    m = OptionsManager.__new__(OptionsManager)
    m.ks, m.autos = KS(), {"x": type("A", (), {"enabled": True, "cfg": AutoConfig()})()}
    m.deps = type("D", (), {"data_dir": None})()
    monkeypatch.setattr(m, "_resume_after_restart", lambda now: None, raising=False)
    monkeypatch.setattr(m, "readiness", lambda now: {"checks": []}, raising=False)
    m._scheduled_checks(datetime(2026, 10, 28, 8, 46))
    assert sent and "log in" in sent[0][0]


def test_zerodha_day_summary_and_saved_copy(tmp_path):
    from app import account

    class K:
        def orders(self):
            return [{"order_id": "1", "tag": "ka123", "tradingsymbol": "NIFTY26N0324000CE", "transaction_type": "BUY", "quantity": 65,
                     "filled_quantity": 65, "status": "COMPLETE", "order_timestamp": datetime(2026, 11, 12, 9, 30)},
                    {"order_id": "2", "tag": "ka123", "tradingsymbol": "NIFTY26N0324000CE", "transaction_type": "SELL", "quantity": 65,
                     "filled_quantity": 65, "status": "COMPLETE", "order_timestamp": datetime(2026, 11, 12, 10, 0)},
                    {"order_id": "3", "tag": None, "tradingsymbol": "BANKNIFTY26NOV52000PE", "transaction_type": "BUY", "quantity": 30,
                     "filled_quantity": 30, "status": "COMPLETE", "order_timestamp": datetime(2026, 11, 12, 11, 0)},
                    {"order_id": "4", "tag": None, "tradingsymbol": "BANKNIFTY26NOV52000PE", "transaction_type": "SELL", "quantity": 30,
                     "filled_quantity": 0, "status": "REJECTED", "status_message": "Insufficient funds", "order_timestamp": datetime(2026, 11, 12, 11, 5)}]

        def trades(self):
            return [{"order_id": "1", "tradingsymbol": "NIFTY26N0324000CE", "transaction_type": "BUY", "quantity": 65, "average_price": 100.0,
                     "fill_timestamp": datetime(2026, 11, 12, 9, 30, 2)},
                    {"order_id": "2", "tradingsymbol": "NIFTY26N0324000CE", "transaction_type": "SELL", "quantity": 65, "average_price": 120.0,
                     "fill_timestamp": datetime(2026, 11, 12, 10, 0, 1)},
                    {"order_id": "3", "tradingsymbol": "BANKNIFTY26NOV52000PE", "transaction_type": "BUY", "quantity": 30, "average_price": 300.0,
                     "fill_timestamp": datetime(2026, 11, 12, 11, 0, 1)}]

        def positions(self):
            return {"day": [
                {"tradingsymbol": "NIFTY26N0324000CE", "exchange": "NFO", "product": "MIS", "quantity": 0, "buy_quantity": 65,
                 "buy_price": 100.0, "buy_value": 6500.0, "sell_quantity": 65, "sell_price": 120.0, "sell_value": 7800.0,
                 "pnl": 1300.0, "realised": 1300.0, "unrealised": 0, "last_price": 121.0},
                {"tradingsymbol": "BANKNIFTY26NOV52000PE", "exchange": "NFO", "product": "MIS", "quantity": 30, "buy_quantity": 30,
                 "buy_price": 300.0, "buy_value": 9000.0, "sell_quantity": 0, "sell_price": 0, "sell_value": 0,
                 "pnl": -150.0, "realised": 0, "unrealised": -150.0, "last_price": 295.0}]}

    d = account.day_summary(K(), "kitealgo", datetime(2026, 11, 12, 15, 40))
    t = d["totals"]
    assert t["pnl"] == 1150.0 and t["trades"] == 3 and t["rejected_orders"] == 1 and t["open_positions"] == 1
    n = next(r for r in d["symbols"] if r["symbol"].startswith("NIFTY"))
    assert n["by"] == "Kite Algo" and n["orders"] == 2 and n["charges_detail"]["brokerage"] == 40.0
    assert 40 < n["charges"] < 70 and n["net"] == round(1300 - n["charges"], 2)
    b = next(r for r in d["symbols"] if r["symbol"].startswith("BANK"))
    assert b["by"] == "Kite app / other" and b["charges_detail"]["brokerage"] == 20.0
    assert t["by_kite_algo"] == 1300.0 and t["by_hand"] == -150.0
    assert any(o["reason"] == "Insufficient funds" for o in d["orders"])
    account.save_day(tmp_path, d)
    assert account.load_day(tmp_path, "2026-11-12")["totals"]["pnl"] == 1150.0
    assert account.saved_days(tmp_path)[0]["day"] == "2026-11-12"
    assert account.load_day(tmp_path, "2026-11-11") is None


def _hist_trend_down(start=24500.0, n=80, drop=6.0, day="2026-10-28"):
    rows, px = [], start
    base = datetime.fromisoformat(day + "T09:15")
    for i in range(n):
        o = px
        px = px - drop
        rows.append({"date": base + timedelta(minutes=5 * (i % 75)) + timedelta(days=-(1 if i < n - 75 else 0)),
                     "open": o, "high": o + 2, "low": px - 2, "close": px, "volume": 1000})
    import pandas as pd
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def test_stretch_waits_for_pullback_then_enters():
    a, reg = _ready(ChainKite(), stretch_mode="pullback", pullback_wait_bars=4)
    a.history = _hist_trend_down()
    st = a.stretch_check(-1)
    assert st["stretched"] and st["rsi"] <= 30 and st["dist_atr"] >= 2.5
    last = a.history["date"].iloc[-1].to_pydatetime()
    now = last + timedelta(minutes=5, seconds=2)
    a.execute_signal(-1, {"date": last}, now)
    assert not reg and a.pending_pullback and a.pending_pullback["direction"] == -1
    assert any("stretched" in p for p in a.signals[-1]["risk_problems"])
    # a candle that does not reach the 20-EMA: still waiting
    c1 = {"date": last + timedelta(minutes=5), "open": st["close"], "high": st["close"] + 1, "low": st["close"] - 5, "close": st["close"] - 2, "volume": 1}
    assert not a.try_pullback(c1, now + timedelta(minutes=5)) and a.pending_pullback
    # bounce up to the 20-EMA that fails (red candle closing below it) -> enter
    e20 = a.stretch_check(-1)["ema20"]
    c2 = {"date": last + timedelta(minutes=10), "open": e20 - 4, "high": e20 + 2, "low": e20 - 10, "close": e20 - 9, "volume": 1}
    a.last_spot = e20 - 9
    import pandas as pd
    a.history = pd.concat([a.history, pd.DataFrame([c2])], ignore_index=True)  # _on_candle adds the candle first
    assert a.try_pullback(c2, now + timedelta(minutes=10))
    assert reg and reg[0].stats["entry_type"] == "pullback" and a.pending_pullback is None


def test_stretch_pullback_cancels_and_skip_mode():
    a, reg = _ready(ChainKite(), stretch_mode="pullback", pullback_wait_bars=2)
    a.history = _hist_trend_down()
    last = a.history["date"].iloc[-1].to_pydatetime()
    a.execute_signal(-1, {"date": last}, last + timedelta(minutes=5, seconds=2))
    late = {"date": last + timedelta(minutes=15), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    assert not a.try_pullback(late, last + timedelta(minutes=20)) and a.pending_pullback is None and not reg
    b, reg2 = _ready(ChainKite(), stretch_mode="skip")
    b.history = _hist_trend_down()
    b.execute_signal(-1, {"date": last}, last + timedelta(minutes=5, seconds=2))
    assert not reg2 and b.pending_pullback is None and any("your setting" in p for p in b.signals[-1]["risk_problems"])
    c, reg3 = _ready(ChainKite(), stretch_mode="off")
    c.history = _hist_trend_down()
    c.execute_signal(-1, {"date": last}, last + timedelta(minutes=5, seconds=2))
    assert reg3


def _open_trade(lots=2, **over):
    a, reg = _ready(ChainKite(), lots=lots, max_daily_loss=20000.0, max_capital=200000.0, **over)
    now = datetime(2026, 10, 28, 9, 40, 1)
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, now)
    return a, reg[0], now


def test_scale_out_books_half_and_frees_the_rest():
    a, p, now = _open_trade(lots=2)
    assert p.filled_qty == 2 * p.spec.lot_size
    p.on_tick(p.entry_price + 1.1 * p.risk_unit)
    info = a.live_risk(p, now, force=True)
    assert info["action"] and "booked 1 of 2 lots" in info["action"]
    assert p.filled_qty == p.spec.lot_size and len(p.booked) == 1 and p.stop >= p.entry_price
    assert not a.live_risk(p, now, force=True)["action"]  # only once
    p.request_exit("manual_close")
    assert p.state == "POSITION_CLOSED" and p.filled_qty == 2 * p.spec.lot_size
    assert p.net == pytest.approx(p.gross - p.charges) and p.gross > 0


def test_live_risk_giveback_and_sharp_move_and_throttle():
    a, p, now = _open_trade(lots=1)
    p.risk.lock_rs = 0  # the per-lot profit lock is tested on its own
    p.on_tick(p.entry_price + 1.3 * p.risk_unit)
    p.on_tick(p.entry_price + 0.3 * p.risk_unit)  # handed back most of it
    info = a.live_risk(p, now, force=True)
    assert "handed back" in info["action"] and p.exit_reason == "ai_giveback"
    b, q, now = _open_trade(lots=1)
    import app.indicators as ind_
    atr = float(ind_.atr(b.history, 14).iloc[-1])
    b.levels = {}
    t0 = now - timedelta(seconds=130)
    b._spot_trail = [(t0, 24010.0), (now, 24010.0 - 1.0 * atr)]
    b.last_spot = 24010.0 - 1.0 * atr
    import app.autotrade as at
    vw = 24010.0 - 1.0 * atr + 50  # index below VWAP -> wrong side for a CE
    orig = at.ind.session_vwap
    at.ind.session_vwap = lambda df: pd.Series([vw] * len(df), index=df.index)
    try:
        q.on_tick(q.entry_price - 0.5 * q.risk_unit)
        info = b.live_risk(q, now, force=True)
    finally:
        at.ind.session_vwap = orig
    assert info["velocity"] <= -0.8 and info["wrong_vwap"] and q.exit_reason == "ai_sharp_move"
    assert info["danger"] >= 70
    c, r, now = _open_trade(lots=1)
    r.on_tick(r.entry_price + 0.1)
    assert c.live_risk(r, now, force=True) is not None
    assert c.live_risk(r, now) is None  # throttled to every 10 s
    assert r.stats["risk_live"]["at_risk"] > 0


def test_live_partial_exit_cuts_sl_qty_then_books():
    K = FakeKite()
    p = ManagedPosition(spec(lots=2), RISK, "live", lambda: K)
    p.open(100.0)
    p.on_order_update({"order_id": p.entry_order_id, "status": "COMPLETE", "filled_quantity": p.spec.qty, "average_price": 100.0})
    full = p.filled_qty
    p.on_tick(110.0)
    assert p.book_partial(full // 2, "scale_out") and p.partial_order_id
    mods = [m for m in K.modified if m.get("quantity")]
    assert mods and mods[-1]["quantity"] == full - full // 2
    p.on_order_update({"order_id": p.partial_order_id, "status": "COMPLETE", "filled_quantity": full // 2, "average_price": 110.0})
    assert p.filled_qty == full - full // 2 and p.booked and p.partial_order_id is None


def test_missed_trend_rejoins_on_pullback(monkeypatch):
    import app.analysis as an
    a, reg = _ready(ChainKite(), stretch_mode="pullback", trend_reentry=True, join_window_minutes=15)
    a.history = _hist_trend_down()
    last = a.history["date"].iloc[-1].to_pydatetime()
    since = last - timedelta(minutes=60)  # move started an hour ago: too late to join
    monkeypatch.setattr(an, "current_signal", lambda *x, **k: {"position": "SHORT", "since": since.isoformat(), "action": "HOLD SHORT"})
    ok, why = a.trend_intact(-1)
    assert ok, why
    now = last + timedelta(minutes=5, seconds=2)
    a.try_late_join(now.replace(hour=9, minute=50))
    assert a.pending_pullback and a.pending_pullback["reentry_of"] == since and not reg
    assert any("Trend re-entry armed" in e["msg"] for e in a.timeline)
    # retest of the 20-EMA that fails -> joins
    e20 = a.stretch_check(-1)["ema20"]
    c = {"date": last + timedelta(minutes=5), "open": e20 - 4, "high": e20 + 2, "low": e20 - 10, "close": e20 - 9, "volume": 1}
    a.history = pd.concat([a.history, pd.DataFrame([c])], ignore_index=True)
    a.last_spot = e20 - 9
    assert a.try_pullback(c, now + timedelta(minutes=5))
    assert reg and reg[0].stats["entry_type"] == "pullback"
    assert any("Trend re-entry" in w["label"] for w in a.signals[-1]["why"])
    # at most 2 tries per move, and nothing when the trend is broken / setting off
    b, _ = _ready(ChainKite(), trend_reentry=True, stretch_mode="pullback")
    b.history = _hist_trend_down()
    b._arm_trend_pullback(-1, since, now); b.pending_pullback = None
    b._arm_trend_pullback(-1, since, now); b.pending_pullback = None
    b._arm_trend_pullback(-1, since, now)
    assert b.pending_pullback is None
    assert not b.trend_intact(1)[0]
    c2, _ = _ready(ChainKite(), trend_reentry=False, stretch_mode="pullback")
    c2.history = _hist_trend_down()
    c2._arm_trend_pullback(-1, since, now)
    assert c2.pending_pullback is None


def test_trend_score_matches_the_six_checks_and_trades_both_ways():
    from app.autotrade import confirmations
    from app.strategies import REGISTRY, TrendScore
    down = _hist_trend_down(n=160)
    up_df = down.copy()
    for col in ("open", "high", "low", "close"):
        up_df[col] = 2 * 24500 - down[col]
    up_df["high"], up_df["low"] = up_df[["high", "low"]].max(axis=1), up_df[["high", "low"]].min(axis=1)
    for df, d in ((down, -1), (up_df, 1)):
        u, dn = TrendScore.scores(df)
        assert int(u.iloc[-1]) == sum(c["ok"] for c in confirmations(df, 1))
        assert int(dn.iloc[-1]) == sum(c["ok"] for c in confirmations(df, -1))
        sig = REGISTRY["trend_score"].signals(df, {"min_agree": 5, "max_against": 1, "exit_level": 3})
        assert sig["entry_short" if d < 0 else "entry_long"].any() and not sig["entry_long" if d < 0 else "entry_short"].any()
    with pytest.raises(ValueError):
        REGISTRY["trend_score"].signals(down, {"min_agree": 4, "exit_level": 4})
    from app.analysis import param_grid
    assert param_grid("trend_score") and all(g["exit_level"] < g["min_agree"] for g in param_grid("trend_score"))


def test_indicators_only_mode_tests_only_trend_score(monkeypatch):
    import app.autotrade as at
    seen = []

    def finder(rows, base, strategies=None, **k):
        seen.append(tuple(strategies or ()))
        return {"ranking": [], "test_period": ["a", "b"], "combinations_tested": 0}

    monkeypatch.setattr(at, "find_strategies", finder, raising=False)
    import app.analysis as an
    monkeypatch.setattr(an, "find_strategies", finder)
    a, _, _ = make_auto(FakeKite(), autopilot=True, entry_source="indicators", max_daily_loss=6000.0)
    a.run_async = False
    a.start_day(datetime(2026, 10, 28, 9, 0))
    assert seen and all(s == ("trend_score",) for s in seen)
    seen.clear()
    b, _, _ = make_auto(FakeKite(), autopilot=True, entry_source="strategy", max_daily_loss=6000.0)
    b.run_async = False
    b.start_day(datetime(2026, 10, 28, 9, 0))
    assert seen and "trend_score" in seen[0] and len(seen[0]) > 1
    with pytest.raises(ValueError):
        AutoConfig(entry_source="both").validate()


def test_paper_capital_follows_closed_pnl():
    a, _, _ = make_auto(FakeKite(), autopilot=True, auto_money=True, paper_capital=200000.0)
    now = datetime(2026, 10, 28, 9, 30)
    a.pnl_days = {}
    assert a.apply_auto_money(now)["capital"] == 200000
    a.pnl_days = {"2026-10-26": 30000.0, "2026-10-27": -5000.0}
    m = a.apply_auto_money(now)
    assert m["capital"] == 225000 and "+ ₹25,000" in m["source"] and m["risk_per_trade"] == round(225000 * m["risk_pct"] / 100)
    a.pnl_days = {"2026-10-27": -50000.0}
    assert a.apply_auto_money(now)["capital"] == 150000


def test_app_settings_auto_mode_runs_its_own_strategies():
    """Settings → Paper/Live + Auto/Manual: the app creates and starts its own Autopilot + Swing; the user never does."""
    from app.options_manager import OptionsManager

    class KS:
        kite = FakeKite()

        def instruments(self):
            return INST

        def historical(self, *a, **k):
            return _prior_days()

    class Hub:
        def __getattr__(self, name):
            return lambda *a, **k: None
    with tempfile.TemporaryDirectory() as d:
        st = Store(os.path.join(d, "s.db"))
        om = OptionsManager(KS(), st, Hub())
        now = datetime(2026, 10, 28, 9, 20)
        s = om.app_settings()
        assert s["money"] == "paper" and s["auto"] is False and not om.autos and not om.swings
        other = om.create_auto(om._managed_auto_cfg("paper", om._app_raw()))  # an older Autopilot of the user's
        other.enable(now)
        s = om.set_app_settings({"auto": True, "capital": 250000}, now=now)
        a, w = om.autos[s["auto_id"]], om.swings[s["swing_id"]]
        assert a.id != other.id and not other.enabled and a.cfg.trend_override_min == 5  # only ONE Autopilot runs
        assert a.enabled and a.cfg.autopilot and a.cfg.mode == "paper" and a.cfg.paper_capital == 250000
        assert w.enabled and w.cfg.mode == "paper" and w.cfg.index == "BANKNIFTY" and w.cfg.capital == 250000
        assert s["intraday_on"] and s["swing_on"] and not s["needs_resume"]
        # turning it on again re-uses the same strategies (no duplicates)
        om.set_app_settings({"auto": True}, now=now)
        assert len(om.autos) == 2 and len(om.swings) == 1 and not other.enabled
        other.enabled = True  # saved as ON before a restart → it must not come back next to the app's own Autopilot
        om._save_auto(other)
        om_r = OptionsManager(KS(), st, Hub())
        assert not om_r.autos[other.id].resume_pending and om_r.autos[a.id].resume_pending
        # only the swing: intraday stops, swing keeps running
        s = om.set_app_settings({"intraday": False}, now=now)
        assert not a.enabled and w.enabled and not s["intraday_on"]
        # Manual: everything stops (manual trading still works)
        s = om.set_app_settings({"auto": False}, now=now)
        assert not w.enabled and not s["swing_on"]
        # live needs the real-money confirmation (and the server flag)
        try:
            om.set_app_settings({"money": "live", "auto": True}, now=now)
            assert False, "live without confirmation must be refused"
        except PermissionError:
            pass
        assert om.app_settings()["money"] == "paper"
        # live + Manual is fine without confirmation; paper strategies stay off
        s = om.set_app_settings({"money": "live"}, now=now)
        assert s["money"] == "live" and s["auto_id"] is None
        # saved across a restart
        assert OptionsManager(KS(), st, Hub()).app_settings()["money"] == "live"
        # emergency stop switches the app back to Manual
        om.set_app_settings({"money": "paper", "auto": True, "intraday": True}, now=now)
        om.kill()
        assert om.app_settings()["auto"] is False
        try:
            om.set_app_settings({"capital": 5}, now=now)
            assert False
        except ValueError:
            pass


def test_app_settings_background_answers_at_once():
    """The app's request returns immediately; the strategies start in a thread."""
    import time as _t
    from app.options_manager import OptionsManager

    class KS:
        kite = FakeKite()

        def instruments(self):
            return INST

        def historical(self, *a, **k):
            return _prior_days()

    class Hub:
        def __getattr__(self, name):
            return lambda *a, **k: None
    with tempfile.TemporaryDirectory() as d:
        om = OptionsManager(KS(), Store(os.path.join(d, "s.db")), Hub())
        s = om.set_app_settings({"auto": True}, now=datetime(2026, 10, 28, 9, 20), background=True)
        assert s["auto"] is True
        for _ in range(100):
            s = om.app_settings()
            if not s["starting"] and s["intraday_on"] and s["swing_on"]:
                break
            _t.sleep(0.05)
        assert s["intraday_on"] and s["swing_on"] and not s["starting"]


def test_autopilot_uses_cheaper_strike_when_one_lot_is_too_big():
    """Second-signal case: 1 ATM lot is above the money limits → try 1-2 strikes OTM (same index stop, fewer ₹)."""
    a, reg, _ = make_auto(FakeKite(), autopilot=True, strategy_source="ai", auto_money=True, paper_capital=100000,
                          risk={"sl_mode": "points", "sl_value": 30.0, "target_mode": "rr", "target_value": 2.0,
                                "trail_mode": "points", "trail_value": 30.0, "min_sl_pct": 1.0, "max_sl_pct": 90.0})
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a._vix_cache = (__import__("time").monotonic(), 14.0)
    a.enabled, a.last_spot = True, 24010.0
    a.pick = {"strategy": "sma_cross", "name": "SMA", "params": {}, "day": "2026-10-28", "atr": 5.0, "luck": {"passed": False},
              "test": {"net_pnl": 1000, "trades": 20, "win_rate_pct": 55}, "train": {"total_return_pct": 1}}
    a.cfg.strike_method = "ATM"
    first, _, _ = a._build_signal(1, None, datetime(2026, 10, 28, 9, 40, 1))
    assert first["risk_problems"] and a._only_size(first["risk_problems"])
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    s = a.signals[-1]
    assert reg, s["risk_problems"]
    assert s["strike"] > first["strike"] and s["est_max_loss"] < first["est_max_loss"]
    assert any("out of the money" in w["label"] for w in s["why"])


def test_profit_lock_per_lot():
    """1 lot up ₹1,000 → stop moves to keep ₹500 per lot; falling back there sells (paper)."""
    from app.risk import trail_stop as ts
    cfg = RiskConfig(sl_mode="points", sl_value=30, target_mode="none")
    assert ts(cfg, 1, 100.0, 70.0, 30.0, 115.0, 0.05, 65) == 70.0  # up ₹975 per lot: not yet
    lk = ts(cfg, 1, 100.0, 70.0, 30.0, 116.0, 0.05, 65)
    assert abs(lk - (100 + 500 / 65)) < 0.05
    assert ts(RiskConfig(lock_rs=0), 1, 100.0, 70.0, 30.0, 200.0, 0.05, 65) == 70.0  # off
    assert ts(cfg, 1, 100.0, 70.0, 30.0, 200.0) == 70.0  # backtests (no lot size) unchanged
    p = ManagedPosition(spec(), RiskConfig(sl_mode="points", sl_value=30, target_mode="none", min_sl_pct=1), "paper")
    p.open(100.0)
    lot, e = p.spec.lot_size, p.entry_price
    p.on_tick(e + 1000 / lot + 1)
    assert p.stats.get("profit_locked") and abs(p.stop - (e + 500 / lot)) < 0.05
    p.on_tick(e + 400 / lot)
    assert not p.is_open and p.exit_reason == "profit_lock"
    assert p.gross > 0


def test_profit_lock_steps_up():
    from app.risk import locked_rs
    c = RiskConfig()
    assert [locked_rs(c, x) for x in (999, 1000, 1499, 1500, 2000, 2240, 3100)] == [0, 500, 500, 1000, 1500, 1500, 2500]
    p = ManagedPosition(spec(), RiskConfig(sl_mode="points", sl_value=30, target_mode="none", min_sl_pct=1), "paper")
    p.open(100.0)
    lot, e = p.spec.lot_size, p.entry_price
    p.on_tick(e + 2050 / lot)
    assert p.stats["locked_rs"] == 1500 and abs(p.stop - (e + 1500 / lot)) < 0.05
    p.on_tick(e + 1400 / lot)
    assert not p.is_open and p.exit_reason == "profit_lock"


def test_stop_and_target_point_caps_buy_more_lots():
    """A 112-pt stop / 224-pt target (BANKNIFTY example) is capped to 50 / 80 pts; the risk money buys more lots."""
    wide = {"sl_mode": "points", "sl_value": 80.0, "target_mode": "rr", "target_value": 2.0,
            "trail_mode": "none", "min_sl_pct": 1.0, "max_sl_pct": 90.0}
    a0, _, _ = make_auto(FakeKite(), risk=wide, lots=10, max_daily_loss=60000.0, max_capital=10_000_000.0)
    a1, _, _ = make_auto(FakeKite(), risk=wide, lots=10, max_daily_loss=60000.0, max_capital=10_000_000.0,
                         max_stop_pts=50, max_target_pts=80)
    for a in (a0, a1):
        a.start_day(datetime(2026, 10, 28, 9, 0))
        a.enabled, a.last_spot = True, 24010.0
    s0, _, _ = a0._build_signal(1, None, datetime(2026, 10, 28, 9, 40, 1))
    s1, _, _ = a1._build_signal(1, None, datetime(2026, 10, 28, 9, 40, 1))
    e1 = s1["entry_price"]
    assert abs(e1 - s1["stop_loss"]) <= 50.05 and abs(s1["target_price"] - e1) <= 80.05
    assert abs(s0["entry_price"] - s0["stop_loss"]) > 70 and s1["lots"] >= s0["lots"]
    assert any("more lots instead" in w["label"] for w in s1["why"])
