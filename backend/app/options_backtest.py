"""Options backtest: signals on the INDEX, trades in CE/PE contracts.

Premium sources (switchable):
* "real": candles of real, currently-listed option contracts fetched from Kite. Accurate,
  but Kite has no data for expired contracts, so only recent weeks can be tested.
* "estimated": premiums from Black-Scholes using the index price, an IV assumption and the
  time left to the (rule-based) expiry. Long history, approximate (no IV changes/skew).

Execution rules (same spirit as the equity backtest):
* signal at an index candle's close -> entry at the NEXT candle's open;
* strike chosen from the index price at entry (ATM / ITM n / OTM n / fixed strike);
* stop-loss is mandatory; stop checked before target inside a candle (pessimistic);
  trailing stop tightens only after the stop/target checks of that candle;
* entries only inside the entry window; every position closed at the exit cutoff
  (default 10:30) or the last candle of the day — options are never carried overnight;
* BUY side = buy to open (long CE on bullish, long PE on bearish signals);
  SELL side = sell to open (short PE on bullish, short CE on bearish) — needs permission;
* an opposite signal only CLOSES a position (no accidental reversal) unless allowed.
"""
from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

from .analysis import to_python
from .backtest import prepare_candles
from .charges import round_trip_charges
from .market_calendar import parse_hm
from .metrics import compute_metrics
from .options import bs_price, estimated_expiry, index_cfg, pick_strike, years_to_expiry
from .risk import RiskConfig, RiskError, compute_levels, trail_stop, validate_position
from .strategies import get_strategy


@dataclass
class OptionsBacktestConfig:
    index: str = "NIFTY"
    strategy: str = "morning_momentum"
    params: dict[str, Any] = field(default_factory=dict)
    side: str = "BUY"  # BUY (buy to open) | SELL (sell to open)
    option_rule: str = "AUTO"  # AUTO (CE on bullish, PE on bearish) | CE | PE
    strike_method: str = "ATM"  # ATM | ITM | OTM | <strike number>
    strike_offset: int = 1
    lots: int = 1
    lot_size: int = 0  # 0 = take from instrument list
    premium_source: str = "estimated"  # estimated | real
    iv_pct: float | None = None  # estimated mode; default per index
    min_days_to_expiry: int = 0  # estimated mode: skip to a later expiry if closer than this
    expiry: str | None = None  # real mode: the listed expiry to trade
    initial_capital: float = 100_000.0
    risk: dict = field(default_factory=lambda: RiskConfig().to_dict())
    slippage_bps: float = 30.0  # options spreads are wide; 30 bps of premium per fill
    entry_start: str = "09:30"
    entry_end: str = "10:30"
    exit_time: str = "10:30"
    max_trades_per_day: int = 2
    max_daily_loss: float | None = None
    allow_option_selling: bool = False
    reverse_on_opposite: bool = False

    def validate(self):
        index_cfg(self.index)
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if self.side == "SELL" and not self.allow_option_selling:
            raise PermissionError("Option selling is disabled. Enable 'allow option selling' to test SELL.")
        if self.option_rule not in ("AUTO", "CE", "PE"):
            raise ValueError("option_rule must be AUTO, CE or PE")
        if self.premium_source not in ("estimated", "real"):
            raise ValueError("premium_source must be estimated or real")
        if self.lots < 1:
            raise ValueError("Lots must be at least 1")
        if parse_hm(self.entry_start) >= parse_hm(self.entry_end):
            raise ValueError("Entry window start must be before its end")
        if parse_hm(self.exit_time) < parse_hm(self.entry_end):
            raise ValueError("Exit cutoff must be at or after the entry window end")
        RiskConfig.from_dict(self.risk)


class Cancelled(Exception):
    pass


class PremiumSource:
    """Gives the option premium OHLC on the index candle at position i."""

    def contract_for(self, ts: datetime, spot: float, option_type: str) -> dict | None:  # pragma: no cover
        raise NotImplementedError

    def bar(self, contract: dict, i: int) -> tuple[float, float, float, float] | None:  # pragma: no cover
        raise NotImplementedError


class EstimatedPremiums(PremiumSource):
    def __init__(self, cfg: OptionsBacktestConfig, idx: pd.DataFrame):
        self.cfg, self.idx = cfg, idx
        self.iv = cfg.iv_pct if cfg.iv_pct else index_cfg(cfg.index)["iv"]
        self.step = index_cfg(cfg.index)["step"]

    def contract_for(self, ts, spot, option_type):
        exp = estimated_expiry(self.cfg.index, ts.date(), self.cfg.min_days_to_expiry)
        strikes = [round(spot / self.step) * self.step + k * self.step for k in range(-15, 16)]
        strike = pick_strike(strikes, spot, option_type, self.cfg.strike_method, self.cfg.strike_offset)
        return {"tradingsymbol": f"{self.cfg.index}{exp:%y%b%d}{int(strike)}{option_type}".upper(),
                "strike": strike, "option_type": option_type, "expiry": exp.isoformat(), "estimated": True}

    def bar(self, contract, i):
        r = self.idx.iloc[i]
        ts = r["date"].to_pydatetime()
        exp = date.fromisoformat(contract["expiry"])
        yrs = years_to_expiry(ts, exp)
        k, ot = contract["strike"], contract["option_type"]
        def px(s):
            return bs_price(float(s), k, yrs, self.iv, ot)
        o, c = px(r["open"]), px(r["close"])
        a, b = px(r["high"]), px(r["low"])
        return (o, max(a, b, o, c), min(a, b, o, c), c)


class RealPremiums(PremiumSource):
    """chain: list of dicts (strike, instrument_type, instrument_token, tradingsymbol, lot_size)
    for the chosen listed expiry; fetch(token) -> list of candles (same interval)."""

    def __init__(self, cfg: OptionsBacktestConfig, idx: pd.DataFrame, chain_rows: list[dict],
                 fetch: Callable[[int], list[dict]]):
        self.cfg, self.idx, self.fetch = cfg, idx, fetch
        self.chain = chain_rows
        self.cache: dict[int, dict] = {}

    def contract_for(self, ts, spot, option_type):
        rows = [r for r in self.chain if r["instrument_type"] == option_type]
        strike = pick_strike([float(r["strike"]) for r in rows], spot, option_type, self.cfg.strike_method, self.cfg.strike_offset)
        r = next(r for r in rows if float(r["strike"]) == strike)
        return {"tradingsymbol": r["tradingsymbol"], "strike": strike, "option_type": option_type,
                "expiry": self.cfg.expiry, "instrument_token": int(r["instrument_token"]), "estimated": False}

    def _series(self, token: int) -> dict:
        if token not in self.cache:
            rows = self.fetch(token) or []
            m = {}
            for c in rows:
                t = pd.Timestamp(c["date"])
                if t.tzinfo is not None:
                    t = t.tz_convert("Asia/Kolkata").tz_localize(None)
                m[t] = (float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))
            self.cache[token] = m
        return self.cache[token]

    def bar(self, contract, i):
        return self._series(contract["instrument_token"]).get(self.idx["date"].iloc[i])


def run_options_backtest(index_candles, cfg: OptionsBacktestConfig, source: PremiumSource | None = None,
                         cancel: threading.Event | None = None) -> dict:
    cfg.validate()
    risk = RiskConfig.from_dict(cfg.risk)
    idx = prepare_candles(index_candles)
    if len(idx) < 30:
        raise ValueError("Not enough index candles")
    if not (idx["date"].dt.normalize() != idx["date"]).any():
        raise ValueError("Options backtests need intraday candles (e.g. 5minute)")
    if cfg.lot_size < 1:
        raise ValueError("Lot size unavailable")
    src = source or EstimatedPremiums(cfg, idx)
    sig = get_strategy(cfg.strategy).signals(idx, cfg.params)
    e_l, x_l, e_s, x_s = (sig[k].to_numpy() for k in ("entry_long", "exit_long", "entry_short", "exit_short"))
    times = idx["date"].dt.time.to_numpy()
    days = idx["date"].dt.date.to_numpy()
    dates = idx["date"].to_numpy()
    o_i = idx["open"].to_numpy()
    es_t, ee_t, ex_t = parse_hm(cfg.entry_start), parse_hm(cfg.entry_end), parse_hm(cfg.exit_time)
    qty = cfg.lots * cfg.lot_size
    pside = 1 if cfg.side == "BUY" else -1  # +1 long option, -1 short option
    slip = cfg.slippage_bps / 10_000

    cash = cfg.initial_capital
    pos: dict | None = None
    pending: dict | None = None
    trades: list[dict] = []
    skipped: list[dict] = []
    equity = np.empty(len(idx))
    day_trades: dict = {}
    day_pnl: dict = {}
    last_prem = 0.0

    def option_type_for(direction: int) -> str | None:
        if cfg.option_rule == "CE":
            want = "CE"
        elif cfg.option_rule == "PE":
            want = "PE"
        else:
            want = None
        # long options: bullish -> CE, bearish -> PE; short options: bullish -> sell PE, bearish -> sell CE
        auto = ("CE" if direction > 0 else "PE") if pside > 0 else ("PE" if direction > 0 else "CE")
        if want and want != auto:
            return None  # signal direction doesn't match the restricted option type
        return auto

    def close(i: int, raw: float, reason: str):
        nonlocal pos, cash
        px = raw * (1 - slip * pside)
        q = pos["qty"]
        gross = (px - pos["entry"]) * q * pside
        buy_val = (pos["entry"] if pside > 0 else px) * q
        sell_val = (px if pside > 0 else pos["entry"]) * q
        ch = round_trip_charges("options", buy_val, sell_val, "BSE" if index_cfg(cfg.index)["opt_exchange"] == "BFO" else "NSE")["total"]
        net = gross - ch
        cash += net
        d = days[i]
        day_pnl[d] = day_pnl.get(d, 0.0) + net
        trades.append({
            "side": "LONG" if pside > 0 else "SHORT",
            "action": ("BUY " if pside > 0 else "SELL ") + pos["contract"]["option_type"],
            "contract": pos["contract"]["tradingsymbol"], "strike": pos["contract"]["strike"],
            "option_type": pos["contract"]["option_type"], "expiry": pos["contract"]["expiry"],
            "lots": cfg.lots, "qty": q,
            "entry_time": pd.Timestamp(pos["time"]).isoformat(), "entry_price": round(pos["entry"], 2),
            "exit_time": pd.Timestamp(dates[i]).isoformat(), "exit_price": round(px, 2),
            "stop_loss": pos["stop0"], "target": pos["target"], "final_stop": round(pos["stop"], 2),
            "index_at_entry": round(pos["spot"], 2),
            "gross_pnl": round(gross, 2), "charges": round(ch, 2), "net_pnl": round(net, 2),
            "risk": round(pos["risk_unit"] * q, 2),
            "r_multiple": round(net / (pos["risk_unit"] * q), 2) if pos["risk_unit"] else None,
            "return_pct": round((px - pos["entry"]) / pos["entry"] * 100 * pside, 2),
            "exit_reason": reason, "estimated": pos["contract"].get("estimated", False),
        })
        pos = None

    for i in range(len(idx)):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        d, t = days[i], times[i]
        last_bar = i == len(idx) - 1 or days[i + 1] != d

        # 1a) signal exit queued at the previous close -> fill at this open
        if pos is not None and pos.get("exit_next_open"):
            b = src.bar(pos["contract"], i)
            close(i, b[0] if b else last_prem, "signal_exit")

        # 1b) pending entry at this candle's open
        if pending is not None and pos is None:
            p = pending
            pending = None
            ts = pd.Timestamp(dates[i]).to_pydatetime()
            try:
                contract = src.contract_for(ts, float(o_i[i]), p["option_type"])
                b = src.bar(contract, i) if contract else None
            except (ValueError, StopIteration) as e:
                skipped.append({"time": ts.isoformat(), "reason": str(e)})
                contract, b = None, None
            if contract is None or b is None:
                if contract is not None:
                    skipped.append({"time": ts.isoformat(), "reason": f"No premium data for {contract['tradingsymbol']}"})
            else:
                entry = b[0] * (1 + slip * pside)
                try:
                    lv = compute_levels(risk, entry, pside)
                    problems = validate_position(risk, lv, qty)
                    if problems:
                        raise RiskError("; ".join(problems))
                    pos = {"contract": contract, "entry": entry, "time": dates[i], "spot": float(o_i[i]),
                           "stop": lv.stop, "stop0": lv.stop, "target": lv.target, "risk_unit": lv.risk_per_unit,
                           "best": entry, "qty": qty}
                    day_trades[d] = day_trades.get(d, 0) + 1
                except RiskError as e:
                    skipped.append({"time": ts.isoformat(), "reason": f"Risk check: {e}"})

        # 2) manage an open position on this candle
        if pos is not None:
            b = src.bar(pos["contract"], i)
            if b is None:
                b = (last_prem, last_prem, last_prem, last_prem)
            po, ph, pl, pc = b
            if t >= ex_t:
                close(i, po, "time_exit")
            elif pside > 0:
                if pl <= pos["stop"]:
                    close(i, min(po, pos["stop"]), "stop_loss" if pos["stop"] <= pos["stop0"] else "trailing_stop")
                elif pos["target"] is not None and ph >= pos["target"]:
                    close(i, max(po, pos["target"]), "target")
            else:
                if ph >= pos["stop"]:
                    close(i, max(po, pos["stop"]), "stop_loss" if pos["stop"] >= pos["stop0"] else "trailing_stop")
                elif pos["target"] is not None and pl <= pos["target"]:
                    close(i, min(po, pos["target"]), "target")
            if pos is not None:
                last_prem = pc
                pos["best"] = max(pos["best"], ph) if pside > 0 else min(pos["best"], pl)
                pos["stop"] = trail_stop(risk, pside, pos["entry"], pos["stop"], pos["risk_unit"], pos["best"])
                if last_bar:
                    close(i, pc, "end_of_day")

        # 3) signals at this candle's close -> next open (only if next open is inside the window)
        if i < len(idx) - 1 and not last_bar:
            nt = times[i + 1]
            bull, bear = bool(e_l[i]), bool(e_s[i])
            if pos is not None:
                # option direction of the open position: long CE / short PE = bullish
                pos_dir = 1 if (pos["contract"]["option_type"] == "CE") == (pside > 0) else -1
                exit_sig = (pos_dir > 0 and (x_l[i] or bear)) or (pos_dir < 0 and (x_s[i] or bull))
                if exit_sig:
                    pos["exit_next_open"] = True  # filled at the next candle's open (no look-ahead)
                    if not cfg.reverse_on_opposite:
                        bull = bear = False
            if (pos is None or (cfg.reverse_on_opposite and pos.get("exit_next_open"))) and (bull or bear) and es_t <= nt < ee_t:
                lost = day_pnl.get(d, 0.0)
                if day_trades.get(d, 0) >= cfg.max_trades_per_day:
                    skipped.append({"time": pd.Timestamp(dates[i]).isoformat(), "reason": "Max trades per day reached"})
                elif cfg.max_daily_loss is not None and lost <= -abs(cfg.max_daily_loss):
                    skipped.append({"time": pd.Timestamp(dates[i]).isoformat(), "reason": "Daily loss limit reached"})
                else:
                    ot = option_type_for(1 if bull else -1)
                    if ot:
                        pending = {"option_type": ot}

        if pos is not None:
            b = src.bar(pos["contract"], i)
            mark = b[3] if b else last_prem
            equity[i] = cash + (mark - pos["entry"]) * pos["qty"] * pside
        else:
            equity[i] = cash

    eq = pd.Series(equity, index=pd.DatetimeIndex(dates))
    m = compute_metrics(eq, trades, cfg.initial_capital)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    m.update({
        "winning_trades": len(wins), "losing_trades": len(losses),
        "gross_profit": round(sum(t["net_pnl"] for t in wins), 2),
        "gross_loss": round(sum(t["net_pnl"] for t in losses), 2),
        "avg_rr_realised": round(m["avg_win"] / abs(m["avg_loss"]), 2) if m["avg_loss"] else None,
        "planned_rr": RiskConfig.from_dict(cfg.risk).target_value if RiskConfig.from_dict(cfg.risk).target_mode == "rr" else None,
        "exits": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in sorted({t["exit_reason"] for t in trades})},
        "signals_skipped": len(skipped),
        "bars": len(idx), "start": idx["date"].iloc[0].isoformat(), "end": idx["date"].iloc[-1].isoformat(),
        "premium_source": cfg.premium_source,
    })
    step = max(1, len(eq) // 400)
    peak = eq.cummax()
    return to_python({
        "config": asdict(cfg),
        "metrics": m,
        "equity_curve": [{"t": t.isoformat(), "equity": round(float(v), 2)} for t, v in eq.iloc[::step].items()],
        "drawdown_curve": [{"t": t.isoformat(), "dd": round(float(v), 2)} for t, v in ((eq / peak - 1) * 100).iloc[::step].items()],
        "trades": trades,
        "skipped": skipped[-200:],
    })
