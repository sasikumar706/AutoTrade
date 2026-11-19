"""Honest engine replay: run the REAL Auto Trade engine over past days, then compare it with random entries.

Why: the AI test checks single strategies on index candles. Live trading adds the morning pick, extra entry
types, filters, time-stops, trailing stops and the daily/weekly limits on top. This module replays past days
through the exact same AutoTrader code, candle by candle, with option premiums priced by Black-Scholes from
the index (the only option prices available for past days — Kite has no candles for expired contracts):

* each day the morning pick sees only data from BEFORE that day (no look-ahead);
* premiums include a bid-ask spread, paper fills add the configured slippage, Zerodha charges are applied;
* every trade is recorded with its entry type, exit reason and the pick that produced it.

Then the same number of RANDOM entries (random day, time and direction, same stop/target/trailing/time-stop
rules and costs) is simulated many times. If the engine doesn't clearly beat random, it has no proven edge.

Limits (shown in the result): Black-Scholes with one volatility can't show real IV changes, skew or expiry-day
spikes, so results are an estimate — saved real option quotes (data/option_quotes) will make this better.
"""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from .autotrade import AutoConfig, AutoTrader, Deps
from .candles import INTERVAL_MINUTES
from .options import bs_price, estimated_expiry, index_cfg, years_to_expiry
from .positions import ManagedPosition, PositionSpec
from .risk import RiskConfig

SPREAD_PCT = 0.6  # simulated bid-ask spread, % of premium (ATM weekly index options; wider than the real tightest)
LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}


class SimMarket:
    """Fake Kite for one replay: synthetic option chain, Black-Scholes quotes from the simulated index price."""

    def __init__(self, index: str, iv: float):
        self.index, self.iv = index.upper(), iv
        self.cfg = index_cfg(index)
        self.spot: float | None = None
        self.now: datetime | None = None
        self.contracts: dict[str, tuple[float, date, str]] = {}
        self.inst: pd.DataFrame | None = None
        self.inst_day: date | None = None

    # instruments for the simulated day: spot rows + 2 expiries x strikes around the price
    def instruments(self) -> pd.DataFrame:
        d = self.now.date()
        if self.inst is not None and self.inst_day == d:
            return self.inst
        c, step = self.cfg, self.cfg["step"]
        rows = [dict(instrument_token=c["spot_token"], exchange=c["spot_exchange"], tradingsymbol=c["spot_symbol"],
                     name=c["spot_symbol"], instrument_type="EQ", segment="INDICES", expiry="", strike=0.0, lot_size=0, tick_size=0.05)]
        e1 = estimated_expiry(self.index, d)
        e2 = estimated_expiry(self.index, e1 + timedelta(days=1))
        mid = round((self.spot or 0) / step) * step
        tok = 5_000_000
        for exp in (e1, e2):
            for k in range(-40, 41):
                strike = float(mid + k * step)
                for ot in ("CE", "PE"):
                    tok += 1
                    sym = f"{c['name']}{exp:%y%m%d}{int(strike)}{ot}"
                    self.contracts[sym] = (strike, exp, ot)
                    rows.append(dict(instrument_token=tok, exchange=c["opt_exchange"], tradingsymbol=sym, name=c["name"],
                                     instrument_type=ot, segment=f"{c['opt_exchange']}-OPT", expiry=exp.isoformat(),
                                     strike=strike, lot_size=LOT.get(self.index, 50), tick_size=0.05))
        self.inst, self.inst_day = pd.DataFrame(rows), d
        return self.inst

    def premium(self, sym: str, spot: float | None = None, now: datetime | None = None) -> float:
        strike, exp, ot = self.contracts[sym]
        return round(bs_price(spot or self.spot, strike, years_to_expiry(now or self.now, exp), self.iv, ot), 2)

    def quote(self, keys: list[str]) -> dict:
        out = {}
        for k in keys:
            sym = k.split(":", 1)[-1]
            if sym == "INDIA VIX":
                out[k] = {"last_price": self.iv}
            elif sym in self.contracts:
                p = self.premium(sym)
                half = max(0.05, p * SPREAD_PCT / 200)
                out[k] = {"last_price": p, "ohlc": {"close": p},
                          "depth": {"buy": [{"price": round(p - half, 2)}], "sell": [{"price": round(p + half, 2)}]}}
            else:
                out[k] = {"last_price": self.spot}
        return out


def _trading_days(candles: pd.DataFrame) -> list[date]:
    return sorted(set(candles["date"].dt.date))


def _path(o: float, h: float, l: float, c: float) -> list[float]:
    """Intra-candle price path: up candles visit the low first, down candles the high first."""
    return [o, l, h, c] if c >= o else [o, h, l, c]


def replay_engine(cfg: AutoConfig, candles: list[dict], days: int = 20, iv: float = 14.0,
                  progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict:
    """Replay the last `days` trading days of `candles` (index candles at cfg.interval, newest last)."""
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.sort_values("date").reset_index(drop=True)
    all_days = _trading_days(df)
    if len(all_days) < days + 15:
        raise ValueError(f"Need at least {days + 15} days of candles to replay {days} days")
    sim_days = all_days[-days:]
    mins = INTERVAL_MINUTES[cfg.interval]
    rcfg = replace(cfg, mode="paper", autopilot=False, params=dict(cfg.params), risk=dict(cfg.risk))  # replay the current index
    market = SimMarket(cfg.index, iv)
    rows_by_day = {d: df[df["date"].dt.date == d] for d in sim_days}
    trades: list[dict] = []
    picks: list[dict] = []
    pnl_days: dict[str, float] = {}

    def history(tok, interval, a, b):
        cut = pd.Timestamp(market.now)
        sub = df[(df["date"] >= pd.Timestamp(a)) & (df["date"] + pd.Timedelta(minutes=mins) <= cut)]
        return sub.to_dict("records")

    deps = Deps(instruments=market.instruments, quote=market.quote, history=history, kite_getter=lambda: None,
                margin_available=None, order_margin=None, data_dir=None)
    for n, d in enumerate(sim_days, 1):
        if cancelled and cancelled():
            break
        if progress:
            progress(f"Replaying day {n} of {len(sim_days)} ({d:%d %b})")
        day_rows = rows_by_day[d]
        if day_rows.empty:
            continue
        market.now = datetime.combine(d, time(9, 0))
        market.spot = float(df[df["date"] < pd.Timestamp(market.now)]["close"].iloc[-1])
        positions: list[ManagedPosition] = []
        a = AutoTrader(replace(rcfg, params=dict(rcfg.params), risk=dict(rcfg.risk)), deps, positions.append)
        a.run_async = False
        a.pnl_days = dict(pnl_days)
        a.notify_enabled = False
        a.start_day(market.now)
        a.enable(market.now)
        if a.pick is not None:
            picks.append({"day": d.isoformat(), "strategy": a.pick.get("name"), "luck": (a.pick.get("luck") or {}).get("passed"),
                          "proven": bool(a.pick.get("strategy"))})
        for _, r in day_rows.iterrows():
            t0 = r["date"].to_pydatetime()
            pts = _path(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for i, px in enumerate(pts):
                market.now = t0 + timedelta(seconds=int(i * (mins * 60 - 1) / 3))
                market.spot = px
                a.on_index_tick(market.now, px)
                for p in positions:
                    if p.is_open or p.state == "EXIT_FAILED":
                        p.on_tick(market.premium(p.spec.tradingsymbol))
                        if p.state == "POSITION_CLOSED":
                            p._sim_closed = market.now
                            p.exit_time = market.now.isoformat(timespec="seconds")
            market.now = t0 + timedelta(minutes=mins)
            a.on_clock(market.now)
            for p in positions:
                if p.state == "POSITION_CLOSED" and not getattr(p, "_sim_closed", None):
                    p._sim_closed = market.now
                    p.exit_time = market.now.isoformat(timespec="seconds")  # simulated clock (cool-down etc. use it)
        # end of day: make sure everything is closed at the exit time
        market.now = datetime.combine(d, time(15, 20))
        a.on_clock(market.now)
        for p in positions:
            if p.is_open:
                p.on_tick(market.premium(p.spec.tradingsymbol))
                p.request_exit("time_cutoff")
            if p.state != "POSITION_CLOSED":
                continue
            closed_at = getattr(p, "_sim_closed", None) or market.now
            trades.append({"day": d.isoformat(), "entry_time": (p.entry_time or "")[11:16], "exit_time": f"{closed_at:%H:%M}",
                           "symbol": p.spec.tradingsymbol, "side": p.spec.instrument_type, "lots": p.spec.lots,
                           "entry": round(p.entry_price or 0, 2), "exit": round(p.exit_price or 0, 2), "net": round(float(p.net), 2),
                           "exit_reason": p.exit_reason, "entry_type": p.stats.get("entry_type") or "strategy",
                           "bars": round((closed_at - datetime.fromisoformat(p.entry_time)).total_seconds() / 60 / mins, 1) if p.entry_time else None})
        pnl_days[d.isoformat()] = round(sum(t["net"] for t in trades if t["day"] == d.isoformat()), 2)

    summary = _summary(trades)
    rnd = random_baseline(trades, df, sim_days, rcfg, market, iv, runs=100, cancelled=cancelled, progress=progress) if trades else None
    pick_days = [p for p in picks if p["proven"]]
    picker = None
    if picks:
        traded = [pnl_days.get(p["day"], 0.0) for p in pick_days]
        picker = {"days": len(picks), "days_with_proven_pick": len(pick_days),
                  "profitable_pick_days": sum(1 for x in traded if x > 0), "losing_pick_days": sum(1 for x in traded if x < 0),
                  "luck_passed_days": sum(1 for p in picks if p["luck"]),
                  "strategies_used": sorted({p["strategy"] for p in pick_days if p["strategy"]})}
    verdict = _verdict(summary, rnd)
    return {"config": {"index": rcfg.index, "interval": rcfg.interval, "days": len(sim_days), "iv": iv,
                       "from": sim_days[0].isoformat(), "to": sim_days[-1].isoformat(),
                       "strategy_source": rcfg.strategy_source, "strategy": rcfg.strategy},
            "summary": summary, "random": rnd, "picker": picker, "verdict": verdict,
            "daily": [{"day": d.isoformat(), "net": pnl_days.get(d.isoformat(), 0.0),
                       "trades": sum(1 for t in trades if t["day"] == d.isoformat()),
                       "pick": next((p["strategy"] for p in picks if p["day"] == d.isoformat()), None)} for d in sim_days],
            "trades": trades[-200:],
            "limits": ["Option prices are Black-Scholes estimates from the index with one volatility "
                       f"({iv:g}%) and a {SPREAD_PCT}% spread — real IV changes, skew and expiry-day spikes are not included.",
                       "Autopilot is replayed on the index and candle size it uses now.",
                       "Results are a test, not a promise: a no-edge system can still look good over a few weeks."]}


def _summary(trades: list[dict]) -> dict:
    nets = np.array([t["net"] for t in trades], dtype=float)
    if not len(nets):
        return {"trades": 0, "net": 0.0, "win_rate_pct": 0.0, "profit_factor": None, "max_drawdown": 0.0, "avg_net": 0.0,
                "by_entry_type": {}, "exit_reasons": {}}
    wins, losses = nets[nets > 0], nets[nets <= 0]
    eq = np.concatenate([[0.0], np.cumsum(nets)])
    by_type: dict[str, dict] = {}
    for t in trades:
        g = by_type.setdefault(t["entry_type"], {"trades": 0, "net": 0.0, "wins": 0})
        g["trades"] += 1
        g["net"] = round(g["net"] + t["net"], 2)
        g["wins"] += t["net"] > 0
    return {"trades": int(len(nets)), "net": round(float(nets.sum()), 2), "avg_net": round(float(nets.mean()), 2),
            "win_rate_pct": round(len(wins) / len(nets) * 100, 1),
            "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
            "max_drawdown": round(float((eq - np.maximum.accumulate(eq)).min()), 2),
            "by_entry_type": by_type,
            "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in sorted({t["exit_reason"] or "?" for t in trades})}}


def random_baseline(trades: list[dict], df: pd.DataFrame, sim_days: list[date], cfg: AutoConfig, market: SimMarket, iv: float,
                    runs: int = 100, seed: int = 7, cancelled=None, progress=None) -> dict:
    """Same number of trades with RANDOM day, time and direction; same risk rules, costs and time-stop."""
    rng = random.Random(seed)
    mins = INTERVAL_MINUTES[cfg.interval]
    e0, e1 = cfg.entry_start, cfg.entry_end
    by_day = {d: df[(df["date"].dt.date == d) & (df["date"].dt.strftime("%H:%M") >= e0)
                    & (df["date"].dt.strftime("%H:%M") < e1)] for d in sim_days}
    full_day = {d: df[df["date"].dt.date == d] for d in sim_days}
    days_ok = [d for d in sim_days if len(by_day[d])]
    if not days_ok:
        return None
    lots = [t["lots"] for t in trades]
    results = []
    step = market.cfg["step"]
    exit_t = cfg.exit_time
    for run in range(runs):
        if cancelled and cancelled():
            break
        if progress and run % 20 == 0:
            progress(f"Random-entry comparison {run} of {runs}")
        total = 0.0
        for k in range(len(trades)):
            d = rng.choice(days_ok)
            cand = by_day[d]
            i = rng.randrange(len(cand))
            row = cand.iloc[i]
            ot = rng.choice(("CE", "PE"))
            start = row["date"] + pd.Timedelta(minutes=mins)  # enter at the candle close, like the engine
            spot = float(row["close"])
            strike = float(round(spot / step) * step)
            exp = estimated_expiry(cfg.index, d)
            if cfg.skip_expiry_day and exp == d:
                exp = estimated_expiry(cfg.index, d + timedelta(days=1))
            sym = f"R{exp:%y%m%d}{int(strike)}{ot}"
            market.contracts[sym] = (strike, exp, ot)
            prem = bs_price(spot, strike, years_to_expiry(start.to_pydatetime(), exp), iv, ot)
            ask = prem * (1 + SPREAD_PCT / 200)
            spec = PositionSpec(exchange=market.cfg["opt_exchange"], tradingsymbol=sym, instrument_token=1, lots=lots[k],
                                lot_size=LOT.get(cfg.index, 50), side=1, instrument_type=ot, strike=strike, expiry=exp.isoformat())
            p = ManagedPosition(spec, RiskConfig.from_dict(cfg.risk), "paper", None, slippage_bps=cfg.slippage_bps)
            try:
                p.open(round(ask, 2), start.to_pydatetime())
            except Exception:
                continue
            later = full_day[d][full_day[d]["date"] >= start]
            for _, r in later.iterrows():
                t0 = r["date"].to_pydatetime()
                if t0.strftime("%H:%M") >= exit_t:
                    break
                for j, px in enumerate(_path(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))):
                    now = t0 + timedelta(seconds=int(j * (mins * 60 - 1) / 3))
                    p.on_tick(round(bs_price(px, strike, years_to_expiry(now, exp), iv, ot), 2))
                    if not p.is_open:
                        break
                if not p.is_open:
                    break
                # same time-stop rule as the engine
                held = (t0 + timedelta(minutes=mins) - start.to_pydatetime()).total_seconds() / 60 / mins
                qp = getattr(cfg, "quick_target_pts", 0)
                if qp and held >= cfg.quick_target_after_bars and p.target is not None and p.target - p.entry_price > qp \
                        and not p.stats.get("quick_target") and p.ltp is not None:
                    p.stats["quick_target"] = True
                    if p.ltp - p.entry_price >= qp:
                        p.request_exit("quick_target")
                        break
                    p.lower_target(p.entry_price + qp, "slow trade")
                if cfg.time_stop_bars and held >= cfg.time_stop_bars and p.risk_unit and p.ltp is not None:
                    if (p.ltp - p.entry_price) / p.risk_unit < cfg.time_stop_min_r:
                        p.request_exit("time_stop")
                        break
            if p.is_open:
                p.request_exit("time_cutoff")
            total += float(p.net)
        results.append(round(total, 2))
    if not results:
        return None
    real = sum(t["net"] for t in trades)
    arr = np.array(results)
    return {"runs": len(results), "real_net": round(real, 2), "random_mean": round(float(arr.mean()), 2),
            "random_p10": round(float(np.percentile(arr, 10)), 2), "random_p90": round(float(np.percentile(arr, 90)), 2),
            "beats_pct": round(float((arr < real).mean() * 100), 1)}


def _verdict(s: dict, rnd: dict | None) -> dict:
    if s["trades"] < 10:
        return {"level": "unknown", "text": f"Only {s['trades']} trades — too few to judge. Replay more days."}
    if s["net"] <= 0:
        return {"level": "bad", "text": "Lost money over the replay after costs. Don't go live with these settings."}
    if rnd and rnd["beats_pct"] < 90:
        return {"level": "weak", "text": f"Made money, but random entries with the same rules did as well {100 - rnd['beats_pct']:.0f}% of the "
                                         "time — the profit may be luck or just the stop/target rules, not the strategy."}
    if s["trades"] < 30:
        return {"level": "promising", "text": "Profitable and better than random, but on fewer than 30 trades. Keep paper trading."}
    return {"level": "good", "text": "Profitable after costs and better than 90% of random runs. Still confirm with 8–12 weeks of paper trading."}
