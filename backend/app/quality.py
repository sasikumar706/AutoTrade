"""Measured execution quality: real option spreads from our own saved quotes, and why signals were skipped."""
from __future__ import annotations

import csv
from pathlib import Path
from statistics import median

SKIP_CATEGORIES = [
    ("lunch", ("Lunch",)), ("vix", ("VIX",)), ("chop", ("Chop filter",)), ("expiry day", ("expiry day",)),
    ("event day", ("event days", "RBI", "Fed", "Budget")), ("spread", ("Spread",)), ("confidence", ("Confidence",)),
    ("daily loss limit", ("Daily loss", "daily risk")), ("weekly/monthly limit", ("Weekly loss", "Monthly loss")),
    ("losing streak", ("in a row",)), ("too big for account", ("too big for the account", "1 lot is too big")),
    ("max capital", ("max capital",)), ("max trades", ("trades/day",)), ("stale signal", ("stale",)),
    ("margin", ("margin",)), ("AI found nothing / luck", ("AI found", "LIVE paused")), ("no price", ("No live",)),
]


def skip_category(problem: str) -> str:
    for cat, keys in SKIP_CATEGORIES:
        if any(k.lower() in problem.lower() for k in keys):
            return cat
    return "other"


def measured_spreads(data_dir, days: int = 10) -> dict:
    """Bid-ask spread of the options Auto Trade watched (data/option_quotes/*.csv), as % of the premium."""
    out = {"days": 0, "samples": 0, "median_spread_pct": None, "p90_spread_pct": None, "by_hour": {}}
    if not data_dir:
        return out
    d = Path(data_dir) / "option_quotes"
    files = sorted(d.glob("*.csv"))[-days:] if d.exists() else []
    vals, hours = [], {}
    for f in files:
        try:
            with f.open() as fh:
                for r in csv.DictReader(fh):
                    try:
                        bid, ask = float(r["bid"]), float(r["ask"])
                    except (TypeError, ValueError):
                        continue
                    if bid <= 0 or ask <= bid:
                        continue
                    pct = (ask - bid) / ((ask + bid) / 2) * 100
                    if pct > 20:
                        continue
                    vals.append(pct)
                    hours.setdefault(r["time"][:2], []).append(pct)
        except Exception:
            continue
    if not vals:
        out["days"] = len(files)
        return out
    vals.sort()
    out.update(days=len(files), samples=len(vals), median_spread_pct=round(median(vals), 2),
               p90_spread_pct=round(vals[int(len(vals) * 0.9) - 1 if len(vals) > 1 else 0], 2),
               by_hour={h: round(median(v), 2) for h, v in sorted(hours.items()) if len(v) >= 5})
    return out


def calibrated_slippage_pct(data_dir, default: float) -> tuple[float, str]:
    """Slippage per side = half the measured spread + 0.25% impact (min 0.5%), once 3+ days are saved."""
    m = measured_spreads(data_dir)
    if m["days"] >= 3 and m["samples"] >= 200 and m["median_spread_pct"] is not None:
        return round(max(0.5, m["median_spread_pct"] / 2 + 0.25), 2), f"measured from {m['days']} days of your saved quotes"
    return default, "standard estimate (not enough saved quotes yet)"
