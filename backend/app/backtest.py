"""Bar-by-bar backtest engine.

Execution rules (chosen to avoid the common ways backtests lie):
* Signals are read at a bar's CLOSE and filled at the NEXT bar's OPEN (no look-ahead).
* Slippage is applied against you on every fill (`slippage_bps`, basis points).
* Stop-loss / target are checked inside each bar using its high/low. If a bar touches
  both, the STOP is assumed to hit first (pessimistic). If the bar opens beyond the stop
  or target (a gap), the fill is at the open, not the level.
* Intraday (MIS) positions are squared off at `squareoff_time` or at the last bar of the
  day, and no new MIS entry is taken after `no_entry_after`, nor one that would fill on
  the next day.
* Every round trip pays Zerodha brokerage + statutory charges (see charges.py).
* Position size is limited by available cash (no intraday leverage is assumed).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import time
from typing import Any

import numpy as np
import pandas as pd

from .charges import round_trip_charges
from .metrics import compute_metrics
from .strategies import get_strategy


@dataclass
class BacktestConfig:
    strategy: str
    params: dict[str, Any] = field(default_factory=dict)
    initial_capital: float = 100_000.0
    sizing: str = "pct_capital"  # "pct_capital" | "fixed_qty"
    pct_capital: float = 100.0  # used when sizing == pct_capital
    quantity: int = 1  # used when sizing == fixed_qty (in units, not lots)
    lot_size: int = 1  # F&O lot size; quantity is rounded down to a multiple
    product: str = "MIS"  # MIS (intraday) | CNC (delivery) | NRML (F&O carry)
    segment: str = "equity_intraday"  # see charges.SEGMENTS
    exchange: str = "NSE"
    allow_short: bool = True
    stop_loss_pct: float | None = None
    target_pct: float | None = None
    breakeven_at_pct: float | None = None  # once price moves this % in favour, the stop moves to the entry price
    # same rules the live Auto Trade uses, so the AI test matches what really happens
    time_stop_bars: int = 0  # exit at the close if after N bars the trade isn't time_stop_min_r × stop in profit
    time_stop_min_r: float = 0.25
    no_entry_window: tuple[str, str] | None = None  # e.g. ("12:30", "13:15") lunch: no new entries
    min_adx: float = 0.0  # no new entries while ADX(14) of the traded series is below this (chop filter)
    slippage_bps: float = 5.0
    squareoff_time: str = "15:10"
    no_entry_after: str = "14:45"
    fixed_cost: float | None = None  # flat ₹ per round trip instead of Zerodha's equity charges (option proxy)
    bar_cost: float = 0.0  # ₹ charged per candle held (option time decay / theta in the option proxy)

    def validate(self):
        if self.product == "CNC" and self.allow_short:
            # Short delivery selling is not allowed for cash equity.
            self.allow_short = False
        if self.initial_capital <= 0:
            raise ValueError("Initial capital must be positive")
        if self.sizing not in ("pct_capital", "fixed_qty"):
            raise ValueError("sizing must be pct_capital or fixed_qty")
        if self.lot_size < 1:
            raise ValueError("lot_size must be >= 1")


def _parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def prepare_candles(candles: list[dict] | pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(candles).copy()
    required = {"date", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"Candles need columns {sorted(required)}")
    if "volume" not in df:
        df["volume"] = 0
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is not None:
        df["date"] = df["date"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def run_backtest(candles: list[dict] | pd.DataFrame, cfg: BacktestConfig) -> dict:
    cfg.validate()
    df = prepare_candles(candles)
    if len(df) < 3:
        raise ValueError("Not enough candles to backtest")
    strat = get_strategy(cfg.strategy)
    sig = strat.signals(df, cfg.params)

    is_intraday_data = (df["date"].dt.normalize() != df["date"]).any()
    intraday = cfg.product == "MIS"
    if intraday and not is_intraday_data:
        raise ValueError("MIS (intraday) needs an intraday interval such as 5minute")
    if strat.intraday_only and not is_intraday_data:
        raise ValueError(f"{strat.name} needs intraday candles")

    sq_t, ne_t = _parse_hm(cfg.squareoff_time), _parse_hm(cfg.no_entry_after)
    nw = (_parse_hm(cfg.no_entry_window[0]), _parse_hm(cfg.no_entry_window[1])) if cfg.no_entry_window else None
    adx_ok = None
    if cfg.min_adx:
        from . import indicators as _ind
        _a = _ind.adx(df, 14)[0].to_numpy()
        adx_ok = ~(_a < cfg.min_adx)  # NaN (warm-up) counts as OK, like live
    dates = df["date"].to_numpy()
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    times = df["date"].dt.time.to_numpy()
    days = df["date"].dt.date.to_numpy()
    e_l, x_l = sig["entry_long"].to_numpy(), sig["exit_long"].to_numpy()
    e_s, x_s = sig["entry_short"].to_numpy(), sig["exit_short"].to_numpy()
    slip = cfg.slippage_bps / 10_000

    cash = cfg.initial_capital
    pos = 0  # +qty long, -qty short
    entry_px = 0.0
    entry_time = None
    entry_reason = ""
    stop = target = None
    pending: str | None = None  # "long" | "short" | "flat"
    trades: list[dict] = []
    equity = np.empty(len(df))

    def size(price: float) -> int:
        if cfg.sizing == "fixed_qty":
            q = cfg.quantity
        else:
            q = math.floor(cash * cfg.pct_capital / 100 / price)
        q = (q // cfg.lot_size) * cfg.lot_size
        return max(q, 0)

    def open_pos(side: int, raw_px: float, i: int, reason: str):
        nonlocal pos, entry_px, entry_time, stop, target, entry_reason
        px = raw_px * (1 + slip * side)
        q = size(px)
        if q <= 0:
            return
        pos = side * q
        entry_px, entry_time, entry_reason = px, dates[i], reason
        stop = px * (1 - side * cfg.stop_loss_pct / 100) if cfg.stop_loss_pct else None
        target = px * (1 + side * cfg.target_pct / 100) if cfg.target_pct else None

    def close_pos(raw_px: float, i: int, reason: str):
        nonlocal pos, cash, stop, target
        side = 1 if pos > 0 else -1
        qty = abs(pos)
        px = raw_px * (1 - slip * side)
        buy_val = (entry_px if side > 0 else px) * qty
        sell_val = (px if side > 0 else entry_px) * qty
        gross = (px - entry_px) * qty * side
        ch = {"total": cfg.fixed_cost} if cfg.fixed_cost is not None else round_trip_charges(cfg.segment, buy_val, sell_val, cfg.exchange)
        if cfg.bar_cost:
            ch = {**ch, "total": ch["total"] + cfg.bar_cost * max(1, int(i - np.searchsorted(dates, entry_time)))}
        net = gross - ch["total"]
        cash += net
        trades.append(
            {
                "side": "LONG" if side > 0 else "SHORT",
                "qty": qty,
                "entry_time": pd.Timestamp(entry_time).isoformat(),
                "entry_price": round(entry_px, 2),
                "exit_time": pd.Timestamp(dates[i]).isoformat(),
                "exit_price": round(px, 2),
                "gross_pnl": round(gross, 2),
                "charges": round(ch["total"], 2),
                "net_pnl": round(net, 2),
                "return_pct": round(gross / (entry_px * qty) * 100, 3),
                "entry_reason": entry_reason,
                "exit_reason": reason,
                "bars_held": int(i - np.searchsorted(dates, entry_time)),
            }
        )
        pos = 0
        stop = target = None

    n = len(df)
    for i in range(n):
        # 1) execute order queued at previous close, at this bar's open
        if pending is not None:
            if pending in ("flat", "long", "short") and pos != 0:
                cur = "long" if pos > 0 else "short"
                if pending != cur:
                    close_pos(o[i], i, "signal")
            if pending in ("long", "short") and pos == 0:
                open_pos(1 if pending == "long" else -1, o[i], i, "signal")
            pending = None

        # 2a) intraday square-off at the open of the first bar at/after squareoff_time
        if intraday and pos != 0 and times[i] >= sq_t:
            close_pos(o[i], i, "squareoff")

        # 2b) stop-loss / target inside the bar (stop checked first = pessimistic)
        if pos != 0:
            if pos > 0:
                if stop is not None and l[i] <= stop:
                    close_pos(min(o[i], stop), i, "stop_loss")
                elif target is not None and h[i] >= target:
                    close_pos(max(o[i], target), i, "target")
            else:
                if stop is not None and h[i] >= stop:
                    close_pos(max(o[i], stop), i, "stop_loss")
                elif target is not None and l[i] <= target:
                    close_pos(min(o[i], target), i, "target")
        # 2c) break-even: after this bar, a trade that went far enough in favour can no longer lose (pessimistic: from next bar)
        if pos != 0 and cfg.breakeven_at_pct and stop is not None:
            if pos > 0 and h[i] >= entry_px * (1 + cfg.breakeven_at_pct / 100):
                stop = max(stop, entry_px)
            elif pos < 0 and l[i] <= entry_px * (1 - cfg.breakeven_at_pct / 100):
                stop = min(stop, entry_px)

        # 2d) time-stop (as live): a trade that hasn't moved enough after N bars is closed at this bar's close
        if pos != 0 and cfg.time_stop_bars and cfg.stop_loss_pct and entry_time is not None:
            held = i - int(np.searchsorted(dates, entry_time)) + 1
            if held >= cfg.time_stop_bars:
                side_ = 1 if pos > 0 else -1
                r_mult = (c[i] - entry_px) * side_ / (entry_px * cfg.stop_loss_pct / 100)
                if r_mult < cfg.time_stop_min_r:
                    close_pos(c[i], i, "time_stop")

        last_bar_of_day = i == n - 1 or days[i + 1] != days[i]

        # 3) intraday: never carry a position past the day's last bar
        if intraday and pos != 0 and last_bar_of_day:
            close_pos(c[i], i, "squareoff")

        # 4) read signals at the close, queue for next open
        if i < n - 1:
            want = None
            if pos > 0 and x_l[i]:
                want = "flat"
            if pos < 0 and x_s[i]:
                want = "flat"
            if e_l[i] and pos <= 0:
                want = "long"
            elif e_s[i] and cfg.allow_short and pos >= 0:
                want = "short"
            # live decides at the candle close (= next bar's start), so the lunch window is checked on that time
            if want in ("long", "short") and ((nw and nw[0] <= times[i + 1] < nw[1]) or (adx_ok is not None and not adx_ok[i])):
                want = None if pos == 0 else ("flat" if want != ("long" if pos > 0 else "short") else None)
            if want in ("long", "short") and intraday and (times[i] >= ne_t or times[i] >= sq_t or last_bar_of_day):
                want = "flat" if pos != 0 and want != ("long" if pos > 0 else "short") else None
            pending = want

        # 5) mark to market
        unreal = (c[i] - entry_px) * pos if pos != 0 else 0.0
        equity[i] = cash + unreal

    if pos != 0:
        close_pos(c[-1], len(df) - 1, "end_of_data")
        equity[-1] = cash

    eq = pd.Series(equity, index=pd.DatetimeIndex(dates))
    metrics = compute_metrics(eq, trades, cfg.initial_capital)
    metrics["bars"] = len(df)
    metrics["start"] = pd.Timestamp(dates[0]).isoformat()
    metrics["end"] = pd.Timestamp(dates[-1]).isoformat()
    bh = (c[-1] / c[0] - 1) * 100
    metrics["buy_and_hold_pct"] = round(float(bh), 2)

    step = max(1, len(eq) // 400)
    curve = [{"t": ts.isoformat(), "equity": round(float(v), 2)} for ts, v in eq.iloc[::step].items()]
    if curve[-1]["t"] != eq.index[-1].isoformat():
        curve.append({"t": eq.index[-1].isoformat(), "equity": round(float(eq.iloc[-1]), 2)})
    peak = eq.cummax()
    dd = ((eq / peak - 1) * 100).iloc[::step]
    from .analysis import to_python

    return to_python({
        "config": asdict(cfg),
        "metrics": metrics,
        "equity_curve": curve,
        "drawdown_curve": [{"t": t.isoformat(), "dd": round(float(v), 2)} for t, v in dd.items()],
        "trades": trades,
    })
