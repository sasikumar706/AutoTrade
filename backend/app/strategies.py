"""Strategy library.

Every strategy turns a candle DataFrame (columns: date, open, high, low, close, volume)
into four boolean signal columns evaluated at the CLOSE of each bar:

    entry_long, exit_long, entry_short, exit_short

The engines (backtest, paper, live) only act on a signal at the NEXT bar's open / next
tick, so no strategy can peek into the future. Indicators are causal (see indicators.py).

To add your own strategy: subclass Strategy, set `key`, `name`, `params`, implement
`compute`, and add it to REGISTRY at the bottom.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import indicators as ind


@dataclass
class Param:
    key: str
    label: str
    default: float
    min: float | None = None
    max: float | None = None
    step: float = 1
    kind: str = "int"  # "int" | "float"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Strategy:
    key: str = ""
    name: str = ""
    description: str = ""
    params: list[Param] = field(default_factory=list)
    intraday_only: bool = False

    def resolve(self, given: dict[str, Any] | None) -> dict[str, Any]:
        given = given or {}
        out: dict[str, Any] = {}
        for p in self.params:
            v = given.get(p.key, p.default)
            v = int(v) if p.kind == "int" else float(v)
            if p.min is not None and v < p.min:
                raise ValueError(f"{p.label} must be >= {p.min}")
            if p.max is not None and v > p.max:
                raise ValueError(f"{p.label} must be <= {p.max}")
            out[p.key] = v
        return out

    def warmup(self, p: dict[str, Any]) -> int:
        """Bars needed before signals are meaningful (used to seed live sessions)."""
        nums = [int(v) for v in p.values() if isinstance(v, (int, float))]
        return max(nums + [20]) * 3

    def compute(self, df: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def signals(self, df: pd.DataFrame, params: dict[str, Any] | None = None) -> pd.DataFrame:
        p = self.resolve(params)
        # optional near-miss allowance (in %) for strategies that enter on a price level;
        # crossover strategies ignore it
        p["_tol"] = max(0.0, float((params or {}).get("level_tol_pct", 0) or 0)) / 100
        sig = self.compute(df, p)
        for col in ("entry_long", "exit_long", "entry_short", "exit_short"):
            if col not in sig:
                sig[col] = False
            sig[col] = sig[col].fillna(False).astype(bool)
        return sig[["entry_long", "exit_long", "entry_short", "exit_short"]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "intraday_only": self.intraday_only,
            "params": [p.to_dict() for p in self.params],
        }


class SmaCross(Strategy):
    def __init__(self):
        super().__init__(
            key="sma_cross",
            name="Moving Average Crossover",
            description="Buy when the fast EMA crosses above the slow EMA, sell/short on the cross below.",
            params=[
                Param("fast", "Fast EMA period", 9, 2, 200),
                Param("slow", "Slow EMA period", 21, 3, 400),
            ],
        )

    def compute(self, df, p):
        if p["fast"] >= p["slow"]:
            raise ValueError("Fast period must be smaller than slow period")
        f, s = ind.ema(df["close"], p["fast"]), ind.ema(df["close"], p["slow"])
        up, dn = ind.crossed_above(f, s), ind.crossed_below(f, s)
        return pd.DataFrame({"entry_long": up, "exit_long": dn, "entry_short": dn, "exit_short": up})


class RsiReversion(Strategy):
    def __init__(self):
        super().__init__(
            key="rsi_reversion",
            name="RSI Mean Reversion",
            description="Buy when RSI climbs back above the oversold level; exit when RSI reaches the exit level. Mirror for shorts.",
            params=[
                Param("period", "RSI period", 14, 2, 100),
                Param("oversold", "Oversold level", 30, 5, 50),
                Param("overbought", "Overbought level", 70, 50, 95),
                Param("exit_level", "Exit level", 50, 30, 70),
            ],
        )

    def compute(self, df, p):
        r = ind.rsi(df["close"], p["period"])
        return pd.DataFrame(
            {
                "entry_long": ind.crossed_above(r, p["oversold"]),
                "exit_long": r >= p["exit_level"],
                "entry_short": ind.crossed_below(r, p["overbought"]),
                "exit_short": r <= p["exit_level"],
            }
        )


class Supertrend(Strategy):
    def __init__(self):
        super().__init__(
            key="supertrend",
            name="Supertrend",
            description="Go long when Supertrend flips up, short when it flips down.",
            params=[
                Param("period", "ATR period", 10, 2, 100),
                Param("multiplier", "ATR multiplier", 3.0, 0.5, 10, 0.5, "float"),
            ],
        )

    def compute(self, df, p):
        d = ind.supertrend(df, p["period"], p["multiplier"])
        prev = d.shift(1)
        up = (d == 1) & (prev == -1)
        dn = (d == -1) & (prev == 1)
        return pd.DataFrame({"entry_long": up, "exit_long": dn, "entry_short": dn, "exit_short": up})


class OpeningRangeBreakout(Strategy):
    def __init__(self):
        super().__init__(
            key="orb",
            name="Opening Range Breakout",
            description="Intraday: mark the high/low of the first N minutes (from 09:15). Buy a close above the range high, short a close below the range low (Auto Trade: or within the entry tolerance of it). One trade per day per side.",
            params=[Param("range_minutes", "Opening range (minutes)", 15, 5, 120)],
            intraday_only=True,
        )

    def warmup(self, p):
        return 400

    def compute(self, df, p):
        ts = pd.to_datetime(df["date"])
        day = ts.dt.date
        minutes = ts.dt.hour * 60 + ts.dt.minute - (9 * 60 + 15)
        in_range = minutes < p["range_minutes"]
        rh = df["high"].where(in_range).groupby(day).cummax()
        rl = df["low"].where(in_range).groupby(day).cummin()
        rh = rh.groupby(day).ffill()
        rl = rl.groupby(day).ffill()
        after = ~in_range
        # tolerance: count a close within tol% of the range high/low as a breakout, but never more
        # than half the range, so a candle can't qualify as both an up and a down breakout
        tol = (rh * p.get("_tol", 0.0)).clip(upper=(rh - rl) / 2)
        brk_up = after & (df["close"] > rh - tol)
        brk_dn = after & (df["close"] < rl + tol)
        # first breakout of the day only
        first_up = brk_up & (brk_up.astype(int).groupby(day).cumsum() == 1)
        first_dn = brk_dn & (brk_dn.astype(int).groupby(day).cumsum() == 1)
        return pd.DataFrame(
            {"entry_long": first_up, "exit_long": first_dn, "entry_short": first_dn, "exit_short": first_up}
        )


class BollingerReversion(Strategy):
    def __init__(self):
        super().__init__(
            key="bollinger",
            name="Bollinger Band Reversion",
            description="Buy a close back inside the lower band, short a close back inside the upper band, exit at the middle band.",
            params=[
                Param("period", "Band period", 20, 5, 200),
                Param("stddev", "Std deviations", 2.0, 0.5, 4, 0.1, "float"),
            ],
        )

    def compute(self, df, p):
        lo, mid, hi = ind.bollinger(df["close"], p["period"], p["stddev"])
        c = df["close"]
        return pd.DataFrame(
            {
                "entry_long": ind.crossed_above(c, lo),
                "exit_long": c >= mid,
                "entry_short": ind.crossed_below(c, hi),
                "exit_short": c <= mid,
            }
        )


class MacdCross(Strategy):
    def __init__(self):
        super().__init__(
            key="macd",
            name="MACD Crossover",
            description="Buy when the MACD line crosses above its signal line, sell/short on the cross below. Optional ADX filter trades only when a trend is present.",
            params=[
                Param("fast", "Fast EMA", 12, 2, 100),
                Param("slow", "Slow EMA", 26, 3, 200),
                Param("signal", "Signal EMA", 9, 2, 50),
                Param("adx_min", "Min ADX (0 = off)", 0, 0, 60),
            ],
        )

    def compute(self, df, p):
        if p["fast"] >= p["slow"]:
            raise ValueError("Fast period must be smaller than slow period")
        line, sig, _ = ind.macd(df["close"], p["fast"], p["slow"], p["signal"])
        up, dn = ind.crossed_above(line, sig), ind.crossed_below(line, sig)
        if p["adx_min"] > 0:
            a, _, _ = ind.adx(df, 14)
            ok = a >= p["adx_min"]
            return pd.DataFrame({"entry_long": up & ok, "exit_long": dn, "entry_short": dn & ok, "exit_short": up})
        return pd.DataFrame({"entry_long": up, "exit_long": dn, "entry_short": dn, "exit_short": up})


class MorningMomentum(Strategy):
    """Index-options morning setup (spec example strategy).

    Bullish (-> BUY CE): EMA20 > EMA50, close > EMA20, RSI in [rsi_min, rsi_max],
    close above the session VWAP/TWAP and above the previous day's pivot (or its high
    when strict=1). Bearish (-> BUY PE) mirrored. Exit when price closes back through EMA20.
    Never trades on the previous day's move alone: every condition uses today's candles.
    """

    def __init__(self):
        super().__init__(
            key="morning_momentum",
            name="Morning Momentum (EMA+RSI+VWAP+Pivot)",
            description="Trend (EMA 20/50) + momentum (RSI) + price confirmation (VWAP and previous-day pivot breakout). Built for index options in the 9:30–10:30 window.",
            params=[
                Param("ema_fast", "Fast EMA", 20, 3, 100),
                Param("ema_slow", "Slow EMA", 50, 5, 300),
                Param("rsi_min", "RSI min (bull)", 55, 40, 80),
                Param("rsi_max", "RSI max (bull)", 75, 55, 95),
                Param("strict", "Strict breakout (1 = prev-day high/low)", 0, 0, 1),
            ],
            intraday_only=True,
        )

    def warmup(self, p):
        return 400

    def compute(self, df, p):
        from .options import daily_levels, pivots

        if p["ema_fast"] >= p["ema_slow"]:
            raise ValueError("Fast EMA must be smaller than slow EMA")
        c = df["close"]
        ef, es = ind.ema(c, p["ema_fast"]), ind.ema(c, p["ema_slow"])
        r = ind.rsi(c, 14)
        vw = ind.session_vwap(df)
        lv = daily_levels(df)
        day = pd.to_datetime(df["date"]).dt.date
        ph = day.map(lv["prev_high"])
        pl = day.map(lv["prev_low"])
        pc = day.map(lv["prev_close"])
        pv = (ph + pl + pc) / 3
        up_level = ph if p["strict"] else pv
        dn_level = pl if p["strict"] else pv
        t = p.get("_tol", 0.0)  # near-miss allowance on the VWAP and pivot/high-low levels
        bull = (ef > es) & (c > ef) & r.between(p["rsi_min"], p["rsi_max"]) & (c > vw * (1 - t)) & (c > up_level * (1 - t))
        bear = (ef < es) & (c < ef) & r.between(100 - p["rsi_max"], 100 - p["rsi_min"]) & (c < vw * (1 + t)) & (c < dn_level * (1 + t))
        bull = bull.fillna(False)
        bear = bear.fillna(False)
        # fire on the first candle the full condition becomes true
        up = bull & ~bull.shift(1, fill_value=False)
        dn = bear & ~bear.shift(1, fill_value=False)
        return pd.DataFrame({
            "entry_long": up, "exit_long": c < ef,
            "entry_short": dn, "exit_short": c > ef,
        })


class TrendPullback(Strategy):
    """Join a trend that is already running, on a dip — not at the top.

    Up-trend: fast EMA above slow EMA and ADX at least adx_min. Pullback: within the last
    `lookback` candles the low came back to the fast EMA or the day's VWAP (within touch_pct).
    Entry (→ buy CE): the trend still holds and this candle closes above the previous candle's
    high and above the fast EMA — the dip has bounced. Down-trend mirrored (→ buy PE).
    Exit: close back through the slow EMA, or the trend flips. No entries before 09:30.
    """

    def __init__(self):
        super().__init__(
            key="trend_pullback",
            name="Trend Pullback (join a running trend)",
            description="In a running up-trend (EMA fast above slow, ADX strong) wait for a dip back to the EMA or VWAP, then buy CE when "
                        "the next candle closes above the previous high. Down-trend mirrored → buy PE. Exit when price closes back "
                        "through the slow EMA or the trend flips.",
            params=[
                Param("ema_fast", "Fast EMA", 20, 5, 50),
                Param("ema_slow", "Slow EMA", 50, 20, 200),
                Param("adx_min", "Min trend strength (ADX)", 20, 0, 50),
                Param("lookback", "Dip within last N candles", 3, 1, 10),
                Param("touch_pct", "Touch distance %", 0.05, 0, 0.5, 0.01, "float"),
            ],
            intraday_only=True,
        )

    def warmup(self, p):
        return 300

    def compute(self, df, p):
        if p["ema_fast"] >= p["ema_slow"]:
            raise ValueError("Fast EMA must be smaller than slow EMA")
        c, hi, lo = df["close"], df["high"], df["low"]
        ef, es = ind.ema(c, p["ema_fast"]), ind.ema(c, p["ema_slow"])
        adx, _, _ = ind.adx(df, 14)
        vw = ind.session_vwap(df)
        t = p["touch_pct"] / 100
        n = int(p["lookback"])
        ts = pd.to_datetime(df["date"])
        after_open = (ts.dt.hour * 60 + ts.dt.minute) >= 9 * 60 + 30
        strong = adx >= p["adx_min"]
        up = (ef > es) & strong
        dn = (ef < es) & strong
        # dip: low reached the higher of EMA-fast / VWAP (up-trend); rally: high reached the lower one (down-trend)
        dip = (lo <= pd.concat([ef, vw], axis=1).max(axis=1) * (1 + t)).astype(int).rolling(n, min_periods=1).max() > 0
        rally = (hi >= pd.concat([ef, vw], axis=1).min(axis=1) * (1 - t)).astype(int).rolling(n, min_periods=1).max() > 0
        bounce = (c > hi.shift(1)) & (c > ef)
        fail = (c < lo.shift(1)) & (c < ef)
        long_ = (up & dip & bounce & after_open).fillna(False)
        short_ = (dn & rally & fail & after_open).fillna(False)
        return pd.DataFrame({
            "entry_long": long_ & ~long_.shift(1, fill_value=False),
            "exit_long": (c < es) | (ef < es),
            "entry_short": short_ & ~short_.shift(1, fill_value=False),
            "exit_short": (c > es) | (ef > es),
        })


def _first_only(cond: pd.Series, day) -> pd.Series:
    """True only on the first True candle of each day."""
    c = cond.fillna(False).astype(bool)
    return c & (c.astype(int).groupby(day).cumsum() == 1)


class VwapBounce(Strategy):
    """VWAP bounce — the classic intraday institutional level.

    Up-trend day (price has held above VWAP for `hold` candles): when a candle's low dips to VWAP
    (within touch %) and it closes back above VWAP as a green candle → buy CE. Mirror below VWAP → PE.
    Exit: a close back through VWAP. No entries before 09:30.
    """

    def __init__(self):
        super().__init__(
            key="vwap_bounce", name="VWAP Bounce",
            description="Price has stayed above today's VWAP; it dips to VWAP and bounces (green close back above) → buy CE. "
                        "Below VWAP and rejected from it → buy PE. Exit on a close back through VWAP.",
            params=[Param("hold", "Candles held on one side first", 4, 2, 12),
                    Param("touch_pct", "Touch distance %", 0.05, 0, 0.3, 0.01, "float")],
            intraday_only=True,
        )

    def warmup(self, p):
        return 120

    def compute(self, df, p):
        c, o, hi, lo = df["close"], df["open"], df["high"], df["low"]
        # indices have no traded volume, so their "VWAP" is the day's average price (TWAP); use that everywhere
        vw = ind.session_vwap(df.assign(volume=0))
        ts = pd.to_datetime(df["date"])
        ok_time = (ts.dt.hour * 60 + ts.dt.minute) >= 9 * 60 + 30
        n, t = int(p["hold"]), p["touch_pct"] / 100
        above = (c > vw).astype(int).rolling(n).min().shift(1) == 1
        below = (c < vw).astype(int).rolling(n).min().shift(1) == 1
        long_ = above & (lo <= vw * (1 + t)) & (c > vw) & (c > o) & ok_time
        short_ = below & (hi >= vw * (1 - t)) & (c < vw) & (c < o) & ok_time
        return pd.DataFrame({"entry_long": long_.fillna(False), "exit_long": c < vw,
                             "entry_short": short_.fillna(False), "exit_short": c > vw})


class CprBreakout(Strategy):
    """Central Pivot Range breakout (popular with Indian index traders).

    From yesterday's high/low/close: pivot P, bottom BC = (H+L)/2, top TC = 2P − BC. A narrow CPR
    (width below `max_width_pct` of price) tends to precede trending days. First close above the top
    of the CPR → buy CE; first close below the bottom → buy PE. Exit on a close back inside the CPR.
    """

    def __init__(self):
        super().__init__(
            key="cpr_breakout", name="CPR Breakout",
            description="Uses yesterday's Central Pivot Range. On narrow-CPR days, the first close above the CPR top → buy CE; "
                        "first close below the CPR bottom → buy PE. Exit on a close back inside the CPR.",
            params=[Param("max_width_pct", "Max CPR width %", 0.25, 0.05, 1.0, 0.05, "float")],
            intraday_only=True,
        )

    def warmup(self, p):
        return 200

    def compute(self, df, p):
        from .options import daily_levels
        c = df["close"]
        lv = daily_levels(df)
        day = pd.to_datetime(df["date"]).dt.date
        ph, pl, pc = day.map(lv["prev_high"]), day.map(lv["prev_low"]), day.map(lv["prev_close"])
        piv = (ph + pl + pc) / 3
        bc = (ph + pl) / 2
        tc = 2 * piv - bc
        top, bot = pd.concat([tc, bc], axis=1).max(axis=1), pd.concat([tc, bc], axis=1).min(axis=1)
        narrow = ((top - bot) / piv * 100) <= p["max_width_pct"]
        ts = pd.to_datetime(df["date"])
        ok_time = (ts.dt.hour * 60 + ts.dt.minute) >= 9 * 60 + 20
        up = _first_only(narrow & ok_time & (c > top), day)
        dn = _first_only(narrow & ok_time & (c < bot), day)
        inside = (c <= top) & (c >= bot)
        return pd.DataFrame({"entry_long": up, "exit_long": inside | (c < bot),
                             "entry_short": dn, "exit_short": inside | (c > top)})


class GapAndGo(Strategy):
    """Gap-and-go: a gap that holds after the open tends to continue.

    If today opens more than `gap_pct` above yesterday's close and, after the first `wait` minutes, a
    candle closes above the high of those first minutes → buy CE. Gap down mirrored → PE. One trade a
    day. Exit if price closes back below today's open (gap failing).
    """

    def __init__(self):
        super().__init__(
            key="gap_and_go", name="Gap and Go",
            description="Gap up of at least the set % that holds: after the first minutes, a close above their high → buy CE. "
                        "Gap down mirrored → buy PE. Exit if price closes back through today's open.",
            params=[Param("gap_pct", "Min gap %", 0.3, 0.1, 2.0, 0.1, "float"),
                    Param("wait", "Wait first N minutes", 15, 5, 60)],
            intraday_only=True,
        )

    def warmup(self, p):
        return 200

    def compute(self, df, p):
        from .options import daily_levels
        c = df["close"]
        ts = pd.to_datetime(df["date"])
        day = ts.dt.date
        lv = daily_levels(df)
        pc = day.map(lv["prev_close"])
        dopen = df["open"].groupby(day).transform("first")
        gap = (dopen - pc) / pc * 100
        mins = ts.dt.hour * 60 + ts.dt.minute - (9 * 60 + 15)
        first = mins < p["wait"]
        fh = df["high"].where(first).groupby(day).transform("max")
        fl = df["low"].where(first).groupby(day).transform("min")
        after = ~first
        up = _first_only(after & (gap >= p["gap_pct"]) & (c > fh), day)
        dn = _first_only(after & (gap <= -p["gap_pct"]) & (c < fl), day)
        return pd.DataFrame({"entry_long": up, "exit_long": c < dopen, "entry_short": dn, "exit_short": c > dopen})


class TrendDay(Strategy):
    """Trend day: on a strong one-way day, buy the pullbacks.

    A day qualifies once the index has moved at least `move_pct` from yesterday's close, holds above (below)
    today's VWAP and trend strength ADX(14) is at least `adx_min`, after the first `wait` minutes. Entry: the
    first green (red) candle after a dip to the fast EMA (9 or 20) — up to 2 entries a day. Exit only when price closes back
    through VWAP (the trend has broken), so a big trend day is ridden with the trailing stop.
    """

    def __init__(self):
        super().__init__(
            key="trend_day", name="Trend Day",
            description="Strong one-way day (big move from yesterday's close, above VWAP, ADX high): buy the dip to the fast EMA "
                        "on the first green candle → CE (down-day mirrored → PE). Up to 2 entries a day. Exit when price "
                        "closes back through VWAP.",
            params=[Param("move_pct", "Min move from yesterday %", 0.8, 0.3, 3.0, 0.1, "float"),
                    Param("adx_min", "Min trend strength (ADX)", 25, 10, 50),
                    Param("wait", "Wait first N minutes", 30, 15, 90),
                    Param("ema", "Pullback to EMA", 9, 5, 50),
                    Param("lookback", "Dip within last N candles", 3, 1, 10),
                    Param("touch_pct", "Touch distance %", 0.05, 0, 0.5, 0.01, "float")],
            intraday_only=True,
        )

    def warmup(self, p):
        return 300

    def compute(self, df, p):
        from .options import daily_levels
        c, o, hi, lo = df["close"], df["open"], df["high"], df["low"]
        ts = pd.to_datetime(df["date"])
        day = ts.dt.date
        pc = day.map(daily_levels(df)["prev_close"])
        move = (c - pc) / pc * 100
        ef = ind.ema(c, int(p["ema"]))
        vw = ind.session_vwap(df)
        adx, _, _ = ind.adx(df, 14)
        mins = ts.dt.hour * 60 + ts.dt.minute - (9 * 60 + 15)
        ready = (mins >= p["wait"]) & (adx >= p["adx_min"])
        up_day = ready & (move >= p["move_pct"]) & (c > vw)
        dn_day = ready & (move <= -p["move_pct"]) & (c < vw)
        t, n = p["touch_pct"] / 100, int(p["lookback"])
        dipped = (lo <= ef * (1 + t)).astype(int).rolling(n, min_periods=1).max().astype(bool)
        rallied = (hi >= ef * (1 - t)).astype(int).rolling(n, min_periods=1).max().astype(bool)
        up = (up_day & dipped & (c > o) & (c > ef)).fillna(False)
        dn = (dn_day & rallied & (c < o) & (c < ef)).fillna(False)
        up, dn = up & ~up.shift(1, fill_value=False), dn & ~dn.shift(1, fill_value=False)  # one entry per bounce
        up = up & (up.astype(int).groupby(day).cumsum() <= 2)
        dn = dn & (dn.astype(int).groupby(day).cumsum() <= 2)
        return pd.DataFrame({"entry_long": up, "exit_long": c < vw, "entry_short": dn, "exit_short": c > vw})


class TrendScore(Strategy):
    """Indicators only: the same 6 checks the app shows (EMA20 vs EMA50, RSI vs 50, price vs VWAP, MACD histogram,
    ADX >= 20 with the leading DI, price vs yesterday's pivot). Buy CE when at least `min_agree` say up and at most
    `max_against` say down; buy PE the other way. Exit when support fades to `exit_level` or fewer, or it flips."""

    def __init__(self):
        super().__init__(
            key="trend_score", name="Trend Score (indicators only)",
            description="Trades only when the 6 trend indicators agree: EMA20/50, RSI, VWAP, MACD, ADX and yesterday's pivot. "
                        "Enters when enough of them line up one way and few point the other; exits when that agreement fades.",
            params=[Param("min_agree", "Checks that must agree", 5, 3, 6),
                    Param("max_against", "Checks allowed against", 1, 0, 2),
                    Param("exit_level", "Exit when agreement falls to", 3, 1, 4)],
            intraday_only=True,
        )

    def warmup(self, p):
        return 200

    @staticmethod
    def scores(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        from .options import daily_levels
        c = df["close"]
        e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
        r = ind.rsi(c, 14)
        vw = ind.session_vwap(df)
        _, _, hist = ind.macd(c)
        a, pdi, mdi = ind.adx(df, 14)
        lv = daily_levels(df)
        day = pd.to_datetime(df["date"]).dt.date
        piv = (day.map(lv["prev_high"]) + day.map(lv["prev_low"]) + day.map(lv["prev_close"])) / 3
        piv = pd.Series(piv.to_numpy(), index=df.index)
        strong = a >= 20
        up = ((e20 > e50).astype(int) + (r > 50).astype(int) + (c > vw).astype(int) + (hist > 0).astype(int)
              + (strong & (pdi > mdi)).astype(int) + (c > piv).fillna(False).astype(int))
        dn = ((e20 < e50).astype(int) + (r < 50).astype(int) + (c < vw).astype(int) + (hist < 0).astype(int)
              + (strong & (mdi > pdi)).astype(int) + (c < piv).fillna(False).astype(int))
        ready = e50.notna() & a.notna()
        return up.where(ready, 0), dn.where(ready, 0)

    def compute(self, df, p):
        up, dn = self.scores(df)
        if p["exit_level"] >= p["min_agree"]:
            raise ValueError("Exit level must be below the checks that must agree")
        long_ok = (up >= p["min_agree"]) & (dn <= p["max_against"])
        short_ok = (dn >= p["min_agree"]) & (up <= p["max_against"])
        prev_l, prev_s = long_ok.shift(1, fill_value=False), short_ok.shift(1, fill_value=False)
        return pd.DataFrame({"entry_long": long_ok & ~prev_l, "exit_long": (up <= p["exit_level"]) | short_ok,
                             "entry_short": short_ok & ~prev_s, "exit_short": (dn <= p["exit_level"]) | long_ok})


REGISTRY: dict[str, Strategy] = {
    s.key: s for s in (SmaCross(), RsiReversion(), Supertrend(), OpeningRangeBreakout(), BollingerReversion(), MacdCross(), MorningMomentum(), TrendPullback(),
                  VwapBounce(), CprBreakout(), GapAndGo(), TrendDay(), TrendScore())
}


def _member_states(sig: pd.DataFrame, days) -> tuple[pd.Series, pd.Series]:
    """Replay one strategy's own long/short state (intraday: flat at each new day)."""
    n = len(sig)
    el, xl, es, xs = (sig[k].to_numpy() for k in ("entry_long", "exit_long", "entry_short", "exit_short"))
    long_, short_ = [False] * n, [False] * n
    pos = 0
    for i in range(n):
        if days is not None and i and days[i] != days[i - 1]:
            pos = 0
        if pos > 0 and (xl[i] or es[i]):
            pos = 0
        if pos < 0 and (xs[i] or el[i]):
            pos = 0
        if el[i]:
            pos = 1
        elif es[i]:
            pos = -1
        long_[i], short_[i] = pos > 0, pos < 0
    return pd.Series(long_, index=sig.index), pd.Series(short_, index=sig.index)


class ComboStrategy(Strategy):
    """Runs 2-3 strategies together on one index and candle size.

    Entry: ANY member's buy (→ CE) or sell (→ PE) signal. The position is held while at least one
    member is still in that direction; it exits when every member has left it or any member signals
    the opposite way. params = {"members": [{"strategy": key, "params": {...}}, ...]}.
    """

    def __init__(self):
        super().__init__(key="combo", name="Combo", description="Several proven strategies together: enter on any one's signal, "
                         "hold while at least one still agrees, exit when none do or one signals the opposite way.",
                         params=[], intraday_only=False)

    @staticmethod
    def members(params: dict | None) -> list[dict]:
        ms = (params or {}).get("members") or []
        if not 2 <= len(ms) <= 4:
            raise ValueError("A combo needs 2-4 member strategies")
        for m in ms:
            if m.get("strategy") in ("combo", None) or m["strategy"] not in REGISTRY:
                raise ValueError(f"Unknown combo member {m.get('strategy')}")
        return ms

    def resolve(self, given):
        ms = self.members(given)
        for m in ms:
            REGISTRY[m["strategy"]].resolve(m.get("params"))
        return {"members": ms}

    def warmup(self, p):
        return max(REGISTRY[m["strategy"]].warmup(REGISTRY[m["strategy"]].resolve(m.get("params"))) for m in self.members(p))

    def member_signals(self, df, params) -> list[tuple[str, pd.DataFrame]]:
        extra = {k: v for k, v in (params or {}).items() if k != "members"}  # e.g. level_tol_pct
        return [(m["strategy"], REGISTRY[m["strategy"]].signals(df, {**(m.get("params") or {}), **extra}))
                for m in self.members(params)]

    def signals(self, df, params=None):
        sigs = self.member_signals(df, params)
        dates = pd.to_datetime(df["date"])
        intraday = bool((dates.dt.normalize() != dates).any())
        days = dates.dt.date.to_numpy() if intraday else None
        any_el = pd.concat([s["entry_long"] for _, s in sigs], axis=1).any(axis=1)
        any_es = pd.concat([s["entry_short"] for _, s in sigs], axis=1).any(axis=1)
        states = [_member_states(s, days) for _, s in sigs]
        any_long = pd.concat([lg for lg, _ in states], axis=1).any(axis=1)
        any_short = pd.concat([sh for _, sh in states], axis=1).any(axis=1)
        out = pd.DataFrame({
            "entry_long": any_el & ~any_es,
            "entry_short": any_es & ~any_el,
            "exit_long": ~any_long | any_es,
            "exit_short": ~any_short | any_el,
        })
        return out.fillna(False).astype(bool)

    def fired(self, df, params, i: int = -1) -> list[str]:
        """Names of the members whose entry rule fired on candle i."""
        out = []
        for key, s in self.member_signals(df, params):
            if s["entry_long"].iloc[i] or s["entry_short"].iloc[i]:
                out.append(REGISTRY[key].name)
        return out

    def title(self, params) -> str:
        return " + ".join(REGISTRY[m["strategy"]].name.split(" (")[0] for m in self.members(params))


COMBO = ComboStrategy()


def get_strategy(key: str) -> Strategy:
    if key == "combo":
        return COMBO
    if key not in REGISTRY:
        raise ValueError(f"Unknown strategy '{key}'. Available: {', '.join(REGISTRY)}")
    return REGISTRY[key]
