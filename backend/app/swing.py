"""Swing trend: hold a monthly in-the-money CE for weeks while the index is in an uptrend.

The rule (picked from 2008-2016 data and checked on unseen 2017-2023 data, see the app's notes):
  * once a day near the close (default 15:15) compare the 10-day and 50-day average of the index's daily closes,
    using today's live price as today's close;
  * 10-day above 50-day  -> be LONG: hold 1 strike in-the-money CE of a MONTHLY expiry with >= 20 days left;
  * 10-day below 50-day  -> be OUT (no shorting: in the test, betting on falls lost heavily);
  * the option is rolled to the next monthly expiry when 7 or fewer days are left (time decay speeds up then).
There is no target. A wide "disaster" stop (default 60% of the premium) protects against a crash between checks;
the trend exit is the normal exit. Positions are NRML (carried overnight), so they are not squared off at 15:20.
"""
from __future__ import annotations

import math
import threading
import time as _time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from .options import bs_price, expiries, index_cfg, select_contract, spot_instrument
from .positions import ACTIVE_STATES, ManagedPosition, PositionSpec
from .risk import RiskConfig

try:
    from .market_calendar import is_trading_day
except Exception:  # pragma: no cover
    def is_trading_day(d, data_dir=None):
        return d.weekday() < 5


@dataclass
class SwingConfig:
    index: str = "NIFTY"
    fast: int = 10
    slow: int = 50
    mode: str = "paper"
    lots: int = 1
    min_days_to_expiry: int = 20  # buy a monthly expiry with at least this many days left
    roll_days: int = 7  # roll to the next monthly when this many days or fewer are left
    check_time: str = "15:15"  # daily decision time (IST)
    disaster_stop_pct: float = 60.0  # emergency stop on the premium; the trend exit is the normal exit
    max_premium: float = 0.0  # ₹ cap on premium per entry (0 = no cap)
    sizing: str = "capital"  # capital = lots grow/shrink with your money | fixed = always `lots`
    capital: float = 200_000.0  # starting money for capital sizing (profits and losses of this strategy are added)
    alloc_pct: float = 10.0  # % of current money put into premium at each buy / roll (10% = smaller worst drops)
    max_lots: int = 10  # never more than this many lots
    itm_strikes: int = 2  # how far in the money (2 tested slightly better than 1: less time decay, moves more with the index)
    book_half_pct: float = 6.0  # book half the lots once the index is up this % from entry (0 = off). In 2017-23 with growing
    #                             lots it gave more winning trades, a higher end amount and a smaller worst drop; with 1 fixed
    #                             lot it gave less total profit (and in 2008-16 it was worse) — the price of locking profit early
    trail_atr: float = 0.0  # also exit if the index closes this many daily ranges (ATR) below its best close (0 = off)
    crash_guard_atr: float = 2.0  # watched every minute: sell at once if the index trades this many daily ranges below
    #                               yesterday's close (0 = off). Roughly halved the worst drop in both test periods.

    def validate(self) -> "SwingConfig":
        self.index = self.index.upper()
        if self.index not in ("NIFTY", "BANKNIFTY", "SENSEX"):
            raise ValueError("Index must be NIFTY, BANKNIFTY or SENSEX")
        if not (2 <= self.fast < self.slow <= 250):
            raise ValueError("Fast average must be shorter than the slow one (2-250 days)")
        if self.mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if not (1 <= self.lots <= 50):
            raise ValueError("Lots must be 1-50")
        if not (8 <= self.min_days_to_expiry <= 60) or not (1 <= self.roll_days < self.min_days_to_expiry):
            raise ValueError("Expiry days: buy with 8-60 days left, roll with fewer days left than that")
        if not ("14:30" <= self.check_time <= "15:25"):
            raise ValueError("Daily check time must be between 14:30 and 15:25")
        if self.sizing not in ("capital", "fixed"):
            raise ValueError("Sizing must be capital or fixed")
        if not (5 <= self.alloc_pct <= 50) or not (1 <= self.max_lots <= 100) or self.capital < 10_000:
            raise ValueError("Capital sizing: 5-50% per buy, 1-100 max lots, capital at least ₹10,000")
        if not (0 <= self.crash_guard_atr <= 6):
            raise ValueError("Crash guard must be 0-6 daily ranges")
        if not (1 <= self.itm_strikes <= 3) or not (0 <= self.book_half_pct <= 20) or not (0 <= self.trail_atr <= 8):
            raise ValueError("ITM strikes 1-3, book-half 0-20%, trailing 0-8 ATR")
        if not (20 <= self.disaster_stop_pct <= 90):
            raise ValueError("Emergency stop must be 20-90% of the premium")
        return self


def monthly_expiries(exps: list[str]) -> list[str]:
    """The last listed expiry of each month = the monthly contract."""
    by_m: dict[str, str] = {}
    for e in sorted(exps):
        by_m[e[:7]] = e
    return sorted(by_m.values())


def trend_state(closes: pd.Series, fast: int, slow: int) -> dict:
    """10/50 (fast/slow) daily averages, the side and when it last crossed."""
    c = closes.astype(float)
    f, s = c.rolling(fast).mean(), c.rolling(slow).mean()
    if len(c) < slow or pd.isna(s.iloc[-1]):
        raise ValueError(f"Need at least {slow} daily closes")
    up = f > s
    side = bool(up.iloc[-1])
    since = None
    for i in range(len(up) - 1, slow - 1, -1):
        if bool(up.iloc[i]) != side:
            since = str(pd.Timestamp(up.index[i + 1]).date()) if i + 1 < len(up) else None
            break
    gap_pct = (f.iloc[-1] - s.iloc[-1]) / s.iloc[-1] * 100
    return {"trend": "UP" if side else "DOWN", "fast": round(float(f.iloc[-1]), 2), "slow": round(float(s.iloc[-1]), 2),
            "gap_pct": round(float(gap_pct), 2), "since": since, "close": round(float(c.iloc[-1]), 2)}


# ------------------------------------------------------------------ backtest (same rule, option priced from India VIX)
def backtest(daily: pd.DataFrame, cfg: SwingConfig, lot: int, step: float, vix: pd.Series | None = None,
             charges_per_order: float = 25.0, slippage_pct: float = 1.0) -> dict:
    """daily: DataFrame indexed by date with 'close' (and 'high'/'low' for the trailing exit). Decision and fill at the
    day's close (as the live rule). Holds an in-the-money CE (cfg.itm_strikes), ~30 days to expiry at entry, rolled every
    21 trading days; optional book-half and trailing exit exactly as the live engine."""
    d = daily.copy()
    d.index = pd.to_datetime(d.index)
    c = d["close"].astype(float)
    v = (vix.reindex(d.index).ffill().bfill() if vix is not None and len(vix) else pd.Series(15.0, index=d.index)).clip(lower=8)
    f, s = c.rolling(cfg.fast).mean(), c.rolling(cfg.slow).mean()
    want = (f > s).astype(int).where(s.notna(), 0).to_numpy()
    closes, vv, idx = c.to_numpy().copy(), v.to_numpy(), d.index
    slip = slippage_pct / 100
    trades, pos_i = [], None
    legs: list[dict] = []
    cap = float(cfg.capital)
    cap_path: list[tuple[pd.Timestamp, float]] = [(idx[0], cap)] if len(idx) else []
    lots_seen: list[int] = []

    def n_lots(prem):
        per = prem * lot
        if cfg.sizing == "fixed":
            n = cfg.lots
        else:
            n = int(cfg.alloc_pct / 100 * cap // per) if per else 0
            if n < 1 and cap >= per * 1.05:
                n = 1
            n = min(n, cfg.max_lots)
        if cfg.max_premium and per:
            n = min(n, int(cfg.max_premium // per))
        return max(n, 0)

    def leg_open(i):
        k = math.floor(closes[i] / step) * step - step * cfg.itm_strikes  # in the money
        p0 = bs_price(closes[i], k, 30 / 365, vv[i], "CE")
        n = n_lots(p0)
        lots_seen.append(n)
        return {"i": i, "k": k, "p": p0, "n": n}

    def leg_close(leg, i, frac=1.0):
        """P&L of ONE lot of this leg (before scaling by `frac`); the money path is updated for the part closed."""
        nonlocal cap
        days_left = max(30 - (i - leg["i"]) * 1.4, 0.5)
        p1 = bs_price(closes[i], leg["k"], days_left / 365, vv[i], "CE")
        one = (p1 - leg["p"]) * lot - 2 * charges_per_order - slip * (leg["p"] + p1) * lot
        cap += one * leg["n"] * frac
        cap_path.append((idx[i], cap))
        return one

    hi_col = d["high"].astype(float) if "high" in d else c
    lo_col = d["low"].astype(float) if "low" in d else c
    tr = pd.concat([hi_col - lo_col, (hi_col - c.shift()).abs(), (lo_col - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().to_numpy()
    pos: dict | None = None
    guard_wait = False
    for i in range(len(c)):
        if pos is None:
            if not want[i]:
                guard_wait = False
            if want[i] and not guard_wait and i < len(c) - 1:
                pos = {"start": i, "legs": [leg_open(i)], "frac": 1.0, "hi": closes[i], "entry": closes[i], "booked": False,
                       "pnl1": 0.0, "sized": 0.0}
            continue
        pos["hi"] = max(pos["hi"], closes[i])
        leg = pos["legs"][-1]
        lows = lo_col.to_numpy()
        if cfg.crash_guard_atr and i > 0 and not np.isnan(atr[i - 1]) and lows[i] <= closes[i - 1] - cfg.crash_guard_atr * atr[i - 1]:
            lvl = closes[i - 1] - cfg.crash_guard_atr * atr[i - 1]
            saved = closes[i]
            closes[i] = lvl  # filled at the guard level that day
            one = leg_close(leg, i, pos["frac"])
            closes[i] = saved
            pos["pnl1"] += one * pos["frac"]
            pos["sized"] += one * leg["n"] * pos["frac"]
            trades.append({"entry": str(idx[pos["start"]].date()), "exit": str(idx[i].date()), "days": int(i - pos["start"]),
                           "index_from": round(float(closes[pos["start"]]), 2), "index_to": round(float(lvl), 2),
                           "rolls": len(pos["legs"]) - 1, "net": round(float(pos["pnl1"]), 2), "open": False,
                           "lots": [lg["n"] for lg in pos["legs"]], "booked_half": pos["booked"], "crash_guard": True,
                           "net_sized": round(float(pos["sized"]), 2), "capital_after": round(cap, 0)})
            pos, guard_wait = None, True
            continue
        exit_ = not want[i] or i == len(c) - 1
        if cfg.trail_atr and not np.isnan(atr[i]) and closes[i] < pos["hi"] - cfg.trail_atr * atr[i]:
            exit_ = True
        if cfg.book_half_pct and not pos["booked"] and leg["n"] >= 2 and closes[i] >= pos["entry"] * (1 + cfg.book_half_pct / 100):
            one = leg_close(leg, i, 0.5)
            pos["pnl1"] += one * 0.5
            pos["sized"] += one * leg["n"] * 0.5
            pos["frac"], pos["booked"] = 0.5, True
        if exit_ or (i - leg["i"] >= 21):
            one = leg_close(leg, i, pos["frac"])
            pos["pnl1"] += one * pos["frac"]
            pos["sized"] += one * leg["n"] * pos["frac"]
            if exit_:
                trades.append({"entry": str(idx[pos["start"]].date()), "exit": str(idx[i].date()), "days": int(i - pos["start"]),
                               "index_from": round(float(closes[pos["start"]]), 2), "index_to": round(float(closes[i]), 2),
                               "rolls": len(pos["legs"]) - 1, "net": round(float(pos["pnl1"]), 2), "open": bool(want[i] and i == len(c) - 1),
                               "lots": [lg["n"] for lg in pos["legs"]], "booked_half": pos["booked"],
                               "net_sized": round(float(pos["sized"]), 2), "capital_after": round(cap, 0)})
                pos = None
            else:  # roll the remaining part into a fresh monthly
                pos["legs"].append(leg_open(i))
    a = np.array([t["net"] for t in trades]) if trades else np.array([])
    eq = np.cumsum(a) if len(a) else np.array([0.0])
    dd = float((eq - np.maximum.accumulate(np.r_[0.0, eq])[1:]).min()) if len(a) else 0.0
    years = max((idx[-1] - idx[0]).days / 365.25, 0.1) if len(idx) > 1 else 1.0
    bh = (closes[-1] - closes[0]) * lot if len(closes) > 1 else 0.0
    bh_eq = (c - c.iloc[0]) * lot
    yearly: dict[str, float] = {}
    for t in trades:
        yearly[t["exit"][:4]] = round(yearly.get(t["exit"][:4], 0.0) + t["net"], 0)
    gw, gl = float(a[a > 0].sum()) if len(a) else 0.0, float(-a[a <= 0].sum()) if len(a) else 0.0
    return {"from": str(idx[0].date()) if len(idx) else None, "to": str(idx[-1].date()) if len(idx) else None,
            "trades": len(a), "wins": int((a > 0).sum()) if len(a) else 0,
            "win_pct": round(float((a > 0).mean() * 100), 1) if len(a) else None,
            "net": round(float(a.sum()), 0) if len(a) else 0.0, "per_year": round(float(a.sum() / years), 0) if len(a) else 0.0,
            "profit_factor": round(gw / gl, 2) if gl else None, "max_drawdown": round(dd, 0),
            "in_market_pct": round(float(want.mean() * 100), 0),
            "buy_hold": round(float(bh), 0), "buy_hold_max_drawdown": round(float((bh_eq - bh_eq.cummax()).min()), 0),
            "yearly": yearly, "list": trades[-30:], "lot": lot,
            "sizing": cfg.sizing, "alloc_pct": cfg.alloc_pct, "capital_start": cfg.capital, "capital_end": round(cap, 0),
            "capital_worst_drop": _worst_drop([x[1] for x in cap_path]) if cap_path else 0.0,
            "capital_yearly_pct": _yearly_pct(cap_path, cfg.capital),
            "lots_range": [min(lots_seen), max(lots_seen)] if lots_seen else [0, 0],
            "note": "Option prices are modelled from the index and India VIX (Black-Scholes), not real option quotes; "
                    "1 lot, charges ₹25 per order and 1% slippage each side included."}


def _worst_drop(vals: list[float]) -> float:
    peak, dd = -1e18, 0.0
    for v in vals:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return round(dd, 0)


def _yearly_pct(path: list, start: float) -> dict:
    out, prev = {}, start
    by: dict[int, float] = {}
    for t, v in path:
        by[pd.Timestamp(t).year] = v
    for y in sorted(by):
        out[str(y)] = round((by[y] / prev - 1) * 100, 1) if prev else None
        prev = by[y]
    return out


# ------------------------------------------------------------------ live / paper engine
class SwingTrader:
    def __init__(self, cfg: SwingConfig, deps, register_position: Callable[[ManagedPosition], None],
                 on_change: Callable[["SwingTrader"], None] | None = None, sid: str | None = None):
        cfg.validate()
        self.id = sid or uuid.uuid4().hex[:8]
        self.cfg, self.deps = cfg, deps
        self.register_position, self.on_change = register_position, on_change
        self.lock = threading.RLock()
        self.enabled = False
        self.position: ManagedPosition | None = None
        self.last_check: str | None = None  # date of the last daily decision
        self.signal: dict | None = None
        self.timeline: list[dict] = []
        self.created = datetime.now().isoformat(timespec="seconds")
        self.realized = 0.0  # closed P&L of this swing strategy (₹), added to `capital` for sizing
        self.counted: list[str] = []
        self.wait_cross = False  # after a trailing exit: no re-buy until the trend has turned down and up again

    # ---------------------------------------------------------- helpers
    def log(self, msg: str, kind: str = "info"):
        self.timeline.append({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "msg": msg})
        self.timeline = self.timeline[-200:]
        self._changed()

    def _changed(self):
        if self.on_change:
            try:
                self.on_change(self)
            except Exception:
                pass

    def settle(self):
        """Add the closed position's net P&L to this strategy's money (once)."""
        p = self.position
        if p is not None and p.state == "POSITION_CLOSED" and p.id not in self.counted:
            self.realized = round(self.realized + float(p.net), 2)
            self.counted = (self.counted + [p.id])[-200:]

    @property
    def equity(self) -> float:
        return round(self.cfg.capital + self.realized, 2)

    def lots_for(self, premium: float, lot_size: int) -> tuple[int, str]:
        cfg = self.cfg
        per_lot = premium * lot_size
        if cfg.sizing == "fixed":
            n, why = cfg.lots, f"fixed {cfg.lots} lot{'s' if cfg.lots > 1 else ''}"
        else:
            eq = self.equity
            n = int(cfg.alloc_pct / 100 * eq // per_lot) if per_lot else 0
            why = f"{cfg.alloc_pct:g}% of ₹{eq:,.0f} = ₹{cfg.alloc_pct / 100 * eq:,.0f} ÷ ₹{per_lot:,.0f} per lot"
            if n < 1 and eq >= per_lot * 1.05:
                n, why = 1, why + " → 1 lot (smallest)"
            n = min(n, cfg.max_lots)
        if cfg.max_premium and per_lot:
            n = min(n, int(cfg.max_premium // per_lot))
        return max(n, 0), why

    @property
    def active(self) -> bool:
        return self.position is not None and self.position.state in ACTIVE_STATES

    def daily_frame(self, now: datetime) -> pd.DataFrame:
        inst = self.deps.instruments()
        sp = spot_instrument(inst, self.cfg.index)
        rows = self.deps.history(sp["instrument_token"], "day", now - timedelta(days=self.cfg.slow * 2 + 60), now)
        df = pd.DataFrame(rows)
        if df.empty:
            raise ValueError("No daily candles")
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        df = df.set_index("date")[["high", "low", "close"]].astype(float)
        live = self._spot_live(sp)
        if live:  # today's candle so far, with the live price as today's close
            t = pd.Timestamp(now.date())
            if t in df.index:
                df.loc[t, "close"] = live
                df.loc[t, "high"], df.loc[t, "low"] = max(df.loc[t, "high"], live), min(df.loc[t, "low"], live)
            else:
                df.loc[t] = [live, live, live]
        return df[~df.index.duplicated(keep="last")].sort_index()

    def daily_closes(self, now: datetime) -> pd.Series:
        return self.daily_frame(now)["close"]

    def _spot_live(self, sp) -> float | None:
        try:
            key = f"{sp['exchange']}:{sp['tradingsymbol']}"
            return float(self.deps.quote([key])[key]["last_price"])
        except Exception:
            return None

    def days_left(self, expiry: str, today: date) -> int:
        return (date.fromisoformat(str(expiry)[:10]) - today).days

    # ---------------------------------------------------------- every-minute crash guard
    def watch(self, now: datetime):
        """While a position is open: once a minute, compare the index with yesterday's close minus k daily ranges."""
        if not self.cfg.crash_guard_atr or not self.active:
            return None
        t = _time.monotonic()
        if t - getattr(self, "_watched", 0.0) < 55:
            return None
        self._watched = t
        day = now.date().isoformat()
        if getattr(self, "_guard_day", None) != day:
            try:
                df = self.daily_frame(now)
                df = df[df.index < pd.Timestamp(now.date())]
                tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                                (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1])
                prev = float(df["close"].iloc[-1])
            except Exception:
                return None
            self._guard_day, self._guard = day, {"prev_close": prev, "atr": atr, "level": round(prev - self.cfg.crash_guard_atr * atr, 2)}
        g = self._guard
        sp = spot_instrument(self.deps.instruments(), self.cfg.index)
        spot = self._spot_live(sp)
        if not spot:
            return None
        self.watching = {"at": now.isoformat(timespec="minutes"), "spot": spot, **g,
                         "room_pct": round((spot - g["level"]) / spot * 100, 2)}
        if spot <= g["level"]:
            self.log(f"Crash guard: {self.cfg.index} {spot:,.0f} fell below {g['level']:,.0f} (yesterday {g['prev_close']:,.0f} − "
                     f"{self.cfg.crash_guard_atr:g} daily ranges) → selling {self.position.spec.tradingsymbol} now; no re-buy until a fresh uptrend", "signal")
            self.position.request_exit("crash_guard")
            self.wait_cross = True
        return self.watching

    # ---------------------------------------------------------- the daily decision
    def on_clock(self, now: datetime):
        t = now.strftime("%H:%M")
        if self.active and "09:16" <= t <= "15:28":  # guards an open position even after you press Stop
            try:
                self.watch(now)
            except Exception:
                pass
        if not self.enabled:
            return
        if not (self.cfg.check_time <= t <= "15:25") or self.last_check == now.date().isoformat():
            return
        if not is_trading_day(now.date(), getattr(self.deps, "data_dir", None)):
            return
        self.check(now)

    def check(self, now: datetime) -> dict:
        with self.lock:
            self.settle()
            self.last_check = now.date().isoformat()
            try:
                df = self.daily_frame(now)
                st = trend_state(df["close"], self.cfg.fast, self.cfg.slow)
                tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                                (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1]) if len(df) >= 15 else 0.0
            except Exception as e:
                self.log(f"Daily check failed: {e}"[:200], "alert")
                return {"error": str(e)}
            self.signal = {**st, "checked_at": now.isoformat(timespec="minutes")}
            pos = self.position if self.active else None
            what = f"{self.cfg.index} {st['close']:,.0f} · {self.cfg.fast}-day avg {st['fast']:,.0f} vs {self.cfg.slow}-day {st['slow']:,.0f}"
            if pos is not None:
                try:  # fresh option price for the decisions below (partial booking needs it)
                    key = f"{pos.spec.exchange}:{pos.spec.tradingsymbol}"
                    lp = float(self.deps.quote([key])[key]["last_price"])
                    if lp > 0:
                        pos.on_tick(lp)
                except Exception:
                    pass
                spot = st["close"]
                hi = max(float(pos.stats.get("hi_close") or spot), spot)
                pos.stats["hi_close"] = hi
                entry_spot = float(pos.stats.get("spot_at_entry") or spot)
                trail_hit = bool(self.cfg.trail_atr and atr and spot < hi - self.cfg.trail_atr * atr)
                if self.cfg.book_half_pct and not pos.stats.get("booked_half") and spot >= entry_spot * (1 + self.cfg.book_half_pct / 100):
                    lots = pos.filled_qty // pos.spec.lot_size if pos.spec.lot_size else 0
                    if lots >= 2:
                        half = (lots // 2) * pos.spec.lot_size
                        if pos.book_partial(half, "swing_book_half"):
                            pos.stats["booked_half"] = True
                            self.log(f"{self.cfg.index} is up {(spot / entry_spot - 1) * 100:.1f}% since the buy → booked {half // pos.spec.lot_size} "
                                     f"of {lots} lots; the rest keeps riding the trend", "order")
                    else:
                        pos.stats["booked_half"] = True
                        self.log(f"Up {(spot / entry_spot - 1) * 100:.1f}% — would book half, but there is only 1 lot", "info")
                if st["trend"] == "DOWN" or trail_hit:
                    why = (f"Trend turned DOWN ({what})" if st["trend"] == "DOWN" else
                           f"{self.cfg.index} {spot:,.0f} fell {self.cfg.trail_atr:g} daily ranges below its best close {hi:,.0f} (trailing exit)")
                    self.log(f"{why} → selling {pos.spec.tradingsymbol} and staying out", "signal")
                    pos.request_exit("trend_down" if st["trend"] == "DOWN" else "trail")
                    if st["trend"] != "DOWN":
                        self.wait_cross = True
                elif pos.spec.expiry and self.days_left(pos.spec.expiry, now.date()) <= self.cfg.roll_days:
                    self.log(f"Rolling: {pos.spec.tradingsymbol} has {self.days_left(pos.spec.expiry, now.date())} days left "
                             f"→ selling it and buying the next monthly (trend still UP)", "signal")
                    pos.request_exit("roll")
                    self.settle()
                    self._open(now, roll=True)
                else:
                    self.log(f"Trend still UP ({what}) — holding {pos.spec.tradingsymbol}", "info")
            elif st["trend"] == "UP" and getattr(self, "wait_cross", False):
                self.log(f"Trend is UP ({what}) but the trailing exit sold this move — waiting for the next fresh uptrend", "info")
            elif st["trend"] == "UP":
                self.log(f"Trend is UP ({what}) → buying a monthly in-the-money CE", "signal")
                self._open(now)
            else:
                self.wait_cross = False
                self.log(f"Trend is DOWN ({what}) — staying out", "info")
            self._changed()
            return self.signal

    def _open(self, now: datetime, roll: bool = False):
        cfg = self.cfg
        inst = self.deps.instruments()
        exps = [e for e in monthly_expiries(expiries(inst, cfg.index, now.date()))
                if self.days_left(e, now.date()) >= cfg.min_days_to_expiry]
        if not exps:
            self.log("No monthly expiry with enough days left — not buying", "alert")
            return None
        exp = exps[0]
        sp = spot_instrument(inst, cfg.index)
        spot = self._spot_live(sp)
        if not spot:
            self.log("No live index price — not buying", "alert")
            return None
        c = select_contract(inst, cfg.index, exp, "CE", spot, "ITM", cfg.itm_strikes)
        key = f"{c.exchange}:{c.tradingsymbol}"
        q = self.deps.quote([key]).get(key, {})
        depth = q.get("depth") or {}
        ask = ((depth.get("sell") or [{}])[0] or {}).get("price") or None
        ref = float(ask or q.get("last_price") or 0)
        if not ref:
            self.log(f"No price for {c.tradingsymbol} — not buying", "alert")
            return None
        lots, why = self.lots_for(ref, c.lot_size)
        if lots < 1:
            cap_note = f"your premium cap ₹{cfg.max_premium:,.0f}" if cfg.max_premium else f"this strategy's money ₹{self.equity:,.0f}"
            self.log(f"1 lot of {c.tradingsymbol} costs ₹{ref * c.lot_size:,.0f}, more than {cap_note} allows — not buying", "alert")
            return None
        spec = PositionSpec(exchange=c.exchange, tradingsymbol=c.tradingsymbol, instrument_token=c.instrument_token,
                            lots=lots, lot_size=c.lot_size, side=1, product="NRML", tick_size=c.tick_size,
                            instrument_type="CE", strike=c.strike, expiry=c.expiry, source=f"swing:{self.id}", tag="kaswing")
        risk = RiskConfig.from_dict({"sl_mode": "pct", "sl_value": cfg.disaster_stop_pct, "target_mode": "none",
                                     "trail_mode": "none", "min_sl_pct": 1.0, "max_sl_pct": 95.0})
        p = ManagedPosition(spec, risk, cfg.mode, self.deps.kite_getter)
        p.stats.update({"entry_type": "swing", "why": [f"{cfg.fast}/{cfg.slow}-day trend UP", "roll" if roll else "new trend"],
                        "index": cfg.index, "spot_at_entry": spot, "strategy_name": f"Swing trend {cfg.fast}/{cfg.slow}"})
        p.preview(ref)
        self.register_position(p)
        p.open(ref)
        self.position = p
        self.log(f"Bought {lots} lot{'s' if lots > 1 else ''} {c.tradingsymbol} (strike {c.strike:g}, expiry {c.expiry}) at ~₹{ref:.2f} "
                 f"= ₹{ref * lots * c.lot_size:,.0f} premium ({why}) · emergency stop −{cfg.disaster_stop_pct:g}%", "order")
        return p

    # ---------------------------------------------------------- control / persistence
    def close_now(self, reason: str = "manual_close"):
        if self.active:
            self.position.request_exit(reason)
            self.log("Position closed by you", "order")

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        p = self.position
        pos = None
        if p is not None:
            pos = {"id": p.id, "state": p.state, "tradingsymbol": p.spec.tradingsymbol, "strike": p.spec.strike,
                   "expiry": p.spec.expiry, "qty": p.filled_qty or p.spec.qty, "entry_price": p.entry_price, "ltp": p.ltp,
                   "stop": p.stop, "unrealized": round(p.unrealized(), 2) if p.is_open else None, "net": p.net if not p.is_open else None,
                   "entry_time": p.entry_time, "exit_time": p.exit_time, "exit_reason": p.exit_reason,
                   "days_left": self.days_left(p.spec.expiry, now.date()) if p.spec.expiry else None}
        self.settle()
        return {"id": self.id, "config": asdict(self.cfg), "enabled": self.enabled, "active": self.active, "position": pos,
                "equity": self.equity, "realized": self.realized,
                "signal": self.signal, "last_check": self.last_check, "timeline": self.timeline[-60:],
                "watching": getattr(self, "watching", None) if self.active else None,
                "next_check": f"{self.cfg.check_time} on trading days", "created": self.created}

    def to_store(self) -> dict:
        return {"id": self.id, "config": asdict(self.cfg), "enabled": self.enabled, "last_check": self.last_check,
                "signal": self.signal, "timeline": self.timeline[-200:], "created": self.created,
                "position_id": self.position.id if self.position else None, "realized": self.realized, "counted": self.counted, "wait_cross": self.wait_cross}
