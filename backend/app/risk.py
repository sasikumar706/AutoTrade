"""Mandatory stop-loss / target / trailing-stop rules, shared by backtest, paper and live.

Prices here are whatever the position is priced in — for options that is the PREMIUM.
`side` is +1 for a long position (bought) and -1 for a short position (sold).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass
class RiskConfig:
    sl_mode: str = "pct"  # pct | points | price
    sl_value: float = 20.0
    target_mode: str = "rr"  # rr | pct | points | price | none
    target_value: float = 2.1  # rr: reward = 2.1 x risk  (2.1:1 risk-to-reward, NOT 2.1% win rate)
    trail_mode: str = "none"  # none | pct | points
    trail_value: float = 0.0
    trail_activate_rr: float = 1.0  # start trailing once profit >= this many R (0 = immediately)
    breakeven_rr: float = 0.0  # once profit >= this many R, move the stop to the entry price (0 = off): no loss after that
    lock_trigger_rs: float = 1000.0  # profit lock (per lot, ₹): once a lot is up this much...
    lock_rs: float = 500.0  # ...the stop moves to keep at least this much per lot; falls back to it → sold (0 = off)
    lock_step_rs: float = 500.0  # then every further ₹500 per lot raises the lock by ₹500 (₹1,500 → keep ₹1,000, ₹2,000 → ₹1,500…)
    max_risk_per_trade: float | None = None  # rupees; entry rejected if qty*risk exceeds it
    min_sl_pct: float = 2.0  # stop closer than this % of entry is rejected (noise)
    max_sl_pct: float = 60.0  # stop farther than this % of entry is rejected

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskConfig":
        d = d or {}
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and d[k] is not None}
        return cls(**known)


def round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


@dataclass
class Levels:
    entry: float
    stop: float
    target: float | None
    risk_per_unit: float
    reward_per_unit: float | None

    @property
    def rr(self) -> float | None:
        return round(self.reward_per_unit / self.risk_per_unit, 2) if self.reward_per_unit and self.risk_per_unit else None


class RiskError(ValueError):
    pass


def compute_levels(cfg: RiskConfig, entry: float, side: int, tick: float = 0.05) -> Levels:
    if entry <= 0:
        raise RiskError("Entry price must be positive")
    if cfg.sl_mode == "pct":
        dist = entry * cfg.sl_value / 100
    elif cfg.sl_mode == "points":
        dist = cfg.sl_value
    elif cfg.sl_mode == "price":
        dist = (entry - cfg.sl_value) * side
    else:
        raise RiskError("Stop-loss mode must be pct, points or price")
    if not dist or dist <= 0 or math.isnan(dist):
        raise RiskError("Stop-loss is mandatory and must be on the losing side of the entry")
    stop = round_tick(entry - side * dist, tick)
    if side > 0 and stop <= 0:
        raise RiskError("Stop-loss would be at or below zero")
    dist = abs(entry - stop)
    pct = dist / entry * 100
    if pct < cfg.min_sl_pct:
        raise RiskError(f"Stop-loss too tight ({pct:.1f}% < {cfg.min_sl_pct}%)")
    if pct > cfg.max_sl_pct:
        raise RiskError(f"Stop-loss too wide ({pct:.1f}% > {cfg.max_sl_pct}%)")

    tm, tv = cfg.target_mode, cfg.target_value
    if tm == "none":
        target, reward = None, None
    else:
        if tm == "rr":
            reward = dist * tv
        elif tm == "pct":
            reward = entry * tv / 100
        elif tm == "points":
            reward = tv
        elif tm == "price":
            reward = (tv - entry) * side
        else:
            raise RiskError("Target mode must be rr, pct, points, price or none")
        if reward <= 0:
            raise RiskError("Target must be on the profit side of the entry")
        target = round_tick(entry + side * reward, tick)
        if target <= 0:
            raise RiskError("Target would be at or below zero")
        reward = abs(target - entry)
    return Levels(entry=entry, stop=stop, target=target, risk_per_unit=dist, reward_per_unit=reward)


def validate_position(cfg: RiskConfig, lv: Levels, qty: int) -> list[str]:
    """Return a list of problems; empty means OK."""
    problems = []
    if qty <= 0:
        problems.append("Quantity must be positive")
    if cfg.max_risk_per_trade is not None and lv.risk_per_unit * qty > cfg.max_risk_per_trade + 1e-9:
        problems.append(f"Risk ₹{lv.risk_per_unit * qty:,.0f} exceeds max risk per trade ₹{cfg.max_risk_per_trade:,.0f}")
    return problems


def locked_rs(cfg: RiskConfig, best_profit_per_lot: float) -> float:
    """Rupees per lot the profit lock keeps, from the best profit per lot so far (0 = not yet / off).
    Defaults: ₹1,000 → keep ₹500; then each ₹500 more raises it ₹500 (₹1,500 → ₹1,000, ₹2,000 → ₹1,500, ...)."""
    trig, keep = cfg.lock_trigger_rs or 0, min(cfg.lock_rs or 0, cfg.lock_trigger_rs or 0)
    if not trig or not keep or best_profit_per_lot < trig:
        return 0.0
    step = cfg.lock_step_rs or 0
    if step > 0:
        keep += math.floor((best_profit_per_lot - trig) / step + 1e-9) * step
    return float(keep)


def trail_stop(cfg: RiskConfig, side: int, entry: float, stop: float, risk_per_unit: float,
               best_price: float, tick: float = 0.05, lot_size: int = 0) -> float:
    """Return the (possibly tightened) stop given the best price seen so far. Never loosens.
    With lot_size: profit lock — once 1 lot is up ₹lock_trigger_rs, the stop moves to lock ₹lock_rs per lot."""
    profit = (best_price - entry) * side
    keep = locked_rs(cfg, profit * lot_size) if lot_size else 0.0
    if keep:
        lk = round_tick(entry + side * keep / lot_size, tick)
        stop = max(stop, lk) if side > 0 else min(stop, lk)
    if cfg.breakeven_rr and risk_per_unit and profit >= cfg.breakeven_rr * risk_per_unit:
        be = round_tick(entry, tick)
        stop = max(stop, be) if side > 0 else min(stop, be)
    if cfg.trail_mode == "none" or cfg.trail_value <= 0:
        return stop
    if profit < cfg.trail_activate_rr * risk_per_unit:
        return stop
    dist = best_price * cfg.trail_value / 100 if cfg.trail_mode == "pct" else cfg.trail_value
    cand = round_tick(best_price - side * dist, tick)
    return max(stop, cand) if side > 0 else min(stop, cand)
