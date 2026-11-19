"""Performance metrics for an equity curve and a list of closed trades."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def compute_metrics(equity: pd.Series, trades: list[dict], initial_capital: float) -> dict:
    out: dict = {}
    final = float(equity.iloc[-1]) if len(equity) else initial_capital
    out["initial_capital"] = round(initial_capital, 2)
    out["final_equity"] = round(final, 2)
    out["net_pnl"] = round(final - initial_capital, 2)
    out["total_return_pct"] = round((final / initial_capital - 1) * 100, 2)

    if len(equity) >= 2:
        days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400
        years = days / 365.25
        out["cagr_pct"] = (
            round(((final / initial_capital) ** (1 / years) - 1) * 100, 2)
            if years >= 0.25 and final > 0
            else None  # CAGR is misleading for periods shorter than ~3 months
        )
        peak = equity.cummax()
        dd = equity / peak - 1
        out["max_drawdown_pct"] = round(float(dd.min()) * 100, 2)
        daily = equity.resample("1D").last().dropna()
        rets = daily.pct_change().dropna()
        if len(rets) > 1 and rets.std() > 0:
            out["sharpe"] = round(float(rets.mean() / rets.std() * math.sqrt(252)), 2)
            downside = rets[rets < 0]
            out["sortino"] = (
                round(float(rets.mean() / downside.std() * math.sqrt(252)), 2)
                if len(downside) > 1 and downside.std() > 0
                else None
            )
        else:
            out["sharpe"] = None
            out["sortino"] = None
        cagr = out.get("cagr_pct")
        out["calmar"] = (
            round(cagr / abs(out["max_drawdown_pct"]), 2) if cagr is not None and out["max_drawdown_pct"] < 0 else None
        )
    else:
        out.update(cagr_pct=None, max_drawdown_pct=0.0, sharpe=None, sortino=None, calmar=None)

    pnls = np.array([t["net_pnl"] for t in trades], dtype=float)
    n = len(pnls)
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    out["trades"] = n
    out["win_rate_pct"] = round(len(wins) / n * 100, 2) if n else 0.0
    out["avg_win"] = round(float(wins.mean()), 2) if len(wins) else 0.0
    out["avg_loss"] = round(float(losses.mean()), 2) if len(losses) else 0.0
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    out["profit_factor"] = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
    out["expectancy"] = round(float(pnls.mean()), 2) if n else 0.0
    out["total_charges"] = round(sum(t["charges"] for t in trades), 2)
    out["gross_pnl"] = round(sum(t["gross_pnl"] for t in trades), 2)
    streak = best = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        best = max(best, streak)
    out["max_consecutive_losses"] = best
    return out
