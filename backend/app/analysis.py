"""AI Strategy Finder.

1. technical_snapshot(): reads the latest candle's indicators and gives each a vote
   (+1 bullish, -1 bearish, 0 neutral) with a plain-English reason -> overall verdict.
2. find_strategies(): tries every strategy with a grid of parameters. Parameters are
   chosen on the OLDER part of the data (in-sample) and then judged on the NEWER part
   they never saw (out-of-sample). Ranking uses out-of-sample results only, which
   guards against settings that merely fit the past.
3. current_signal(): what a strategy says on the latest candle (BUY / SELL / HOLD / EXIT).

Nothing here predicts the future; it summarises evidence from past prices.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind
from .backtest import BacktestConfig, prepare_candles, run_backtest
from .strategies import REGISTRY, get_strategy

# Parameter grids searched by the finder (kept small so it runs on a phone).
GRIDS: dict[str, dict[str, list]] = {
    "trend_score": {"min_agree": [4, 5, 6], "max_against": [0, 1], "exit_level": [2, 3]},
    "sma_cross": {"fast": [5, 9, 13, 20], "slow": [21, 34, 50]},
    "rsi_reversion": {"period": [7, 14], "oversold": [25, 30, 35], "overbought": [65, 70, 75], "exit_level": [50]},
    "supertrend": {"period": [7, 10, 14], "multiplier": [2.0, 3.0, 4.0]},
    "orb": {"range_minutes": [15, 30, 45, 60]},
    "bollinger": {"period": [14, 20, 30], "stddev": [1.5, 2.0, 2.5]},
    "macd": {"fast": [8, 12], "slow": [21, 26], "signal": [9], "adx_min": [0, 20]},
    "vwap_bounce": {"hold": [3, 5, 8], "touch_pct": [0.03, 0.08]},
    "cpr_breakout": {"max_width_pct": [0.15, 0.25, 0.4]},
    "gap_and_go": {"gap_pct": [0.2, 0.4, 0.7], "wait": [15, 30]},
    "trend_day": {"move_pct": [0.6, 0.9, 1.2], "adx_min": [20, 25], "wait": [30], "ema": [9, 20], "lookback": [3], "touch_pct": [0.05]},
    "trend_pullback": {"ema_fast": [9, 20], "ema_slow": [50], "adx_min": [15, 20, 25], "lookback": [3, 5], "touch_pct": [0.05]},
    "morning_momentum": {"ema_fast": [9, 20], "ema_slow": [50], "rsi_min": [52, 55, 60], "rsi_max": [75], "strict": [0, 1]},
}


def _valid(strategy: str, combo: dict) -> bool:
    if "fast" in combo and "slow" in combo and combo["fast"] >= combo["slow"]:
        return False
    if strategy == "rsi_reversion" and combo["oversold"] >= combo["overbought"]:
        return False
    if strategy == "trend_score" and combo["exit_level"] >= combo["min_agree"]:
        return False
    return True


def param_grid(strategy: str) -> list[dict]:
    grid = GRIDS.get(strategy)
    if not grid:
        return [{p.key: p.default for p in REGISTRY[strategy].params}]
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]
    return [c for c in combos if _valid(strategy, c)]


def to_python(o):
    """Convert numpy scalars inside dicts/lists to plain Python (JSON-safe)."""
    if isinstance(o, dict):
        return {k: to_python(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_python(v) for v in o]
    if isinstance(o, np.generic):
        v = o.item()
        return None if isinstance(v, float) and math.isnan(v) else v
    return o


# ------------------------------------------------------------------ technical snapshot


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if math.isnan(v) else round(v, 2)
    except (TypeError, ValueError):
        return None


def technical_snapshot(candles) -> dict:
    df = prepare_candles(candles)
    if len(df) < 60:
        raise ValueError("Need at least 60 candles for the technical snapshot")
    c = df["close"]
    last = float(c.iloc[-1])
    signals: list[dict] = []

    def vote(name: str, value, v: int, reason: str, weight: float = 1.0):
        signals.append({"indicator": name, "value": value, "vote": v, "weight": weight, "reason": reason})

    e20, e50 = ind.ema(c, 20).iloc[-1], ind.ema(c, 50).iloc[-1]
    e200 = ind.ema(c, 200).iloc[-1] if len(df) >= 200 else np.nan
    if not np.isnan(e200):
        vote("EMA 200", _f(e200), 1 if last > e200 else -1,
             f"Price is {'above' if last > e200 else 'below'} the 200 EMA (long-term trend {'up' if last > e200 else 'down'})", 1.5)
    vote("EMA 20/50", f"{_f(e20)} / {_f(e50)}", 1 if e20 > e50 else -1,
         f"20 EMA is {'above' if e20 > e50 else 'below'} 50 EMA (medium trend {'up' if e20 > e50 else 'down'})", 1.5)

    r = ind.rsi(c, 14).iloc[-1]
    if r >= 70:
        vote("RSI 14", _f(r), -1, "RSI above 70: overbought, pullback risk")
    elif r <= 30:
        vote("RSI 14", _f(r), 1, "RSI below 30: oversold, bounce possible")
    elif r >= 55:
        vote("RSI 14", _f(r), 1, "RSI 55-70: bullish momentum")
    elif r <= 45:
        vote("RSI 14", _f(r), -1, "RSI 30-45: bearish momentum")
    else:
        vote("RSI 14", _f(r), 0, "RSI near 50: no momentum edge")

    line, sig, hist = ind.macd(c)
    h_now, h_prev = hist.iloc[-1], hist.iloc[-2]
    if h_now > 0:
        vote("MACD", _f(h_now), 1, "MACD above signal line" + (" and rising" if h_now > h_prev else " but weakening"))
    else:
        vote("MACD", _f(h_now), -1, "MACD below signal line" + (" and falling" if h_now < h_prev else " but recovering"))

    a, pdi, mdi = ind.adx(df, 14)
    a_now = a.iloc[-1]
    trending = a_now >= 25
    vote("ADX 14", _f(a_now), (1 if pdi.iloc[-1] > mdi.iloc[-1] else -1) if trending else 0,
         (f"ADX {a_now:.0f}: strong trend, {'buyers (+DI)' if pdi.iloc[-1] > mdi.iloc[-1] else 'sellers (-DI)'} in control")
         if trending else f"ADX {a_now:.0f}: weak/no trend (range-bound market)", 1.0)

    st = ind.supertrend(df, 10, 3).iloc[-1]
    vote("Supertrend 10,3", "UP" if st == 1 else "DOWN", 1 if st == 1 else -1,
         f"Supertrend is in {'up' if st == 1 else 'down'}trend mode", 1.5)

    lo, mid, hi = ind.bollinger(c, 20, 2)
    pb = (last - lo.iloc[-1]) / (hi.iloc[-1] - lo.iloc[-1]) if hi.iloc[-1] > lo.iloc[-1] else 0.5
    if pb > 1:
        vote("Bollinger %B", _f(pb), -1 if not trending else 1, "Closed above upper band: " + ("strong breakout" if trending else "stretched, may revert"))
    elif pb < 0:
        vote("Bollinger %B", _f(pb), 1 if not trending else -1, "Closed below lower band: " + ("strong breakdown" if trending else "stretched, may revert"))
    else:
        vote("Bollinger %B", _f(pb), 0, f"Inside the bands ({pb:.0%} of the way from lower to upper)", 0.5)

    hh, ll = df["high"].tail(20).max(), df["low"].tail(20).min()
    pos = (last - ll) / (hh - ll) if hh > ll else 0.5
    vote("20-bar range", f"{_f(ll)} – {_f(hh)}", 1 if pos > 0.8 else -1 if pos < 0.2 else 0,
         f"Price is in the {'top' if pos > 0.8 else 'bottom' if pos < 0.2 else 'middle'} of its 20-candle range", 0.5)

    vol = df["volume"]
    if vol.tail(20).sum() > 0:
        vr = vol.iloc[-1] / max(vol.tail(20).mean(), 1e-9)
        day_move = last - float(df["open"].iloc[-1])
        vote("Volume", f"{vr:.1f}x avg", (1 if day_move > 0 else -1) if vr > 1.5 else 0,
             f"Volume {vr:.1f}x its 20-candle average" + (f", confirming the {'up' if day_move > 0 else 'down'} move" if vr > 1.5 else " (no confirmation)"), 0.5)

    atr_pct = float(ind.atr(df, 14).iloc[-1] / last * 100)
    total_w = sum(s["weight"] for s in signals)
    score = sum(s["vote"] * s["weight"] for s in signals) / total_w  # -1 .. +1
    verdict = "BULLISH" if score >= 0.25 else "BEARISH" if score <= -0.25 else "NEUTRAL"
    return to_python({
        "as_of": df["date"].iloc[-1].isoformat(),
        "close": round(last, 2),
        "verdict": verdict,
        "score": round(score * 100),  # -100 .. +100
        "regime": "TRENDING" if trending else "RANGING",
        "volatility_atr_pct": round(atr_pct, 2),
        "signals": signals,
        "suggested_style": "trend-following (Supertrend, EMA cross, MACD with ADX filter)" if trending
        else "mean-reversion (RSI, Bollinger) — trend signals whipsaw in ranges",
    })


# ------------------------------------------------------------------ strategy finder


def _score(m: dict) -> float:
    """Return-to-risk score with a penalty for too few trades."""
    if m["trades"] < 3:
        return -1e9
    ret = m["total_return_pct"]
    dd = abs(m["max_drawdown_pct"]) or 0.5
    pf = m["profit_factor"] if m["profit_factor"] is not None else (3.0 if ret > 0 else 0.0)
    return ret / dd + 0.5 * min(pf, 3.0) + 0.1 * math.log(m["trades"])


def current_signal(df: pd.DataFrame, strategy: str, params: dict, allow_short: bool, daily_reset: bool = True) -> dict:
    """What the strategy says on the latest candle, and the position it would be in."""
    sig = get_strategy(strategy).signals(df, params)
    pos, since = 0, None
    dates = pd.to_datetime(df["date"])
    intraday = bool((dates.dt.normalize() != dates).any())
    days = dates.dt.date.to_numpy()
    el, es = sig["entry_long"].to_numpy(dtype=bool), sig["entry_short"].to_numpy(dtype=bool)
    xl, xs = sig["exit_long"].to_numpy(dtype=bool), sig["exit_short"].to_numpy(dtype=bool)
    for i in range(len(sig)):  # replay signal-state only (ignores SL/target; intraday squares off daily)
        if daily_reset and intraday and i and days[i] != days[i - 1]:
            pos, since = 0, None  # intraday positions never carry overnight
        if pos > 0 and xl[i]:
            pos = 0
        if pos < 0 and xs[i]:
            pos = 0
        if el[i] and pos <= 0:
            pos, since = 1, i
        elif es[i] and allow_short and pos >= 0:
            pos, since = -1, i
    last = sig.iloc[-1]
    if last["entry_long"]:
        action = "BUY"
    elif last["entry_short"] and allow_short:
        action = "SELL"
    elif (pos == 0) and (last["exit_long"] or last["exit_short"]):
        action = "EXIT"
    else:
        action = "HOLD LONG" if pos > 0 else "HOLD SHORT" if pos < 0 else "WAIT"
    return {
        "action": action,
        "position": "LONG" if pos > 0 else "SHORT" if pos < 0 else "FLAT",
        "since": df["date"].iloc[since].isoformat() if since is not None and pos != 0 else None,
    }


def find_strategies(candles, base: BacktestConfig, test_fraction: float = 0.3, top: int = 8,
                    strategies: list[str] | None = None, extra_params: dict | None = None,
                    progress=None, grids: dict | None = None) -> dict:
    """extra_params (e.g. {"level_tol_pct": 0.3}) are applied to every test run but not
    reported in each row's params."""
    extra = extra_params or {}
    df = prepare_candles(candles)
    if len(df) < 200:
        raise ValueError("Need at least 200 candles for the strategy finder; widen the date range")
    intraday = (df["date"].dt.normalize() != df["date"]).any()
    # split on a day boundary so intraday sessions aren't cut in half
    cut_idx = int(len(df) * (1 - test_fraction))
    cut_day = df["date"].iloc[cut_idx].normalize()
    split = int((df["date"] < cut_day).sum()) if intraday else cut_idx
    split = min(max(split, 100), len(df) - 60)
    train, test = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)
    # warm-up: the test run gets the tail of training data for indicators, but only
    # trades after the split are counted
    warm = df.iloc[max(0, split - 300):].reset_index(drop=True)
    test_start = test["date"].iloc[0]

    names = strategies or list(REGISTRY)
    results, tried = [], 0
    for n_i, name in enumerate(names):
        strat = get_strategy(name)
        if progress:
            progress(n_i + 1, len(names), strat.name)
        if strat.intraday_only and not intraday:
            continue
        scored = []
        for combo in (grids or {}).get(name) or param_grid(name):
            cfg = replace(base, strategy=name, params={**combo, **extra},
                          min_adx=0.0 if name in ("rsi_reversion", "bollinger") else base.min_adx)
            try:
                r = run_backtest(train, cfg)
            except ValueError:
                continue
            tried += 1
            scored.append((_score(r["metrics"]), combo, r["metrics"]))
        scored.sort(key=lambda x: x[0], reverse=True)
        for s_train, combo, m_train in scored[:2]:  # best 2 settings per strategy go to the test
            cfg = replace(base, strategy=name, params={**combo, **extra},
                          min_adx=0.0 if name in ("rsi_reversion", "bollinger") else base.min_adx)
            r = run_backtest(warm, cfg)
            tr = [t for t in r["trades"] if pd.Timestamp(t["entry_time"]) >= test_start]
            net = sum(t["net_pnl"] for t in tr)
            wins = [t for t in tr if t["net_pnl"] > 0]
            gw, gl = sum(t["net_pnl"] for t in wins), -sum(t["net_pnl"] for t in tr if t["net_pnl"] <= 0)
            eq = base.initial_capital + np.cumsum([0.0] + [t["net_pnl"] for t in tr])
            dd = float(((eq / np.maximum.accumulate(eq)) - 1).min() * 100)
            # how far the price actually moved per trade (index points), for the up/down forecast
            moves = [(t["exit_price"] - t["entry_price"]) * (1 if t["side"] == "LONG" else -1) for t in tr]
            wm, lm = [m for m in moves if m > 0], [-m for m in moves if m <= 0]
            m_pts = {
                "avg_win_pts": round(float(np.mean(wm)), 1) if wm else None,
                "avg_loss_pts": round(float(np.mean(lm)), 1) if lm else None,
                "avg_bars": round(float(np.mean([t["bars_held"] for t in tr])), 1) if tr else None,
            }
            m_test = {
                "trades": len(tr),
                "net_pnl": round(net, 2),
                "total_return_pct": round(net / base.initial_capital * 100, 2),
                "win_rate_pct": round(len(wins) / len(tr) * 100, 1) if tr else 0.0,
                "profit_factor": round(gw / gl, 2) if gl > 0 else None,
                "max_drawdown_pct": round(dd, 2),
                # trade-level t-statistic: mean P&L per trade / its standard error (for the luck check)
                "t_stat": (round(float(np.mean(pn) / (np.std(pn, ddof=1) / np.sqrt(len(pn)))), 2)
                           if len(pn := [t["net_pnl"] for t in tr]) >= 3 and np.std(pn, ddof=1) > 0 else None),
            }
            results.append({
                "strategy": name,
                "name": strat.name,
                "params": combo,
                "train": {k: m_train[k] for k in ("trades", "total_return_pct", "win_rate_pct", "profit_factor", "max_drawdown_pct")},
                "test": m_test,
                "moves": m_pts,
                "score": round(_score(m_test), 3) if m_test["trades"] >= 3 else None,
                "robust": m_test["trades"] >= 3 and m_test["net_pnl"] > 0 and m_train["total_return_pct"] > 0,
                "now": current_signal(df, name, {**combo, **extra}, base.allow_short),
            })
    results.sort(key=lambda x: (x["score"] is not None, x["score"] or -1e9), reverse=True)
    return to_python({
        "train_period": [train["date"].iloc[0].isoformat(), train["date"].iloc[-1].isoformat()],
        "test_period": [test["date"].iloc[0].isoformat(), test["date"].iloc[-1].isoformat()],
        "combinations_tested": tried,
        "buy_and_hold_test_pct": round(float(test["close"].iloc[-1] / test["close"].iloc[0] - 1) * 100, 2),
        "ranking": results[:top],
    })


# ------------------------------------------------------------------ explanation


def rule_based_summary(symbol: str, snap: dict, finder: dict | None) -> str:
    """Plain-English summary without any external AI service."""
    bull = [s["reason"] for s in snap["signals"] if s["vote"] > 0]
    bear = [s["reason"] for s in snap["signals"] if s["vote"] < 0]
    lines = [
        f"{symbol} looks {snap['verdict'].lower()} on this timeframe (score {snap['score']:+d} on a -100…+100 scale). "
        f"The market is {snap['regime'].lower()}, so {snap['suggested_style']} usually suits it better.",
    ]
    if bull:
        lines.append("Bullish evidence: " + "; ".join(bull[:3]) + ".")
    if bear:
        lines.append("Bearish evidence: " + "; ".join(bear[:3]) + ".")
    if finder and finder["ranking"]:
        best = [r for r in finder["ranking"] if r["robust"]]
        if best:
            b = best[0]
            lines.append(
                f"Best strategy on unseen recent data: {b['name']} {b['params']} — "
                f"{b['test']['total_return_pct']:+.2f}% over {b['test']['trades']} trades "
                f"(buy & hold {finder['buy_and_hold_test_pct']:+.2f}%). Right now it says {b['now']['action']}."
            )
        else:
            lines.append("No strategy was profitable on both the older and the newer data — the safest choice is to not trade this setup.")
    lines.append("This is analysis of past prices, not a prediction or advice. Paper-trade before risking money.")
    return " ".join(lines)


def forecast(ranking: list[dict], snap: dict, interval: str) -> dict:
    """Plain up/down call with expected points, from the best proven strategy's CURRENT signal and how far
    the price moved in its unseen-data trades. Information, not a promise."""
    mins = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}.get(interval)
    pool = [r for r in ranking if r.get("robust")] or []
    live = [r for r in pool if r["now"]["action"] in ("BUY", "SELL", "HOLD LONG", "HOLD SHORT")]
    close = snap.get("close")
    base = {"close": close, "interval": interval}
    if not pool:
        return {**base, "direction": "NONE", "headline": "No forecast — no strategy made money on unseen data for this stock/index",
                "strategy": None}
    if not live:
        b = pool[0]
        return {**base, "direction": "NONE", "strategy": b["name"],
                "headline": f"No clear direction now — {b['name']} (best proven) is waiting for its next signal"}
    b = live[0]
    up = b["now"]["action"] in ("BUY", "HOLD LONG")
    mv = b.get("moves") or {}
    win, loss = mv.get("avg_win_pts"), mv.get("avg_loss_pts")
    wr = b["test"]["win_rate_pct"]
    fresh = b["now"]["action"] in ("BUY", "SELL")
    sgn = 1 if up else -1
    out = {
        **base, "direction": "UP" if up else "DOWN", "strategy": b["name"], "strategy_key": b["strategy"], "params": b["params"],
        "signal": b["now"]["action"], "fresh": fresh, "since": b["now"]["since"],
        "expected_pts": win, "risk_pts": loss, "win_rate_pct": wr, "trades": b["test"]["trades"],
        "target_price": round(close + sgn * win, 2) if close and win else None,
        "stop_price": round(close - sgn * loss, 2) if close and loss else None,
        "hold_minutes": round(mv["avg_bars"] * mins) if mins and mv.get("avg_bars") is not None else None,
        "edge_pts": round(wr / 100 * (win or 0) - (1 - wr / 100) * (loss or 0), 1) if win is not None and loss is not None else None,
        "agree_with_indicators": (snap.get("verdict") == ("BULLISH" if up else "BEARISH")),
    }
    pts = f"about {win:.0f} pts" if win else "by an unknown amount"
    out["headline"] = (f"Expected {'UP' if up else 'DOWN'} {pts} "
                       f"(based on {b['name']}, {'signal now' if fresh else 'signal still active since ' + (b['now']['since'] or '')[11:16]})")
    return out
