"""Day Builder: watch today's market, recognise what kind of day it is, and build a strategy for the rest of it.

How it works (no look-ahead, everything from data the app already has):

1. Day profile — at a checkpoint time (e.g. 10:15) today's candles so far are measured: move from yesterday's
   close, opening gap, share of candles above VWAP, VWAP crossings, day range, trend efficiency, ADX. That gives a
   label: TREND UP / TREND DOWN / RANGE / VOLATILE / MIXED.
2. Similar days — the same measurements are taken for every past day (up to the same clock time) and the most
   similar days are found (nearest neighbours on the standardised measurements — a small statistical model).
3. Build — strategies that suit the label are tried with many settings, but scored ONLY on what happened after the
   checkpoint on those similar days, with real costs (charges, slippage, time decay).
4. Proof — the winner must make money on both the older and the newer half of the similar days, still make
   money with costs doubled, and pass a luck check (its per-trade t-score must beat what the best of that many
   no-edge tries reaches by chance). Otherwise nothing is built and the app keeps its normal behaviour.

The built strategy is then traded like any other (its own entry/exit rules), tagged "Day-built" in Stats.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, time

import numpy as np
import pandas as pd

from . import indicators as ind
from .analysis import param_grid
from .backtest import BacktestConfig, run_backtest
from .strategies import REGISTRY

FEATURES = ["move_pct", "gap_pct", "above_vwap", "vwap_crosses", "range_pct", "efficiency", "adx"]
CANDIDATES = {
    "TREND UP": ["trend_day", "trend_pullback", "supertrend", "morning_momentum", "sma_cross", "macd", "orb"],
    "TREND DOWN": ["trend_day", "trend_pullback", "supertrend", "morning_momentum", "sma_cross", "macd", "orb"],
    "RANGE": ["vwap_bounce", "bollinger", "rsi_reversion", "cpr_breakout"],
    "VOLATILE": ["supertrend", "trend_pullback", "bollinger", "vwap_bounce", "macd", "orb"],
    "MIXED": ["supertrend", "trend_pullback", "vwap_bounce", "bollinger", "macd", "sma_cross"],
}
K_SIMILAR = 15
MIN_TRADES = 5


def day_features(day_rows: pd.DataFrame, prev_close: float | None, upto: time) -> dict | None:
    """Measurements of one day's candles up to `upto` (exclusive of later candles)."""
    d = day_rows[day_rows["date"].dt.time < upto]
    if len(d) < 4 or not prev_close:
        return None
    c = d["close"].to_numpy(dtype=float)
    vw = ind.session_vwap(d).to_numpy(dtype=float)
    above = c > vw
    crosses = int(np.sum(above[1:] != above[:-1]))
    path = float(np.sum(np.abs(np.diff(np.concatenate([[float(d["open"].iloc[0])], c])))))
    try:
        adx = float(ind.adx(d, min(14, max(3, len(d) // 2)))[0].iloc[-1])
    except Exception:
        adx = float("nan")
    return {
        "move_pct": (c[-1] - prev_close) / prev_close * 100,
        "gap_pct": (float(d["open"].iloc[0]) - prev_close) / prev_close * 100,
        "above_vwap": float(above.mean()),
        "vwap_crosses": crosses,
        "range_pct": (float(d["high"].max()) - float(d["low"].min())) / prev_close * 100,
        "efficiency": abs(c[-1] - float(d["open"].iloc[0])) / path if path else 0.0,
        "adx": adx if adx == adx else 20.0,
    }


def label_day(f: dict) -> str:
    if f["move_pct"] >= 0.5 and f["above_vwap"] >= 0.7 and f["efficiency"] >= 0.3:
        return "TREND UP"
    if f["move_pct"] <= -0.5 and f["above_vwap"] <= 0.3 and f["efficiency"] >= 0.3:
        return "TREND DOWN"
    if f["vwap_crosses"] >= 4 and f["range_pct"] < 1.0:
        return "RANGE"
    if f["range_pct"] >= 1.2:
        return "VOLATILE"
    if f["efficiency"] < 0.2 and f["range_pct"] < 0.8:
        return "RANGE"
    return "MIXED"


def similar_days(history: pd.DataFrame, today: date, upto: time, k: int = K_SIMILAR) -> tuple[dict, list[dict]]:
    """Today's profile and the k most similar past days (by standardised measurements at the same clock time)."""
    df = history
    days = sorted(set(df["date"].dt.date))
    closes = {dd: float(df[df["date"].dt.date == dd]["close"].iloc[-1]) for dd in days}
    feats: list[tuple[date, dict]] = []
    prev = None
    today_f = None
    for dd in days:
        rows = df[df["date"].dt.date == dd]
        f = day_features(rows, closes.get(prev) if prev else None, upto)
        prev = dd
        if f is None:
            continue
        if dd == today:
            today_f = f
        elif dd < today:
            feats.append((dd, f))
    if today_f is None:
        raise ValueError("Not enough of today's candles yet")
    if len(feats) < 20:
        raise ValueError("Need at least 20 past days of candles")
    m = np.array([[f[k_] for k_ in FEATURES] for _, f in feats], dtype=float)
    mu, sd = m.mean(axis=0), m.std(axis=0)
    sd[sd == 0] = 1.0
    t = (np.array([today_f[k_] for k_ in FEATURES]) - mu) / sd
    dist = np.sqrt((((m - mu) / sd - t) ** 2).sum(axis=1))
    lab = label_day(today_f)
    same = np.array([label_day(f) == lab for _, f in feats])
    order = np.lexsort((dist, ~same))  # same label first, then nearest
    picked = [{"day": feats[i][0].isoformat(), "distance": round(float(dist[i]), 2), "label": label_day(feats[i][1])}
              for i in order[:k]]
    return {**{k_: round(float(v), 3) for k_, v in today_f.items()}, "label": lab}, picked


def build_for_today(history: pd.DataFrame, today: date, upto: time, base: BacktestConfig, entry_end: str = "14:45",
                    k: int = K_SIMILAR, progress=None) -> dict:
    """Pick the strategy + settings that did best AFTER `upto` on days most like today. No look-ahead."""
    profile, sims = similar_days(history, today, upto, k)
    sim_days = {s["day"] for s in sims}
    ordered = sorted(sim_days)
    older, newer = set(ordered[: len(ordered) // 2]), set(ordered[len(ordered) // 2:])
    past = history[history["date"].dt.date < today]
    t0, t1 = upto.strftime("%H:%M"), entry_end
    results = []
    cands = CANDIDATES.get(profile["label"], CANDIDATES["MIXED"])
    for n, key in enumerate(cands, 1):
        if progress:
            progress(f"Day Builder: testing {REGISTRY[key].name} ({n}/{len(cands)}) on {len(sims)} similar days")
        for params in param_grid(key):
            try:
                r = run_backtest(past, replace(base, strategy=key, params=params))
            except Exception:
                continue
            tr = [x for x in r["trades"] if x["entry_time"][:10] in sim_days and t0 <= x["entry_time"][11:16] < t1]
            if len(tr) < MIN_TRADES:
                continue
            nets = [x["net_pnl"] for x in tr]
            half = lambda s: sum(x["net_pnl"] for x in tr if x["entry_time"][:10] in s)  # noqa: E731
            sdv = float(np.std(nets, ddof=1)) if len(nets) > 1 else 0.0
            tstat = float(np.mean(nets) / (sdv / np.sqrt(len(nets)))) if sdv > 0 else 0.0
            gw = sum(v for v in nets if v > 0)
            gl = -sum(v for v in nets if v <= 0)
            results.append({
                "strategy": key, "name": REGISTRY[key].name, "params": params, "trades": len(tr),
                "net": round(sum(nets), 2), "win_rate_pct": round(sum(1 for v in nets if v > 0) / len(nets) * 100, 1),
                "profit_factor": round(gw / gl, 2) if gl > 0 else None,
                "older_half": round(half(older), 2), "newer_half": round(half(newer), 2),
                "double_cost_net": round(sum(nets) - sum(x["charges"] for x in tr), 2),
                "t_stat": round(tstat, 2),
            })
    results.sort(key=lambda r: r["net"], reverse=True)
    # luck check: trying many settings on few days makes the best one look good by chance alone
    need = round(float(np.sqrt(2 * np.log(max(2, len(results))))), 2)
    ok = [r for r in results if r["net"] > 0 and r["older_half"] > 0 and r["newer_half"] > 0 and r["double_cost_net"] > 0
          and r["t_stat"] >= need]
    best = ok[0] if ok else None
    weaker = [r for r in results if r["net"] > 0 and r["older_half"] > 0 and r["newer_half"] > 0 and r["double_cost_net"] > 0]
    return {"profile": profile, "similar_days": sims, "checkpoint": t0, "tested": len(results), "luck_needed": need,
            "practice": None if best or not weaker else weaker[0],
            "best": best, "top": results[:6],
            "why": (f"{best['name']} made ₹{best['net']:,.0f} per lot after {t0} on {len(sims)} days like today "
                    f"({best['trades']} trades, win {best['win_rate_pct']}%), in both older and newer halves and with costs doubled"
                    if best else
                    ("No strategy made money after this time on days like today in both halves, with costs doubled and beyond "
                     "what luck alone gives — nothing built (that's the honest answer, not a failure)"))}
