"""Market page: major index overview and a detail view for any stock or index.

Pure helpers (quote_summary, history_stats, plain_trend) are kept free of Kite calls so they
can be unit-tested; market_overview / market_detail wire them to the Kite service.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .analysis import technical_snapshot

# (exchange, tradingsymbol, display name) — shown when present in today's instrument list
MAJOR_INDICES = [
    ("NSE", "NIFTY 50", "Nifty 50"),
    ("NSE", "NIFTY BANK", "Bank Nifty"),
    ("BSE", "SENSEX", "Sensex"),
    ("NSE", "NIFTY FIN SERVICE", "Fin Nifty"),
    ("NSE", "NIFTY MIDCAP 100", "Midcap 100"),
    ("NSE", "NIFTY IT", "Nifty IT"),
    ("NSE", "NIFTY AUTO", "Nifty Auto"),
    ("NSE", "NIFTY PHARMA", "Nifty Pharma"),
    ("NSE", "INDIA VIX", "India VIX"),
]
FLAT_PCT = 0.15  # moves within +/-0.15% are shown as flat


def _num(x):
    try:
        return None if x is None or pd.isna(x) else float(x)
    except (TypeError, ValueError):
        return None


def quote_summary(q: dict) -> dict:
    """Today's price action from a Kite quote: change in points and % vs previous close."""
    ohlc = q.get("ohlc") or {}
    last, prev = _num(q.get("last_price")), _num(ohlc.get("close"))
    chg = round(last - prev, 2) if last is not None and prev else None
    pct = round(chg / prev * 100, 2) if chg is not None else None
    depth = q.get("depth") or {}
    bid = ((depth.get("buy") or [{}])[0]).get("price") or None
    ask = ((depth.get("sell") or [{}])[0]).get("price") or None
    hi, lo = _num(ohlc.get("high")), _num(ohlc.get("low"))
    return {
        "last": last, "prev_close": prev, "change": chg, "change_pct": pct,
        "direction": "FLAT" if pct is None or abs(pct) <= FLAT_PCT else ("UP" if pct > 0 else "DOWN"),
        "open": _num(ohlc.get("open")), "high": hi, "low": lo,
        # where the price sits in today's range, 0 = at the low, 100 = at the high
        "range_pos": round((last - lo) / (hi - lo) * 100) if last is not None and hi and lo and hi > lo else None,
        "volume": q.get("volume") or q.get("volume_traded") or None,
        "avg_price": _num(q.get("average_price")) or None,
        "upper_circuit": _num(q.get("upper_circuit_limit")) or None,
        "lower_circuit": _num(q.get("lower_circuit_limit")) or None,
        "bid": bid, "ask": ask, "oi": q.get("oi") or None,
        "last_trade_time": str(q.get("last_trade_time") or q.get("timestamp") or "") or None,
    }


def history_stats(daily: list[dict], last: float | None) -> dict:
    """52-week high/low and period returns from daily candles (newest last)."""
    if not daily:
        return {}
    df = pd.DataFrame(daily)
    closes = df["close"].astype(float).tolist()
    cur = last if last is not None else closes[-1]
    yr = df.tail(250)
    hi52, lo52 = float(yr["high"].max()), float(yr["low"].min())
    out = {"high_52w": round(hi52, 2), "low_52w": round(lo52, 2),
           "from_52w_high_pct": round((cur - hi52) / hi52 * 100, 2) if hi52 else None,
           "returns": {}}
    for label, n in (("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126), ("1Y", 250)):
        if len(closes) > n:
            base = closes[-1 - n]
            out["returns"][label] = round((cur - base) / base * 100, 2) if base else None
    return out


def plain_trend(snap: dict) -> str:
    """One sentence a beginner can read."""
    v, regime = snap.get("verdict"), snap.get("regime", "")
    if v == "BULLISH":
        return f"Mostly positive: most daily indicators point up ({regime.lower()})."
    if v == "BEARISH":
        return f"Mostly negative: most daily indicators point down ({regime.lower()})."
    return f"Mixed: indicators don't agree on a direction ({regime.lower()}). Waiting is often wiser."


def market_overview(ks) -> list[dict]:
    inst = ks.instruments()
    have = set(zip(inst["exchange"], inst["tradingsymbol"]))
    rows = [(ex, ts, name) for ex, ts, name in MAJOR_INDICES if (ex, ts) in have]
    if not rows:
        return []
    quotes = ks.kite.quote([f"{ex}:{ts}" for ex, ts, _ in rows])
    return [{"exchange": ex, "tradingsymbol": ts, "name": name, **quote_summary(quotes.get(f"{ex}:{ts}") or {})}
            for ex, ts, name in rows]


def market_detail(ks, exchange: str, tradingsymbol: str) -> dict:
    inst = ks.find_instrument(exchange, tradingsymbol)
    key = f"{inst['exchange']}:{inst['tradingsymbol']}"
    q = quote_summary(ks.kite.quote([key]).get(key) or {})
    token = int(inst["instrument_token"])
    now = datetime.now()
    daily = ks.historical(token, "day", now - timedelta(days=400), now)
    intraday = ks.historical(token, "5minute", now - timedelta(days=6), now)
    last_day = None
    if intraday:
        last_day = max(pd.Timestamp(r["date"]).date() for r in intraday)
        intraday = [r for r in intraday if pd.Timestamp(r["date"]).date() == last_day]
    snap = None
    if len(daily) >= 60:
        s = technical_snapshot(daily)
        snap = {"verdict": s["verdict"], "score": s["score"], "regime": s["regime"],
                "signals": s["signals"][:6], "plain": plain_trend(s)}
    return {
        "instrument": {k: inst.get(k) for k in ("exchange", "tradingsymbol", "name", "instrument_type", "segment", "lot_size", "expiry")},
        "quote": q,
        "stats": history_stats(daily, q["last"]),
        "intraday": {"day": last_day.isoformat() if last_day else None,
                     "times": [str(r["date"])[11:16] for r in intraday],
                     "closes": [float(r["close"]) for r in intraday]},
        "trend": snap,
    }
