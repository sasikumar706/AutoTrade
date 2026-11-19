"""Auto Trade (morning index-options workflow) + manual option trades + analytics.

Daily flow per AutoTrader (all times Asia/Kolkata, editable):
  09:15-09:30 ANALYSIS : collect data, previous-day levels, opening gap; NO orders.
  09:30-10:30 ENTRY    : on each closed candle, a confirmed strategy signal -> contract
                         selection -> risk validation -> entry (max N per day).
  10:30       CUTOFF   : no new entries; every managed position is exited (enforced even if
                         the strategy is paused/disabled); pending entries cancelled.
  The window is editable (e.g. full day 09:30-14:45, exit by 15:10 — before Zerodha's MIS square-off).
  strategy_source="ai": each morning the walk-forward finder ranks every strategy on the index's
  recent candles and the best ROBUST one (profitable on unseen data) trades today; none = WAIT.
  after       SESSION_COMPLETED once the broker confirms nothing is left open.
"""
from __future__ import annotations

import math
import threading
import time as _time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import indicators as ind
from .candles import Candle, CandleBuilder
from .market_calendar import is_trading_day, parse_hm, phase
from .options import (bs_delta, chain, daily_levels, expiries, implied_vol, index_cfg, nearest_future, pivots, select_contract,
                      spot_instrument, years_to_expiry)
from .positions import ACTIVE_STATES, ManagedPosition, PositionSpec
from .risk import RiskConfig, RiskError
from .strategies import REGISTRY, get_strategy

LATEST_EXIT = "15:15"  # Zerodha squares off F&O MIS around 15:20-15:25; exit before that
# history used by the AI picker per candle size (enough for a train/test split)
# counter-trend strategies buy weakness / sell strength, so the trend-agreement confidence
# score is low by design for them; it is shown but not used as a gate
REVERSION = {"rsi_reversion", "bollinger"}
# The AI picker tests strategies on the index itself but scores them like ONE option lot:
# an ATM option moves about half as much as the index (delta ~0.5), and a round trip costs
# roughly ₹60 in charges + ~₹90 slippage. Scoring with stock charges on a few index units
# made costs ~9x too heavy and rejected nearly every strategy.
OPTION_DELTA = 0.5
OPTION_ROUND_TRIP_COST = 150.0
STRICT_MIN_TRADES = 15
STRICT_MIN_PF = 1.2
PRACTICE_MIN_TRADES = 5
OPTION_SLIPPAGE_PCT = 1.0  # % of premium lost per side to spread/slippage when buying ATM options (practitioner guide)
EXPIRY_DAYS_LEFT = {"weekly": 3.0, "monthly": 10.0}  # average trading days to expiry, for premium/theta estimates


def atm_premium(spot: float, vix: float | None, days_left: float) -> float:
    sigma = (vix if vix and vix > 0 else 14.0) / 100
    return 0.4 * spot * sigma * math.sqrt(days_left / 365)


def option_round_trip_cost(index: str, spot: float, lot: int, vix: float | None = None, method: str = "ATM",
                           slip_pct: float | None = None) -> float:
    """₹ for one lot bought and sold: Zerodha's real charges on an estimated premium + slippage both ways."""
    from .charges import round_trip_charges
    c = index_cfg(index)
    prem = atm_premium(spot, vix, EXPIRY_DAYS_LEFT.get(c.get("expiry_rule"), 3.0))
    if (method or "").upper() == "ITM":
        prem += c["step"] * 0.6  # one strike in the money: about one step of intrinsic value
    elif (method or "").upper() == "OTM":
        prem = max(prem - c["step"] * 0.4, prem * 0.5)
    value = prem * lot
    charges = round_trip_charges("options", value, value, "BSE" if c["opt_exchange"] == "BFO" else "NSE")["total"]
    return round(charges + 2 * (OPTION_SLIPPAGE_PCT if slip_pct is None else slip_pct) / 100 * value, 2)


def delta_for(method: str) -> float:
    """Approximate option delta by strike choice (1 strike from the money)."""
    return {"ITM": 0.6, "OTM": 0.4}.get((method or "ATM").upper(), OPTION_DELTA)


def luck_threshold(trials: int) -> float:
    """t-statistic the best of N no-edge trials reaches by luck alone (expected maximum of N normals)."""
    n = max(2, trials)
    return round(math.sqrt(2 * math.log(n)), 2)


def option_theta_per_bar(spot: float, minutes: int, lot: int, vix: float | None = None, days_left: float = 3.0) -> float:
    """₹ an ATM option of one lot loses to time decay per candle (Black-Scholes approximation).

    ATM premium ~ 0.4 * S * sigma * sqrt(T); theta per day ~ premium / (2 T). A weekly option has on
    average ~3 trading days left (monthly ~10); the session is 375 minutes long.
    """
    premium = atm_premium(spot, vix, days_left)
    per_day = premium / (2 * days_left)
    return round(per_day / 375 * minutes * lot, 2)


def strict_robust(r: dict) -> bool:
    t = r["test"]
    pf = t.get("profit_factor")
    return bool(r.get("robust")) and t["trades"] >= STRICT_MIN_TRADES and (pf is None or pf >= STRICT_MIN_PF)
DEFAULT_LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}  # 2026 sizes; real ones come from Kite
AI_LOOKBACK_DAYS = {"minute": 25, "3minute": 60, "5minute": 90, "10minute": 150, "15minute": 200}


@dataclass
class AutoConfig:
    mode: str = "paper"  # paper | live
    index: str = "NIFTY"
    strategy: str = "morning_momentum"
    params: dict[str, Any] = field(default_factory=dict)
    strategy_source: str = "manual"  # manual | ai (AI finder picks the best proven strategy each day)
    use_combo: bool = True  # AI pick: run the top 2-3 proven strategies together if the combination tests better
    entry_source: str = "strategy"  # what decides entries for the AI / Autopilot: strategy = the best of all strategies
    #                                 (Trend Score included) | indicators = only Trend Score (the 6 indicator checks)
    autopilot: bool = False  # AI also chooses index, candle size, stop/target and lots; user sets only mode + money limits
    interval: str = "5minute"
    side: str = "BUY"  # BUY options (default) | SELL options (needs allow_option_selling)
    option_rule: str = "AUTO"  # AUTO | CE | PE
    expiry: str = "NEAREST"  # NEAREST | YYYY-MM-DD
    skip_expiry_day: bool = True  # on expiry day trade the next expiry (gamma risk)
    strike_method: str = "ATM"
    strike_offset: int = 1
    lots: int = 1
    product: str = "MIS"
    risk: dict = field(default_factory=lambda: RiskConfig().to_dict())
    analysis_start: str = "09:15"
    entry_start: str = "09:30"
    entry_end: str = "10:30"
    exit_time: str = "10:30"
    max_trades_per_day: int = 10
    max_daily_loss: float = 2000.0
    max_capital: float = 50_000.0  # max premium outlay per trade
    max_spread_pct: float = 3.0  # reject if (ask-bid)/mid above this
    max_open_gap_pct: float = 2.0  # WAIT all day if the index gaps more than this (0 = off)
    signal_valid_seconds: int = 90  # also: how long a live LIMIT entry may wait before it is cancelled
    fit_lots_to_limits: bool = True  # reduce lots (never increase) so an order fits max capital / risk budget
    join_window_minutes: int = 15  # enter a move that started up to N minutes ago (0 = only fresh signals)
    trend_reentry: bool = True  # move already underway (too late / too far to join): wait for a pullback to the 20-EMA
    #                             and join on the bounce failing, while the trend is intact (up to 2 tries per move)
    # Strong-trend override: enter without the strategy's own signal when at least this many of the
    # 6 indicator checks agree on a closed candle (0 = off). Not part of the backtested results.
    trend_override_min: int = 0  # off by default: not part of any tested result
    # Strategy agreement: enter when at least this many of the library strategies are in the same direction
    # (0 = off). Each strategy uses its best settings from today's AI test.
    consensus_min: int = 0  # off by default: similar indicators agreeing is mostly one signal counted many times
    day_builder: str = "paper"  # Day Builder: read today's market and build a strategy for the rest of it: off | paper | always
    day_build_times: str = "10:15,11:45,13:15"  # checkpoints (re-built when flat)
    trend_day_entry: str = "paper"  # Trend Day setup trades by itself (even with no AI pick): off | paper | always
    any_strategy_entry: bool = False  # also enter on a FRESH signal from any library strategy (exits by that strategy's rules)
    entry_tolerance_pts: float = 1.0  # option LIMIT entry: pay up to this many ₹ above the quote (sell: below)
    level_tolerance_pct: float = 0.0  # optional near-miss on breakout levels (index %); off by default
    min_confidence: int = 60
    allow_option_selling: bool = False
    slippage_bps: float = 75.0  # paper fills: 0.75% of premium per side (ATM option spreads + slippage)
    # ---- market filters (block NEW entries only; open positions are always managed)
    max_vix: float = 30.0  # no new entries while India VIX is above this (0 = off)
    min_adx: float = 15.0  # chop filter: trend entries need index ADX(14) at least this (0 = off; reversion exempt)
    skip_lunch: bool = True  # no new entries in the slow lunch hour
    lunch_start: str = "12:30"
    lunch_end: str = "13:15"
    # ---- smarter exits / re-pick
    time_stop_bars: int = 6  # exit if, after this many candles, the trade is not at least time_stop_min_r in profit (0 = off)
    time_stop_min_r: float = 0.25
    quick_target_pts: float = 20.0  # slow trade: after quick_target_after_bars candles without reaching the full target,
    #                                  bring the target down to entry + this many ₹ points on the option price (0 = off)
    quick_target_after_bars: int = 3
    quick_target_auto: bool = True  # size the quick target to the trade: the smaller of 0.8x the stop distance and
    #                                 15% of the premium, never below 2.5x the trade's costs (False = quick_target_pts)
    # ---- option-buyer checks
    iv_mode: str = "careful"  # option price (IV) check: careful (1 lot when IV is high, skip when extreme) | skip | off
    min_move_cost_ratio: float = 3.0  # the target must be at least this many times the round-trip cost per unit (0 = off)
    same_direction_mode: str = "small"  # a 2nd open trade betting the same way: small (1 lot) | block | off
    oi_walls: bool = True  # put the target just before a big open-interest wall; skip if the wall leaves no room
    scale_out: bool = True  # 2+ lots: book half at +1R, move the rest's stop to entry and let it run
    live_risk: bool = True  # re-check every open trade every ~10 s between candles (sharp moves, profit give-back)
    stretch_mode: str = "pullback"  # move already stretched (far from VWAP + RSI extreme): pullback = wait for a retest
    #                                 and enter on the bounce failing | skip | off
    pullback_wait_bars: int = 4
    midday_repick: str = "11:45"  # AI re-tests strategies at this time when flat ("" = off)
    # ---- strike / expiry / events
    autopilot_itm: bool = True  # Autopilot buys 1 strike in-the-money (more of each index point, less lost to decay/spread)
    expiry_day_mode: str = "careful"  # on the index's expiry day: normal | careful (1 lot, no new trades after 13:30) | skip
    event_day_mode: str = "skip"  # RBI / US Fed / Budget days: off | careful (1 lot) | skip
    max_open_positions: int = 2  # keep looking for new setups while a trade is open (1 = one trade at a time)
    reentry_after_exit: bool = True
    smart_exit: bool = True  # AI manages open trades each candle: exits when the market turns, locks profit, lets strong moves run
    cooldown_after_loss_bars: int = 2  # after a losing exit, wait this many candles before a new entry (stops whipsaw)  # after a trade closes, the same setup type may enter again on a fresh candle
    # ---- automatic money management (Autopilot): the app decides capital use, risk, lots and limits
    autopilot_index: str = "ANY"  # Autopilot: "ANY" = AI chooses among NIFTY/BANKNIFTY/SENSEX, or one fixed index
    auto_money: bool = True
    paper_capital: float = 300_000.0  # PAPER: pretend account size (LIVE uses the real Kite funds)
    max_auto_lots: int = 10  # automatic money: the most lots one trade may use (risk usually allows fewer)
    # ---- money safety
    account_capital: float = 0.0  # your trading capital ₹ (0 = not set); each trade risks at most risk_per_trade_pct of it
    risk_per_trade_pct: float = 1.0
    max_weekly_loss: float = 0.0  # stop new trades for the week after this loss ₹ (0 = off)
    max_monthly_loss: float = 0.0  # stop new trades for the month after this loss ₹ (0 = off)
    max_consecutive_losses: int = 3  # stop for the day after this many losing trades in a row (0 = off)
    paper_practice: bool = True  # PAPER only: when nothing passes the strict proof, practise with the best that still
    #                              made money on unseen data after real costs (trades are labelled PRACTICE in Stats)
    auto_resume_paper: bool = True  # PAPER only: switch back on by itself after a server restart (LIVE always stays off)
    strict_proof: bool = True  # AI pick must have >= 15 unseen-data trades, profit factor >= 1.2 and survive double costs
    live_needs_luck_check: bool = True  # LIVE only trades a pick whose unseen-data result beats what luck alone gives

    def validate(self):
        index_cfg(self.index)
        get_strategy(self.strategy).resolve(self.params)
        if self.mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if self.side == "SELL" and not self.allow_option_selling:
            raise PermissionError("Option selling is disabled for this strategy")
        if self.autopilot_index not in ("ANY", "NIFTY", "BANKNIFTY", "SENSEX"):
            raise ValueError("Autopilot index must be ANY, NIFTY, BANKNIFTY or SENSEX")
        if self.autopilot and self.autopilot_index != "ANY":
            self.index = self.autopilot_index
        if not (1 <= self.max_trades_per_day <= 20):
            raise ValueError("Max trades per day must be 1-20")
        if self.lots < 1 or self.max_trades_per_day < 1:
            raise ValueError("Lots and max trades per day must be at least 1")
        if self.consensus_min not in (0, 2, 3, 4, 5):
            raise ValueError("Strategy agreement must be off (0) or 2-5 strategies")
        if self.trend_override_min not in (0, 4, 5, 6):
            raise ValueError("Strong-trend override must be off (0) or 4, 5 or 6 of 6 checks")
        if not (0 <= self.max_open_gap_pct <= 10):
            raise ValueError("Opening-gap limit must be 0-10 % (0 = off)")
        if not (0 <= self.join_window_minutes <= 120):
            raise ValueError("Join window must be 0-120 minutes")
        if not (0 <= self.entry_tolerance_pts <= 10):
            raise ValueError("Entry price tolerance must be between ₹0 and ₹10")
        if not (0 <= self.level_tolerance_pct <= 1):
            raise ValueError("Level tolerance must be between 0 and 1 %")
        if not (0 <= self.max_vix <= 80) or not (0 <= self.min_adx <= 60):
            raise ValueError("VIX limit must be 0-80 and ADX minimum 0-60 (0 = off)")
        if self.day_builder not in ("off", "paper", "always"):
            raise ValueError("Day Builder must be off, paper or always")
        try:
            [parse_hm(x.strip()) for x in self.day_build_times.split(",") if x.strip()]
        except Exception:
            raise ValueError("Day Builder times must be HH:MM separated by commas")
        if self.trend_day_entry not in ("off", "paper", "always"):
            raise ValueError("Trend-day entry must be off, paper or always")
        if self.expiry_day_mode not in ("normal", "careful", "skip") or self.event_day_mode not in ("off", "careful", "skip"):
            raise ValueError("Expiry-day mode must be normal/careful/skip and event-day mode off/careful/skip")
        if not (0 <= self.cooldown_after_loss_bars <= 12):
            raise ValueError("Cool-down after a loss must be 0-12 candles")
        if not (1 <= self.max_open_positions <= 3):
            raise ValueError("Open positions at once must be 1-3")
        if self.paper_capital < 10_000:
            raise ValueError("Paper capital must be at least ₹10,000")
        if self.account_capital < 0 or not (0.1 <= self.risk_per_trade_pct <= 5):
            raise ValueError("Capital must be ₹0 or more and risk per trade 0.1-5 %")
        if self.max_weekly_loss < 0 or self.max_monthly_loss < 0 or not (0 <= self.max_consecutive_losses <= 20):
            raise ValueError("Weekly/monthly loss limits must be ₹0 or more; losing-streak stop 0-20")
        if not (0 <= self.time_stop_bars <= 60) or not (0 <= self.time_stop_min_r <= 3):
            raise ValueError("Time-stop must be 0-60 candles and 0-3 R")
        if not (0 <= self.quick_target_pts <= 500) or not (1 <= self.quick_target_after_bars <= 60):
            raise ValueError("Quick target must be 0-500 points, after 1-60 candles")
        if self.iv_mode not in ("careful", "skip", "off") or self.same_direction_mode not in ("small", "block", "off"):
            raise ValueError("IV check must be careful/skip/off and same-direction rule small/block/off")
        if not (1 <= self.max_auto_lots <= 50):
            raise ValueError("Max lots per trade must be 1-50")
        if self.entry_source not in ("strategy", "indicators"):
            raise ValueError("Entries must come from strategy or indicators")
        if self.stretch_mode not in ("pullback", "skip", "off") or not (1 <= self.pullback_wait_bars <= 12):
            raise ValueError("Stretch rule must be pullback/skip/off, waiting 1-12 candles")
        if not (0 <= self.min_move_cost_ratio <= 20):
            raise ValueError("Move-vs-cost ratio must be 0-20")
        try:
            if self.skip_lunch and parse_hm(self.lunch_start) >= parse_hm(self.lunch_end):
                raise ValueError("Lunch start must be before lunch end")
            if self.midday_repick:
                parse_hm(self.midday_repick)
        except ValueError as e:
            raise ValueError(str(e) if "Lunch" in str(e) else "Lunch / re-pick times must be HH:MM")
        if self.autopilot:
            self.strategy_source, self.side, self.option_rule, self.allow_option_selling = "ai", "BUY", "AUTO", False
            self.fit_lots_to_limits = True  # lots = "max lots"; each order uses as many as the limits allow
            r = self.risk or {}
            if r.get("sl_mode") == "points" and r.get("trail_mode", "none") == "none":  # older Autopilot settings
                self.risk = {**r, "trail_mode": "points", "trail_value": r.get("sl_value", 0), "trail_activate_rr": 1.0}
            if self.interval not in AI_LOOKBACK_DAYS:
                self.interval = "5minute"
        if self.strategy_source not in ("manual", "ai"):
            raise ValueError("strategy_source must be manual or ai")
        if self.strategy_source == "ai" and self.interval not in AI_LOOKBACK_DAYS:
            raise ValueError("AI strategy pick supports 1m-15m candles")
        try:
            ts = [parse_hm(x) for x in (self.analysis_start, self.entry_start, self.entry_end, self.exit_time)]
        except Exception:
            raise ValueError("Times must be HH:MM, e.g. 09:30")
        if not (parse_hm("09:15") <= ts[0] <= ts[1] < ts[2] <= ts[3]):
            raise ValueError("Times must satisfy 09:15 <= analysis start <= entry start < entry end <= exit time")
        if ts[3] > parse_hm(LATEST_EXIT):
            raise ValueError(f"Exit time must be {LATEST_EXIT} or earlier (Zerodha auto square-off for intraday)")
        RiskConfig.from_dict(self.risk)


@dataclass
class Deps:
    instruments: Callable[[], pd.DataFrame]
    quote: Callable[[list[str]], dict]
    history: Callable[[int, str, datetime, datetime], list[dict]]
    kite_getter: Callable[[], Any]
    margin_available: Callable[[], float] | None = None
    order_margin: Callable[[dict], float] | None = None
    data_dir: Any = None


def _best_prices(q: dict) -> tuple[float | None, float | None, float | None]:
    ltp = q.get("last_price")
    depth = q.get("depth") or {}
    bid = (depth.get("buy") or [{}])[0].get("price") or None
    ask = (depth.get("sell") or [{}])[0].get("price") or None
    return ltp, bid, ask


def confidence_for(df: pd.DataFrame, direction: int) -> tuple[int, list[str]]:
    """Share of independent confirmations agreeing with the direction (0-100) + reasons."""
    c = df["close"]
    last = c.iloc[-1]
    checks = []
    e20, e50 = ind.ema(c, 20).iloc[-1], ind.ema(c, 50).iloc[-1]
    checks.append(((e20 > e50) == (direction > 0), f"EMA20 {'>' if e20 > e50 else '<'} EMA50"))
    r = ind.rsi(c, 14).iloc[-1]
    checks.append(((r > 50) == (direction > 0), f"RSI {r:.0f}"))
    vw = ind.session_vwap(df).iloc[-1]
    checks.append(((last > vw) == (direction > 0), f"price {'above' if last > vw else 'below'} VWAP/TWAP {vw:.1f}"))
    lv = daily_levels(df).iloc[-1]
    if pd.notna(lv.get("prev_high")):
        pv = pivots(lv["prev_high"], lv["prev_low"], lv["prev_close"])["P"]
        checks.append(((last > pv) == (direction > 0), f"price {'above' if last > pv else 'below'} prev-day pivot {pv}"))
    _, _, hist = ind.macd(c)
    checks.append(((hist.iloc[-1] > 0) == (direction > 0), f"MACD hist {hist.iloc[-1]:.2f}"))
    a, pdi, mdi = ind.adx(df, 14)
    checks.append((a.iloc[-1] >= 20 and ((pdi.iloc[-1] > mdi.iloc[-1]) == (direction > 0)), f"ADX {a.iloc[-1]:.0f}"))
    ok = [m for good, m in checks if good]
    return round(len(ok) / len(checks) * 100), ok


def confirmations(df: pd.DataFrame, direction: int) -> list[dict]:
    """The same checks as confidence_for(), in plain words with pass/fail, for the 'why' list."""
    c = df["close"]
    last = c.iloc[-1]
    up = direction > 0
    e20, e50 = ind.ema(c, 20).iloc[-1], ind.ema(c, 50).iloc[-1]
    r = ind.rsi(c, 14).iloc[-1]
    vw = ind.session_vwap(df).iloc[-1]
    _, _, hist = ind.macd(c)
    a, pdi, mdi = ind.adx(df, 14)
    items = [
        ((e20 > e50) == up, f"Short-term trend {'up' if e20 > e50 else 'down'} (EMA20 {'>' if e20 > e50 else '<'} EMA50)"),
        ((r > 50) == up, f"Momentum RSI {r:.0f} ({'above' if r > 50 else 'below'} 50)"),
        ((last > vw) == up, f"Price {'above' if last > vw else 'below'} today's average (VWAP {vw:,.0f})"),
        ((hist.iloc[-1] > 0) == up, f"MACD {'rising' if hist.iloc[-1] > 0 else 'falling'}"),
        (a.iloc[-1] >= 20 and ((pdi.iloc[-1] > mdi.iloc[-1]) == up),
         f"Trend strength ADX {a.iloc[-1]:.0f} ({'weak' if a.iloc[-1] < 20 else 'strong, buyers leading' if pdi.iloc[-1] > mdi.iloc[-1] else 'strong, sellers leading'})"),
    ]
    lv = daily_levels(df).iloc[-1]
    if pd.notna(lv.get("prev_high")):
        pv = pivots(lv["prev_high"], lv["prev_low"], lv["prev_close"])["P"]
        items.append(((last > pv) == up, f"Price {'above' if last > pv else 'below'} yesterday's pivot {pv:,.0f}"))
    return [{"group": "indicators", "ok": bool(ok), "label": lab} for ok, lab in items]


class AutoTrader:
    def __init__(self, cfg: AutoConfig, deps: Deps, register_position: Callable[[ManagedPosition], None],
                 on_change: Callable[["AutoTrader"], None] | None = None, aid: str | None = None):
        cfg.validate()
        self.id = aid or uuid.uuid4().hex[:8]
        self.cfg, self.deps = cfg, deps
        self.register_position = register_position
        self.on_change = on_change
        self.lock = threading.RLock()
        self.enabled = False
        self.paused = False
        self.state = "IDLE"
        self.message = "Disabled"
        self.day: date | None = None
        self.trades_today = 0
        self.day_pnl = 0.0
        self.day_blocked: str | None = None
        self.pnl_days: dict[str, float] = {}  # closed-trade net ₹ per day, for weekly/monthly limits
        self.loss_streak = 0
        self.skips: dict[str, dict[str, int]] = {}
        self.day_build: dict | None = None  # today's Day Builder result
        self.versions: list[dict] = [{"v": 1, "since": datetime.now().isoformat(timespec="minutes"), "changes": []}]
        self.iv_hist: dict[str, list] = {}  # index -> [[day, IV %], ...] samples of the traded option's IV
        self._oi_cache: dict = {}
        self.pending_pullback: dict | None = None  # a stretched signal waiting for a retest before entering
        self._spot_trail: list[tuple[datetime, float]] = []  # recent index ticks for the live risk monitor
        self._risk_at: dict[str, float] = {}
        self._agree: dict[str, int] = {}  # position id -> % of indicators agreeing at the last candle
        self.building = False  # day -> {reason category: count}
        self.position: ManagedPosition | None = None  # the latest position
        self.others: list[ManagedPosition] = []  # earlier positions still being managed (multi-position)
        self.position_ids_today: list[str] = []
        self.processed: list[str] = []
        self.signals: list[dict] = []
        self.timeline: list[dict] = []
        self.history = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        self.builder = CandleBuilder(cfg.interval)
        self.spot: dict | None = None
        self.last_spot: float | None = None
        self.last_tick: datetime | None = None
        self.levels: dict = {}
        self.today_open: float | None = None
        self.pick: dict | None = None  # AI-picked strategy for the day
        self.picking = False
        self.pick_progress: dict | None = None
        self.run_async = True  # tests run the AI pick inline
        self.pos_strategy: tuple[str, dict] | None = None  # strategy that opened the current position
        self.watch: dict | None = None  # live "what happens next" view, refreshed as the price moves
        self._watch_at = 0.0
        self._preview_at = 0.0

    # ------------------------------------------------------------ utils
    NOTIFY_KINDS = {"trade": ("default", "moneybag"), "alert": ("high", "warning")}

    def log(self, msg: str, kind: str = "info"):
        self.timeline.append({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "msg": msg})
        self.timeline = self.timeline[-150:]
        self._changed()
        important = kind in self.NOTIFY_KINDS or (kind == "risk" and any(w in msg for w in ("limit", "Time-stop", "cutoff")))
        if important and getattr(self, "notify_enabled", True):
            try:
                from . import notify
                pr, tags = self.NOTIFY_KINDS.get(kind, ("default", "stopwatch"))
                notify.send(f"Kite Algo {self.cfg.mode.upper()} {self.cfg.index}", msg, pr, tags)
            except Exception:
                pass

    def _changed(self):
        if self.on_change:
            try:
                self.on_change(self)
            except Exception:
                pass

    def _set(self, state: str, msg: str = ""):
        if state != self.state or (msg and msg != self.message):
            self.state, self.message = state, msg or self.message
            self.log(f"{state}{': ' + msg if msg else ''}", "state")

    @property
    def effective_state(self) -> str:
        if self.position is not None and self.position.state in ACTIVE_STATES:
            return self.position.state
        return self.state

    def wanted_tokens(self) -> list[int]:
        toks = []
        if self.spot and (self.enabled or bool(self.active_positions())):
            toks.append(self.spot["instrument_token"])
        return toks

    @staticmethod
    def _label(key: str, params: dict | None) -> str:
        if key == "combo":
            from .strategies import COMBO
            try:
                return "Combo (" + COMBO.title(params) + ")"
            except Exception:
                return "Combo"
        return get_strategy(key).name

    def _p(self, params: dict) -> dict:
        """Strategy params + the configured level tolerance."""
        return {**(params or {}), "level_tol_pct": self.cfg.level_tolerance_pct}

    @property
    def active_strategy(self) -> tuple[str, dict] | None:
        if self.cfg.strategy_source == "ai":
            return (self.pick["strategy"], self.pick["params"]) if self.pick and self.pick.get("strategy") else None
        return self.cfg.strategy, self.cfg.params

    def start_pick(self, now: datetime):
        """Rank all strategies on recent index candles; trade the best robust one today."""
        if self.cfg.strategy_source != "ai" or self.picking:
            return
        # a re-pick keeps trading the current strategy until the new test finishes (it can take minutes on a phone)
        keep = getattr(self, "_keep_pick", None)
        self.picking, self.pick = True, (keep if keep and keep.get("day") == now.date().isoformat() else None)
        self.pick_progress = {"stage": "Loading recent candles", "started": datetime.now().isoformat(timespec="seconds")}
        self.log("AI is testing every strategy on recent data (including today's candles so far) to pick today's best…", "ai")
        if self.run_async:
            threading.Thread(target=self._pick, args=(now,), daemon=True).start()
        else:
            self._pick(now)

    AUTOPILOT_INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")
    AUTOPILOT_INTERVALS = ("5minute", "15minute")
    ATR_STOP_MULT = 1.5  # autopilot stop = 1.5 x ATR of the index, target = 2 x the stop

    def _evaluate(self, index: str, interval: str, now: datetime, label: str) -> dict:
        """Walk-forward test of every strategy on one index + candle size, scored like ONE option lot."""
        from .analysis import find_strategies
        from .backtest import BacktestConfig
        from .candles import INTERVAL_MINUTES
        from .charges import segment_for

        inst = self.deps.instruments()
        spot = spot_instrument(inst, index)
        days = AI_LOOKBACK_DAYS.get(interval, 75)
        self.pick_progress = {**(self.pick_progress or {}), "stage": f"{label}: loading {days} days of candles"}
        rows = self.deps.history(spot["instrument_token"], interval, now - timedelta(days=days), now)
        mins = INTERVAL_MINUTES[interval]  # completed candles only — includes today's so far
        rows = [r for r in rows if pd.Timestamp(r["date"]).tz_localize(None) + pd.Timedelta(minutes=mins) <= pd.Timestamp(now)]
        try:
            fut = nearest_future(inst, index, now.date())
            lot = int(fut["lot_size"]) if fut else DEFAULT_LOT.get(index, 75)
        except Exception:
            lot = DEFAULT_LOT.get(index, 75)
        method = self.trade_strike_method()
        from .quality import calibrated_slippage_pct
        slip_pct, slip_src = calibrated_slippage_pct(self.deps.data_dir, OPTION_SLIPPAGE_PCT)
        units = max(1, round(lot * delta_for(method)))  # one lot
        stop_pct = target_pct = atr_pts = None
        if self.cfg.autopilot and len(rows) > 30:
            df = pd.DataFrame(rows)
            atr_pts = float(ind.atr(df, 14).iloc[-1])
            px = float(df["close"].iloc[-1])
            stop_pct = round(self.ATR_STOP_MULT * atr_pts / px * 100, 3)
            target_pct = round(2 * stop_pct, 3)
        # same trading window as Auto Trade, so the pick is judged on the hours it will trade
        base = BacktestConfig(strategy="sma_cross", product="MIS", segment=segment_for("MIS", "EQ"),
                              exchange="BSE" if index == "SENSEX" else "NSE", lot_size=1, allow_short=True,
                              sizing="fixed_qty", quantity=units,
                              fixed_cost=option_round_trip_cost(index, float(rows[-1]["close"]), lot, self.vix(), method, slip_pct) if rows else OPTION_ROUND_TRIP_COST,
                              slippage_bps=1.0, no_entry_after=self.cfg.entry_end, squareoff_time=self.cfg.exit_time,
                              stop_loss_pct=stop_pct, target_pct=target_pct,
                              time_stop_bars=self.cfg.time_stop_bars, time_stop_min_r=self.cfg.time_stop_min_r,
                              no_entry_window=(self.cfg.lunch_start, self.cfg.lunch_end) if self.cfg.skip_lunch else None,
                              min_adx=self.cfg.min_adx,
                              bar_cost=option_theta_per_bar(float(rows[-1]["close"]), mins, lot, self.vix(),
                                                             EXPIRY_DAYS_LEFT.get(index_cfg(index).get("expiry_rule"), 3.0)) if rows else 0.0)

        def prog(i, n, nm):
            self.pick_progress = {**(self.pick_progress or {}), "stage": f"{label}: testing {i} of {n} — {nm}"}
        pool = ["trend_score"] if self.cfg.entry_source == "indicators" else list(REGISTRY)
        res = find_strategies(rows, base, strategies=pool, extra_params={"level_tol_pct": self.cfg.level_tolerance_pct},
                              progress=prog)
        ev = {"slip": (slip_pct, slip_src), "res": res, "units": units, "lot": lot, "atr": atr_pts, "stop_pct": stop_pct, "rows": rows, "base": base,
              "price": float(rows[-1]["close"]) if rows else None}
        for r in res["ranking"]:
            r["index"], r["interval"], r["_ev"] = index, interval, ev
            if self.cfg.strict_proof and r["robust"] and not strict_robust(r):
                r["robust"], r["weak"] = False, True
        return ev

    def _pick(self, now: datetime):
        t0 = _time.monotonic()
        try:
            if self.cfg.autopilot:
                ixs = self.AUTOPILOT_INDICES if self.cfg.autopilot_index == "ANY" else (self.cfg.autopilot_index,)
                cands = [(ix, iv) for ix in ixs for iv in self.AUTOPILOT_INTERVALS]
            else:
                cands = [(self.cfg.index, self.cfg.interval)]
            evals, combos = [], 0
            for n, (ix, iv) in enumerate(cands, 1):
                label = f"{ix} {iv.replace('minute', '-min')} ({n}/{len(cands)})" if len(cands) > 1 else ix
                try:
                    ev = self._evaluate(ix, iv, now, label)
                except Exception as e:
                    self.log(f"Skipped {ix} {iv}: {e}"[:200], "alert")
                    continue
                evals.append(ev)
                combos += ev["res"]["combinations_tested"]
            if not evals:
                raise ValueError("no index could be tested")
            ranking = [r for ev in evals for r in ev["res"]["ranking"]]
            ranking.sort(key=lambda r: (bool(r.get("robust")), r.get("score") is not None, r.get("score") or -1e9), reverse=True)
            robust = [r for r in ranking if r["robust"]]
            if self.cfg.strict_proof and robust:
                # stress test: the pick must still make money on unseen data with costs DOUBLED
                passed = None
                for r in robust[:4]:
                    self.pick_progress = {**(self.pick_progress or {}), "stage": f"Double-cost check: {r['name']}"}
                    st = self._stress(r, r["_ev"])
                    r["stress"] = st
                    if st is not None and st["test_pnl"] > 0:
                        passed = r
                        break
                    r["robust"], r["failed_stress"] = False, True
                robust = [passed] if passed else []
            if not robust and self.cfg.mode == "paper" and self.cfg.paper_practice:
                cands = [r for r in ranking if r["test"]["trades"] >= PRACTICE_MIN_TRADES and r["test"]["net_pnl"] > 0]
                if cands:
                    cands[0]["practice"] = True
                    robust = [cands[0]]
            n_trials = len(REGISTRY) * 2 * len(evals)  # 2 best settings per strategy reach the unseen-data test
            multi = len(cands) > 1
            tested = []
            for r in ranking[:12]:
                t, tr = r["test"], r["train"]
                why = ("PRACTICE pick (paper only) — made money on unseen data after real costs, but failed the strict proof"
                       if r.get("practice")
                       else "PASSED — made money in both periods" if r["robust"]
                       else (f"lost money when costs were doubled (₹{(r.get('stress') or {}).get('test_pnl', 0):,.0f}) — "
                             f"its profit is too thin to survive real-world slippage") if r.get("failed_stress")
                       else "fewer than 3 trades on unseen data" if t["trades"] < 3
                       else (f"too thin a proof: needs {STRICT_MIN_TRADES}+ unseen trades and profit factor "
                             f"{STRICT_MIN_PF}+ (got {t['trades']}, PF {t.get('profit_factor') or '—'})") if r.get("weak")
                       else "lost money on unseen data" if t["net_pnl"] <= 0
                       else "lost money in the tuning period")
                tested.append({"strategy": r["strategy"], "params": r["params"], "robust": r["robust"],
                               "name": r["name"] + (f" · {r['index']} {r['interval'].replace('minute', '-min')}" if multi else ""),
                               "index": r["index"], "interval": r["interval"],
                               "test": {k: t.get(k) for k in ("trades", "net_pnl", "win_rate_pct", "profit_factor")},
                               "train_return_pct": tr["total_return_pct"], "why": why,
                               "description": REGISTRY[r["strategy"]].description})
            with self.lock:
                if robust:
                    b = robust[0]
                    ev = b["_ev"]
                    if (self.cfg.autopilot or self.cfg.use_combo) and not b.get("practice"):
                        try:
                            cb = self._try_combo(b, ev)
                        except Exception as e:
                            cb = None
                            self.log(f"Combo test failed ({e}); using the single best"[:200], "alert")
                        if cb:
                            b = cb
                    changed = multi and (b["index"] != self.cfg.index or b["interval"] != self.cfg.interval)
                    if self.cfg.autopilot:
                        try:
                            ev["tuned"] = self._tune_risk(b, ev)
                        except Exception as e:
                            ev["tuned"] = None
                            self.log(f"Stop/target tuning failed ({e}); using 1.5×ATR / 2×"[:200], "alert")
                        self._apply_autopilot(b, ev)
                    self.pick = {
                        "strategy": b["strategy"], "name": b["name"], "params": b["params"], "test": b["test"], "train": b["train"],
                        "description": b.get("description") or REGISTRY[b["strategy"]].description, "tested": tested,
                        "lot_units": ev["units"], "combo": b["strategy"] == "combo",
                        "test_period": ev["res"]["test_period"], "combinations_tested": combos,
                        "picked_at": datetime.now().isoformat(timespec="seconds"), "day": now.date().isoformat(),
                        "seconds": round(_time.monotonic() - t0, 1), "index": b["index"], "interval": b["interval"],
                        "autopilot": self.cfg.autopilot, "risk": dict(self.cfg.risk) if self.cfg.autopilot else None,
                        "risk_tuning": ev.get("tuned"), "atr": ev.get("atr"),
                        "applied": ({k: (dict(v) if isinstance(v, dict) else v) for k, v in
                                     (("index", self.cfg.index), ("interval", self.cfg.interval), ("strategy", self.cfg.strategy),
                                      ("params", self.cfg.params), ("risk", self.cfg.risk), ("strike_method", self.cfg.strike_method),
                                      ("strike_offset", self.cfg.strike_offset))} if self.cfg.autopilot else None),
                        "costs": {"round_trip": ev["base"].fixed_cost, "theta_per_candle": ev["base"].bar_cost,
                                  "slippage_pct_per_side": ev.get("slip", (OPTION_SLIPPAGE_PCT,))[0],
                                  "slippage_source": ev.get("slip", (None, "standard estimate"))[1]},
                        "stress": b.get("stress"),
                        "luck": self._luck(b, n_trials),
                        "practice": bool(b.get("practice")),
                    }
                    self._keep_pick = None
                    if b.get("practice"):
                        self.log("No strategy passed the strict proof today, so PAPER practises with the best one that still made money "
                                 "on unseen data after real costs. These trades are labelled PRACTICE in Stats. LIVE would WAIT.", "ai")
                    where = f" on {b['index']} {b['interval'].replace('minute', '-min')} candles" if multi else ""
                    self.log(f"AI picked {b['name']} {b['params']}{where}: on unseen data ₹{b['test']['net_pnl']:,.0f} per lot over "
                             f"{b['test']['trades']} trades, win {b['test']['win_rate_pct']}% (tried {combos} settings)", "ai")
                    if self.cfg.autopilot:
                        r = self.cfg.risk
                        self.log(f"Autopilot set: {self.cfg.index}, {self.cfg.interval.replace('minute', '-min')} candles, buy CE on up-signals / "
                                 f"PE on down-signals, ATM strike, stop {r['sl_value']} pts on the premium, target {r['target_value']}× the stop, "
                                 f"trailing stop from 1× profit, "
                                 f"lots sized to your limits", "ai")
                    lk = self.pick["luck"]
                    self.log(f"Luck check: unseen-data t-score {lk['t_stat'] if lk['t_stat'] is not None else '—'} vs {lk['needed']} that the best "
                             f"of {lk['trials']} no-edge tries reaches by chance — {'PASSED' if lk['passed'] else 'not passed (could be luck)'}", "ai")
                    if self.cfg.mode == "live" and self.cfg.live_needs_luck_check and not lk["passed"]:
                        self.day_blocked = ("LIVE paused today: the AI's best result could be luck (failed the luck check). "
                                            "Paper mode still trades it for practice.")
                        self.log(self.day_blocked, "risk")
                    if changed:
                        self.day = None  # reload candles for the newly chosen index / candle size
                        self.start_day(now)
                    elif self.day and len(self.history):
                        self._review_today(self.history[self.history["date"].dt.date == self.day])
                elif getattr(self, "_keep_pick", None):
                    # midday re-test found nothing better: keep this morning's strategy instead of stopping
                    self.pick, self._keep_pick = self._keep_pick, None
                    self.log("Midday re-test found no better strategy — keeping this morning's pick", "ai")
                else:
                    self.pick = {"strategy": None, "name": None, "params": {}, "day": now.date().isoformat(),
                                 "picked_at": datetime.now().isoformat(timespec="seconds"), "tested": tested,
                                 "combinations_tested": combos}
                    self.day_blocked = "AI found no strategy that made money on unseen data — WAIT today"
                    self.log(self.day_blocked, "risk")
        except Exception as e:
            with self.lock:
                self.pick, self._keep_pick = getattr(self, "_keep_pick", None), None
                self.log(f"AI strategy pick failed: {e}"[:300], "alert")
        finally:
            self.picking = False
            self.pick_progress = None
            self._changed()

    def _stress(self, b: dict, ev: dict) -> dict | None:
        """Re-run the candidate on the same data with trading costs and time decay doubled."""
        from dataclasses import replace as dc_replace

        from .analysis import find_strategies
        base = ev["base"]
        base2 = dc_replace(base, fixed_cost=(base.fixed_cost or 0) * 2, bar_cost=base.bar_cost * 2)
        try:
            res = find_strategies(ev["rows"], base2, strategies=[b["strategy"]], grids={b["strategy"]: [b["params"]]},
                                  extra_params={"level_tol_pct": self.cfg.level_tolerance_pct})
        except Exception:
            return None
        if not res["ranking"]:
            return None
        te = res["ranking"][0]["test"]
        return {"test_pnl": te["net_pnl"], "test_trades": te["trades"], "win_rate_pct": te["win_rate_pct"]}

    @staticmethod
    def _luck(b: dict, trials: int) -> dict:
        t = (b.get("test") or {}).get("t_stat")
        need = luck_threshold(trials)
        return {"t_stat": t, "needed": need, "trials": trials, "passed": bool(t is not None and t >= need)}

    COMBO_SIZE = 3
    COMBO_MIN_SHARE = 0.7  # the combo must keep at least 70% of the best single strategy's unseen-data profit

    def _try_combo(self, b: dict, ev: dict) -> dict | None:
        """Test the top 2-3 proven strategies (same index + candle size) TOGETHER; use them only if the
        combination itself made money on unseen data."""
        from .analysis import find_strategies
        from .strategies import COMBO

        seen, members = set(), []
        for r in ev["res"]["ranking"]:
            if r["robust"] and r["strategy"] not in seen:
                seen.add(r["strategy"])
                members.append({"strategy": r["strategy"], "params": dict(r["params"])})
            if len(members) == self.COMBO_SIZE:
                break
        if len(members) < 2:
            return None
        self.pick_progress = {**(self.pick_progress or {}), "stage": f"Testing the top {len(members)} together"}
        params = {"members": members}
        res = find_strategies(ev["rows"], ev["base"], strategies=["combo"], grids={"combo": [params]},
                              extra_params={"level_tol_pct": self.cfg.level_tolerance_pct})
        if not res["ranking"]:
            return None
        c = res["ranking"][0]
        title = COMBO.title(params)
        ok = c["robust"] and c["test"]["net_pnl"] >= self.COMBO_MIN_SHARE * b["test"]["net_pnl"]
        self.log(f"Combo test {title}: unseen data ₹{c['test']['net_pnl']:,.0f} per lot over {c['test']['trades']} trades "
                 f"vs best single ₹{b['test']['net_pnl']:,.0f} over {b['test']['trades']} — "
                 f"{'USING the combo' if ok else 'keeping the single best'}", "ai")
        if not ok:
            return None
        return {**c, "strategy": "combo", "params": params, "name": f"Combo: {title}", "index": b["index"],
                "interval": b["interval"], "_ev": ev, "robust": True,
                "description": "Any of these can open a trade; the trade is held while at least one still agrees: "
                               + " | ".join(f"{REGISTRY[m['strategy']].name}: {REGISTRY[m['strategy']].description}" for m in members)}

    STOP_MULTS = (1.0, 1.5, 2.0)  # stop distance in ATRs of the index
    TARGET_RRS = (0.75, 1.0, 1.5, 2.0)  # target as a multiple of the stop (smaller = hit more often)
    BREAKEVENS = (0.0, 0.5)  # move the stop to entry after 0.5R in profit (0 = off)

    def _tune_risk(self, b: dict, ev: dict) -> dict | None:
        """Find the best stop/target numbers for the chosen strategy: each stop × target pair is tested on the
        tuning period, the best is chosen there (expectancy, weighted towards a higher win rate) and then
        checked on the unseen period. Kept only if it also made money there."""
        from dataclasses import replace as dc_replace

        from .analysis import find_strategies
        atr, px = ev.get("atr"), ev.get("price")
        if not atr or not px:
            return None
        rows = []
        for sm, rr, be in [(a, b_, c) for a in self.STOP_MULTS for b_ in self.TARGET_RRS for c in self.BREAKEVENS]:
            if True:
                self.pick_progress = {**(self.pick_progress or {}), "stage": f"Testing stop {sm}×ATR / target {rr}× / break-even {be or 'off'}"}
                sp = sm * atr / px * 100
                base = dc_replace(ev["base"], stop_loss_pct=round(sp, 4), target_pct=round(sp * rr, 4),
                                  breakeven_at_pct=round(sp * be, 4) if be else None)
                try:
                    res = find_strategies(ev["rows"], base, strategies=[b["strategy"]], grids={b["strategy"]: [b["params"]]},
                                          extra_params={"level_tol_pct": self.cfg.level_tolerance_pct})
                except Exception:
                    continue
                if not res["ranking"]:
                    continue
                r = res["ranking"][0]
                tr, te = r["train"], r["test"]
                rows.append({"stop_mult": sm, "target_rr": rr, "breakeven_rr": be, "train_win": tr["win_rate_pct"], "train_ret": tr["total_return_pct"],
                             "train_trades": tr["trades"], "test_win": te["win_rate_pct"], "test_pnl": te["net_pnl"],
                             "test_trades": te["trades"]})
        if not rows:
            return None
        good = [r for r in rows if r["train_ret"] > 0 and r["train_trades"] >= 3] or rows
        # profit first, but strongly favour settings that win more often (smaller target / break-even stop)
        best = max(good, key=lambda r: r["train_ret"] * (r["train_win"] / 100) ** 1.5 if r["train_ret"] > 0 else r["train_ret"])
        best = {**best, "kept": best["test_pnl"] > 0 and best["test_trades"] >= 3}
        self.log(f"Best numbers for {b['name']}: stop {best['stop_mult']}×ATR, target {best['target_rr']}× the stop, "
                 f"break-even {('at ' + str(best['breakeven_rr']) + 'R') if best.get('breakeven_rr') else 'off'} → "
                 f"tuning win {best['train_win']}%, unseen data win {best['test_win']}% / ₹{best['test_pnl']:,.0f} per lot over "
                 f"{best['test_trades']} trades — {'USING these' if best['kept'] else 'not confirmed on unseen data, keeping 1.5×/2×'}", "ai")
        return {"best": best, "table": rows}

    def _apply_autopilot(self, b: dict, ev: dict):
        """Fill every trading decision from the AI result; the user only sets mode and money limits."""
        c = self.cfg
        c.index, c.interval = b["index"], b["interval"]
        c.strategy, c.params = b["strategy"], dict(b["params"])
        c.side, c.option_rule, c.product = "BUY", "AUTO", "MIS"
        c.strike_method, c.strike_offset = ("ITM", 1) if c.autopilot_itm else ("ATM", 1)
        tuned = ev.get("tuned") or {}
        bt = tuned.get("best") if tuned.get("best", {}).get("kept") else None
        mult = bt["stop_mult"] if bt else self.ATR_STOP_MULT
        rr = bt["target_rr"] if bt else 2.0
        be = bt.get("breakeven_rr", 0.0) if bt else 0.5
        stop_pts = max(round(delta_for(c.strike_method) * mult * (ev["atr"] or 0), 1), 1.0)
        # once the trade is 1R in profit, the stop follows the best price at the same distance, so a reversal
        # books the profit automatically instead of waiting for the full target
        c.risk = {**RiskConfig().to_dict(), "sl_mode": "points", "sl_value": stop_pts, "target_mode": "rr", "target_value": rr,
                  "trail_mode": "points", "trail_value": stop_pts, "trail_activate_rr": 1.0, "breakeven_rr": be or 0.0,
                  "min_sl_pct": 1.0, "max_sl_pct": 60.0}

    # ------------------------------------------------------------ live prediction
    WATCH_EVERY_S = 5
    PREVIEW_EVERY_S = 20

    def update_watch(self, now: datetime, force: bool = False):
        """What the strategy sees on the candle forming right now, which way the checks lean,
        and the exact order it would place if its rule fires. Information only — never trades."""
        t = _time.monotonic()
        if not force and t - self._watch_at < self.WATCH_EVERY_S:
            return
        self._watch_at = t
        try:
            self.watch = self._build_watch(now, refresh_preview=force or t - self._preview_at >= self.PREVIEW_EVERY_S)
        except Exception as e:  # never let the preview break the trading loop
            self.watch = {"error": f"{type(e).__name__}: {e}"[:200], "as_of": now.isoformat(timespec="seconds")}

    def _build_watch(self, now: datetime, refresh_preview: bool) -> dict:
        from .analysis import current_signal

        act = self.active_strategy
        w: dict = {"as_of": now.isoformat(timespec="seconds"), "price": self.last_spot,
                   "prev_close": self.levels.get("prev_close")}
        if w["price"] and w["prev_close"]:
            w["change"] = round(w["price"] - w["prev_close"], 2)
            w["change_pct"] = round(w["change"] / w["prev_close"] * 100, 2)
        ph = phase(now, self.cfg.analysis_start, self.cfg.entry_start, self.cfg.entry_end, self.cfg.exit_time, self.deps.data_dir)
        w["phase"] = ph
        df = self.history
        cur = self.builder.current
        if cur is not None:  # include the candle that is still forming, so the view moves with the price
            row = pd.DataFrame([cur.as_dict()])
            row["date"] = pd.to_datetime(row["date"])
            df = pd.concat([df, row], ignore_index=True)
            w["candle_closes_at"] = (cur.date + timedelta(minutes=self.builder.minutes)).strftime("%H:%M")
        if len(df) < 60:
            w["status"] = "Collecting candles…"
            return w
        if act is None:
            bull, bear = confirmations(df, 1), confirmations(df, -1)
            nb, ns = sum(c["ok"] for c in bull), sum(c["ok"] for c in bear)
            w.update({"bull_checks": bull, "bear_checks": bear, "bull_score": nb, "bear_score": ns, "checks_total": len(bull),
                      "lean": "BULLISH" if nb >= ns + 2 else "BEARISH" if ns >= nb + 2 else "NEUTRAL", "likelihood": "NONE"})
            if self.picking:
                pp = self.pick_progress or {}
                secs = int((now - datetime.fromisoformat(pp["started"])).total_seconds()) if pp.get("started") else 0
                w["status"] = (f"AI is choosing today's strategy — {pp.get('stage', 'working')} ({secs}s so far). "
                               f"Indicators above already include today's candles since 09:15.")
            else:
                w["status"] = "No strategy to follow today" + (f" — {self.day_blocked}" if self.day_blocked else "")
            return w
        key, params = act
        name = self._label(key, params)
        cs = current_signal(df, key, self._p(params), allow_short=True)
        bull, bear = confirmations(df, 1), confirmations(df, -1)
        nb, ns, n = sum(c["ok"] for c in bull), sum(c["ok"] for c in bear), len(bull)
        lean = 1 if nb >= ns + 2 else -1 if ns >= nb + 2 else 0
        w.update({"strategy": name, "strategy_state": cs["action"], "since": cs["since"],
                  "bull_checks": bull, "bear_checks": bear, "bull_score": nb, "bear_score": ns, "checks_total": n,
                  "lean": "BULLISH" if lean > 0 else "BEARISH" if lean < 0 else "NEUTRAL"})
        at = w.get("candle_closes_at", "the next candle close")
        a = cs["action"]
        if a in ("BUY", "SELL"):
            ot = "CE" if a == "BUY" else "PE"
            w["status"] = f"{name}: {a} signal forming — buys {self.cfg.index} {ot} if it holds at {at}"
            direction = 1 if a == "BUY" else -1
            w["likelihood"] = "HIGH"
        elif a in ("HOLD LONG", "HOLD SHORT"):
            up = a == "HOLD LONG"
            since = (cs["since"] or "")[11:16]
            age_min = None
            if cs["since"]:
                st = pd.Timestamp(cs["since"]).to_pydatetime()
                age_min = (now - (st + timedelta(minutes=self.builder.minutes))).total_seconds() / 60 if st.date() == now.date() else None
            if age_min is not None and age_min <= self.cfg.join_window_minutes and f"late:{pd.Timestamp(cs['since']).to_pydatetime()}" not in self.processed:
                w["status"] = (f"{name}: {'up' if up else 'down'}-move at {since} ({age_min:.0f} min ago) — "
                               f"joins at the next check if limits allow")
            else:
                w["status"] = (f"{name}: {'up' if up else 'down'}-move since {since}, too old to chase — "
                               + (f"waits for a pullback to the 20-EMA or a fresh signal" if self.cfg.trend_reentry
                                  else "waits for a fresh signal"))
            direction = 1 if up else -1
            w["likelihood"] = "LOW"
        else:
            w["status"] = f"No signal yet from {name} · next check {at}"
            trig = self._trigger_hint(df, key, params, now)
            if trig:
                w["status"], w["trigger"] = trig["text"], trig
            # trend strategies whose state really carries across days (intraday ones restart every morning)
            carry = ({"position": "FLAT", "since": None} if trig or get_strategy(key).intraday_only or key == "combo"
                     else current_signal(df, key, self._p(params), allow_short=True, daily_reset=False))
            if carry["position"] != "FLAT" and carry["since"] and pd.Timestamp(carry["since"]).date() < now.date():
                up_c = carry["position"] == "LONG"
                w["status"] = (f"{name}: {'up' if up_c else 'down'} since {pd.Timestamp(carry['since']):%d %b} — "
                               f"waits for a fresh turn · next check {at}")
            direction = lean
            w["likelihood"] = "MEDIUM" if lean else "LOW"
        w["status_main"] = w.get("status", "")  # the app shows this as the headline; the rest as structured parts
        if self.cfg.consensus_min:
            cons = self.consensus()
            w["consensus"] = {**cons, "min": self.cfg.consensus_min}
            w["status"] += (f"\nStrategy agreement: UP {len(cons['up'])}/{cons['total']}"
                            + (f" ({', '.join(cons['up'])})" if cons["up"] else "")
                            + f" · DOWN {len(cons['down'])}/{cons['total']}"
                            + (f" ({', '.join(cons['down'])})" if cons["down"] else "")
                            + f" — enters at {self.cfg.consensus_min}+ on one side"
                            + (", or on any one strategy's fresh signal." if self.cfg.any_strategy_entry else "."))
            if max(len(cons["up"]), len(cons["down"])) >= self.cfg.consensus_min - 1 and w.get("likelihood") in ("LOW", "MEDIUM"):
                w["likelihood"] = "MEDIUM"
        pb = self.pending_pullback
        if pb:
            w["status"] += (f"\n⏳ Waiting for a pullback: the {pb['since']:%H:%M} {'BUY' if pb['direction'] > 0 else 'SELL'} signal was "
                            f"stretched (RSI {pb['rsi']:.0f}) — enters when price retests the 20-EMA and turns again, "
                            f"until {pb['until'] + timedelta(minutes=self.builder.minutes):%H:%M}.")
            w["pullback"] = {"direction": pb["direction"], "since": pb["since"].isoformat(), "until": pb["until"].isoformat(),
                             "reentry": bool(pb.get("reentry_of")),
                             "until_hhmm": (pb["until"] + timedelta(minutes=self.builder.minutes)).strftime("%H:%M")}
        if ph not in ("ANALYSIS", "ENTRY"):
            w["likelihood"] = "NONE"
            w["status"] += f" (outside the entry window {self.cfg.entry_start}–{self.cfg.entry_end}: no new orders)"
            w["window_closed"] = f"New orders only {self.cfg.entry_start}–{self.cfg.entry_end}"
        if not self.slot_free():
            w["likelihood"] = "NONE"
            w["status"] = "Managing the open trade — no new order until it closes"
            w["status_main"] = w["status"]
            direction = 0
        w["expected_direction"] = direction
        if direction and refresh_preview and self.last_spot:
            self._preview_at = _time.monotonic()
            sig, _, _ = self.build_signal(direction, None, now)
            w["expected_order"] = {k: sig.get(k) for k in (
                "action", "tradingsymbol", "strike", "expiry", "entry_price", "stop_loss", "target_price", "quantity",
                "lots", "est_max_loss", "risk_reward_ratio", "risk_check", "risk_problems", "confidence", "option_type", "entry_limit")}
            if sig.get("tradingsymbol"):
                ex = index_cfg(self.cfg.index)["opt_exchange"]
                self._watch_contract = f"{ex}:{sig['tradingsymbol']}"
        elif direction and self.watch and self.watch.get("expected_direction") == direction:
            w["expected_order"] = self.watch.get("expected_order")  # keep the last order preview between refreshes
        # live premium of the chosen strike, every watch refresh (~5 s)
        key = getattr(self, "_watch_contract", None)
        if direction and key and w.get("expected_order"):
            w["option"] = self._option_live(key)
        return w

    def _trigger_hint(self, df: pd.DataFrame, key: str, params: dict, now: datetime) -> dict | None:
        """The exact index level that would trigger the next order, where a strategy has one."""
        if key == "supertrend":
            return self._supertrend_hint(df, params)
        if key == "cpr_breakout":
            return self._cpr_hint(df, params, now)
        if key == "trend_day":
            return self._trend_day_hint(df, params, now)
        if key != "orb":
            return None
        p = get_strategy("orb").resolve(params)
        ts = pd.to_datetime(df["date"])
        today = df[ts.dt.date == now.date()]
        if not len(today):
            return None
        t0 = datetime.combine(now.date(), parse_hm("09:15"))
        rng_end = t0 + timedelta(minutes=int(p["range_minutes"]))
        in_rng = today[pd.to_datetime(today["date"]) < rng_end]
        if not len(in_rng):
            return None
        hi, lo = float(in_rng["high"].max()), float(in_rng["low"].min())
        px = self.last_spot or float(today["close"].iloc[-1])
        mins = self.builder.minutes
        out = {"range_high": round(hi, 2), "range_low": round(lo, 2), "range_end": rng_end.strftime("%H:%M"),
               "to_high_pts": round(hi - px, 1), "to_low_pts": round(px - lo, 1), "forming": now < rng_end}
        idx = self.cfg.index
        if out["forming"]:
            out["text"] = (f"Opening range forming until {out['range_end']}: {lo:,.0f}–{hi:,.0f} ({hi - lo:,.0f} pts). "
                           f"Close above → CE, below → PE")
        else:
            out["text"] = (f"Breakout watch: above {hi:,.0f} (+{max(out['to_high_pts'], 0):,.0f}) → CE · "
                           f"below {lo:,.0f} (−{max(out['to_low_pts'], 0):,.0f}) → PE")
        return out

    def _cpr_hint(self, df: pd.DataFrame, params: dict, now: datetime) -> dict | None:
        from .options import daily_levels
        p = get_strategy("cpr_breakout").resolve(params)
        lv = daily_levels(df)
        d = now.date()
        try:
            ph, pl, pc = float(lv["prev_high"][d]), float(lv["prev_low"][d]), float(lv["prev_close"][d])
        except Exception:
            return None
        piv = (ph + pl + pc) / 3
        bc = (ph + pl) / 2
        tc = 2 * piv - bc
        top, bot = max(tc, bc), min(tc, bc)
        width = (top - bot) / piv * 100
        idx, mins = self.cfg.index, self.builder.minutes
        px = self.last_spot or float(df["close"].iloc[-1])
        out = {"cpr_top": round(top, 2), "cpr_bottom": round(bot, 2), "width_pct": round(width, 3)}
        if width > p["max_width_pct"]:
            out["text"] = (f"CPR Breakout will NOT trade today: today's CPR is {width:.2f}% wide (top {top:,.2f}, bottom {bot:,.2f}) "
                           f"and this setting only trades narrow-CPR days (≤ {p['max_width_pct']}%). Other entry types and the "
                           f"Day Builder can still trade.")
            return out
        ts = pd.to_datetime(df["date"])
        today = df[ts.dt.date == d]
        done_up = bool(len(today) and (today["close"] > top).any())
        done_dn = bool(len(today) and (today["close"] < bot).any())
        parts = []
        if not done_up:
            parts.append(f"a {mins}-min close above {top:,.2f} ({max(top - px, 0):,.0f} pts away) → BUY {idx} CE")
        if not done_dn:
            parts.append(f"a close below {bot:,.2f} ({max(px - bot, 0):,.0f} pts away) → BUY {idx} PE")
        if not parts:
            out["text"] = "CPR Breakout already used today's first breakouts — no new CPR trade today."
        else:
            out["text"] = f"Narrow CPR today ({width:.2f}%). Waiting for " + "; or ".join(parts) + ". Only the first break each way counts."
        return out

    def _trend_day_hint(self, df: pd.DataFrame, params: dict, now: datetime) -> dict | None:
        from .options import daily_levels
        p = get_strategy("trend_day").resolve(params)
        try:
            pc = float(daily_levels(df)["prev_close"][now.date()])
        except Exception:
            return None
        px = self.last_spot or float(df["close"].iloc[-1])
        move = (px - pc) / pc * 100
        try:
            adx = float(ind.adx(df, 14)[0].iloc[-1])
        except Exception:
            adx = float("nan")
        need = p["move_pct"]
        status = "UP" if move >= need else "DOWN" if move <= -need else None
        txt = (f"Trend Day needs a move of ±{need}% from yesterday (now {move:+.2f}%), ADX ≥ {p['adx_min']:g} (now {adx:.0f}) "
               f"and price on the right side of VWAP. ")
        txt += (f"It's a strong {status} day — waiting for a dip to the EMA{int(p['ema'])} and a "
                f"{'green' if status == 'UP' else 'red'} bounce candle." if status and adx >= p["adx_min"]
                else "Not a trend day yet.")
        return {"move_pct": round(move, 2), "adx": round(adx, 1) if adx == adx else None, "text": txt}

    def _supertrend_hint(self, df: pd.DataFrame, params: dict) -> dict | None:
        p = get_strategy("supertrend").resolve(params)
        d, fu, fl = ind.supertrend_bands(df, int(p["period"]), float(p["multiplier"]))
        if pd.isna(d.iloc[-1]):
            return None
        up = d.iloc[-1] > 0
        flips = d[d != d.shift(1)].index
        since_i = flips[-1] if len(flips) else df.index[0]
        since = pd.Timestamp(df["date"].loc[since_i])
        lvl = float(fl.iloc[-1] if up else fu.iloc[-1])
        px = self.last_spot or float(df["close"].iloc[-1])
        mins, idx = self.builder.minutes, self.cfg.index
        gap = (px - lvl) if up else (lvl - px)
        text = (f"Supertrend is {'UP' if up else 'DOWN'} since {since:%d %b %H:%M} (its last turn in the loaded "
                f"{mins}-min candles). Its next order: a {mins}-min close {'BELOW' if up else 'ABOVE'} {lvl:,.2f} "
                f"({gap:,.0f} pts away) turns it {'DOWN → BUY ' + idx + ' PE' if up else 'UP → BUY ' + idx + ' CE'}. "
                f"That line moves {'up' if up else 'down'} with the price; it only trades at a turn.")
        return {"text": text, "level": round(lvl, 2), "direction": "UP" if up else "DOWN", "points_away": round(gap, 1),
                "since": since.isoformat()}

    def _record_quote(self, q: dict):
        """Keep our own option quote history (ltp/bid/ask once a minute) for honest backtests later —
        Kite can't give candles for expired options and only quotes show the real spread."""
        if not self.deps.data_dir or not q.get("ltp"):
            return
        now = datetime.now()
        key = (q["symbol"], now.strftime("%H:%M"))
        if getattr(self, "_last_rec", None) == key:
            return
        self._last_rec = key
        try:
            from pathlib import Path
            d = Path(self.deps.data_dir) / "option_quotes"
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{now:%Y-%m-%d}.csv"
            new = not f.exists()
            with f.open("a") as fh:
                if new:
                    fh.write("time,symbol,ltp,bid,ask,spot\n")
                fh.write(f"{now:%H:%M:%S},{q['symbol']},{q['ltp']},{q.get('bid') or ''},{q.get('ask') or ''},{self.last_spot or ''}\n")
        except Exception:
            pass

    def _option_live(self, key: str) -> dict | None:
        try:
            q = self.deps.quote([key]).get(key) or {}
        except Exception:
            return None
        ltp, bid, ask = _best_prices(q)
        ohlc = q.get("ohlc") or {}
        pc = ohlc.get("close")
        out = {"symbol": key.split(":", 1)[1], "ltp": ltp, "bid": bid, "ask": ask, "prev_close": pc,
               "open": ohlc.get("open"), "high": ohlc.get("high"), "low": ohlc.get("low"),
               "at": datetime.now().isoformat(timespec="seconds")}
        if ltp and pc:
            out["change"] = round(ltp - pc, 2)
            out["change_pct"] = round((ltp - pc) / pc * 100, 2)
        self._record_quote(out)
        prev = (self.watch or {}).get("option") or {}
        if prev.get("symbol") == out["symbol"] and prev.get("ltp") and ltp:
            out["tick"] = round(ltp - prev["ltp"], 2)  # move since the last refresh
        return out

    # ------------------------------------------------------------ day setup
    def start_day(self, now: datetime):
        try:
            m = self.apply_auto_money(now)
            if m:
                self.log(f"Money (automatic): capital ₹{m['capital']:,.0f} ({m['source']}) · risk ₹{m['risk_per_trade']:,.0f} per trade "
                         f"({m['risk_pct']}%) · stop for the day at −₹{m['day_stop']:,.0f}"
                         + (f" · lower risk today: {', '.join(m['why_lower'])}" if m["why_lower"] else ""), "risk")
        except Exception as e:
            self.log(f"Automatic money check failed: {e}"[:200], "alert")
        same_day = getattr(self, "counts_for", None) == now.date()
        self.day = self.counts_for = now.date()
        if not same_day:  # a settings edit mid-day must not reset today's trade count or loss
            self.trades_today, self.day_pnl, self.day_blocked = 0, 0.0, None
            self.position_ids_today, self.processed = [], []
            self.loss_streak = 0
        self.today_open = None
        self.builder = CandleBuilder(self.cfg.interval)
        inst = self.deps.instruments()
        self.spot = spot_instrument(inst, self.cfg.index)
        try:
            rows = self.deps.history(self.spot["instrument_token"], self.cfg.interval, now - timedelta(days=12), now)
        except Exception as e:
            rows = []
            self.log(f"Could not load history: {e}", "alert")
        hist = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        mins = self.builder.minutes
        partial = None
        if len(hist):
            hist["date"] = pd.to_datetime(hist["date"]).dt.tz_localize(None)
            # keep today's COMPLETED candles too, so a mid-day start (or a server restart) reads what
            # already happened since 09:15 instead of waiting for new candles to build up
            ends = hist["date"] + pd.Timedelta(minutes=mins)
            done_mask = (hist["date"].dt.date < self.day) | (ends <= pd.Timestamp(now))
            forming = hist[~done_mask & (hist["date"].dt.date == self.day)]
            if len(forming):
                partial = forming.iloc[-1]
            hist = hist[done_mask].reset_index(drop=True)
        self.history = hist
        self.levels = {}
        prev = hist[hist["date"].dt.date < self.day] if len(hist) else hist
        today = hist[hist["date"].dt.date == self.day] if len(hist) else hist
        if len(prev):
            last_day = prev["date"].dt.date.max()
            d = prev[prev["date"].dt.date == last_day]
            h, l, c = float(d["high"].max()), float(d["low"].min()), float(d["close"].iloc[-1])
            self.levels = {"prev_day": last_day.isoformat(), "prev_open": float(d["open"].iloc[0]), "prev_high": h,
                           "prev_low": l, "prev_close": c, **pivots(h, l, c)}
        self.log(f"New trading day {self.day}: previous-day levels {self.levels or 'unavailable'}")
        # the candle still forming continues from its real open/high/low, not from the first tick we see
        if partial is not None:
            self.builder.current = Candle(partial["date"].to_pydatetime(), float(partial["open"]), float(partial["high"]),
                                          float(partial["low"]), float(partial["close"]), 0.0)
        first = today.iloc[0] if len(today) else partial
        if first is not None:
            self._set_open(float(first["open"]))
        if len(today):
            self.last_spot = float((partial if partial is not None else today.iloc[-1])["close"])
            self.log(f"Read today's {len(today)} completed candle(s) {today['date'].iloc[0]:%H:%M}–"
                     f"{today['date'].iloc[-1] + pd.Timedelta(minutes=mins):%H:%M}: indicators, opening range and the strategy's "
                     f"current state already include them", "ai")
            self._review_today(today)
        if self.cfg.strategy_source == "ai":
            if self.pick and self.pick.get("day") == now.date().isoformat() and self.pick.get("strategy"):
                self.log(f"Using today's AI pick from earlier: {self.pick['name']}", "ai")
                self._review_today(today)
            else:
                self.start_pick(now)

    # ------------------------------------------------------------ inputs
    def _set_open(self, price: float):
        self.today_open = price
        pc = self.levels.get("prev_close")
        if pc:
            gap = (price - pc) / pc * 100
            self.levels["gap_pct"] = round(gap, 2)
            if self.cfg.max_open_gap_pct and abs(gap) > self.cfg.max_open_gap_pct:
                self.day_blocked = f"Opening gap {gap:+.2f}% exceeds {self.cfg.max_open_gap_pct}% — WAIT today"
                self.log(self.day_blocked, "risk")

    def _review_today(self, today: pd.DataFrame):
        """Back-check: which entry signals the strategy gave earlier today (for the log; no orders)."""
        act = self.active_strategy
        if act is None or len(self.history) < 60:
            return
        try:
            sig = get_strategy(act[0]).signals(self.history, self._p(act[1]))
            idx = self.history.index[self.history["date"].dt.date == self.day]
            hits = [(self.history["date"].iat[i], "BUY → CE" if sig["entry_long"].iat[i] else "SELL → PE")
                    for i in idx if sig["entry_long"].iat[i] or sig["entry_short"].iat[i]]
            name = self._label(*act)
            if hits:
                self.log(f"Back-check: {name} gave {len(hits)} signal(s) earlier today — "
                         + ", ".join(f"{t:%H:%M} {w}" for t, w in hits[-5:])
                         + ". Past signals are not traded late; the next one will be.", "ai")
            else:
                self.log(f"Back-check: {name} gave no signal earlier today", "ai")
        except Exception as e:
            self.log(f"Back-check failed: {e}"[:200], "alert")

    def on_index_tick(self, ts: datetime, price: float):
        with self.lock:
            self.last_spot, self.last_tick = price, ts
            self._spot_trail.append((ts, price))
            if len(self._spot_trail) > 400 or (self._spot_trail and (ts - self._spot_trail[0][0]).total_seconds() > 300):
                cut = ts - timedelta(seconds=300)
                self._spot_trail = [x for x in self._spot_trail if x[0] >= cut][-400:]
            if self.today_open is None and ts.time() >= parse_hm("09:15"):
                self._set_open(price)
            done = self.builder.update(ts, price)
            if done is not None:
                self._on_candle(done.as_dict(), ts)

    def on_clock(self, now: datetime):
        with self.lock:
            if self.day != now.date() and (self.enabled or bool(self.active_positions())):
                if not self.active_positions():
                    self.start_day(now)
            done = self.builder.flush_if_due(now)
            if done is not None:
                self._on_candle(done.as_dict(), now)
            ph = phase(now, self.cfg.analysis_start, self.cfg.entry_start, self.cfg.entry_end, self.cfg.exit_time, self.deps.data_dir)
            pos = self.position
            # cutoff exit is enforced regardless of enabled/paused
            if ph == "AFTER_CUTOFF" and pos is not None:
                if pos.is_open:
                    self.log("Exit cutoff reached — closing position", "risk")
                    pos.request_exit("time_cutoff")
                elif pos.state == "ENTRY_PENDING" and pos.mode == "live" and not getattr(pos, "cancel_sent", False):
                    self._cancel_entry(pos, "cutoff")
            if (pos is not None and pos.state == "ENTRY_PENDING" and pos.mode == "live" and pos.entry_limit is not None
                    and not getattr(pos, "cancel_sent", False)
                    and (now - datetime.fromisoformat(pos.created)).total_seconds() > self.cfg.signal_valid_seconds):
                self.log(f"Entry LIMIT {pos.entry_limit} not filled within {self.cfg.signal_valid_seconds}s — price ran away; cancelling", "risk")
                self._cancel_entry(pos, "limit not filled in time")
            if pos is not None and pos.state in ("EXIT_FAILED",) and pos.ltp is not None:
                pos.on_tick(pos.ltp)  # drives exit retries
            if ph != "AFTER_CUTOFF":
                for p in self.active_positions():
                    if p.is_open:
                        try:
                            self.live_risk(p, now)
                        except Exception as e:
                            self.log(f"Risk monitor error: {e}"[:200], "alert")
            self._account_closed()
            if not self.enabled:
                return
            self.maybe_midday_repick(now, ph)
            self.maybe_day_build(now, ph)
            self.update_watch(now)
            if ph == "CLOSED_DAY":
                self._set("IDLE", "Market closed today (weekend/holiday)")
            elif ph == "PRE_MARKET":
                self._set("IDLE", f"Waiting for {self.cfg.analysis_start}")
            elif ph == "ANALYSIS":
                self._set("ANALYZING", "Analysis window — observing, no orders")
            elif ph in ("ENTRY", "MANAGE_ONLY"):
                enabled_for = (now - self.enabled_at).total_seconds() if getattr(self, "enabled_at", None) else 1e9
                if (self.last_tick is None and enabled_for > 60
                        and now.time() >= (datetime.combine(now.date(), parse_hm(self.cfg.analysis_start)) + timedelta(minutes=10)).time()):
                    self._set("ANALYZING", "No market data received — delayed opening or feed problem; waiting")
                elif self.effective_state not in ACTIVE_STATES and self.state not in ("SIGNAL_REJECTED",):
                    self._set("ANALYZING", "Paused — no new entries" if self.paused else "Watching for a confirmed signal")
            elif ph == "AFTER_CUTOFF" and not (pos and pos.is_active):
                self._set("SESSION_COMPLETED", f"Day done: {self.trades_today} trade(s), net ₹{self.day_pnl:,.2f}")

    def maybe_midday_repick(self, now: datetime, ph: str):
        """Re-test strategies once around midday (with the morning's candles included) when no trade is open."""
        cfg = self.cfg
        if not cfg.midday_repick or cfg.strategy_source != "ai" or self.picking or ph not in ("ENTRY",):
            return
        if self.active_positions() or now.time() < parse_hm(cfg.midday_repick):
            return
        if getattr(self, "_midday_done", None) == now.date() or not self.pick or self.pick.get("day") != now.date().isoformat():
            return
        self._midday_done = now.date()
        if self.day_blocked and not self.day_blocked.startswith(("AI found", "LIVE paused")):
            return
        self.day_blocked = None
        self._keep_pick = self.pick if self.pick.get("strategy") else None
        self.log(f"Midday re-pick ({cfg.midday_repick}): re-testing strategies with this morning's candles included", "ai")
        self.start_pick(now)

    def period_limits(self, today: date) -> list[str]:
        """Weekly (Mon-Sun) and monthly loss limits from closed trades."""
        out, cfg = [], self.cfg
        if cfg.max_weekly_loss:
            wk0 = (today - timedelta(days=today.weekday())).isoformat()
            wk = sum(v for d, v in self.pnl_days.items() if d >= wk0)
            if wk <= -cfg.max_weekly_loss:
                out.append(f"Weekly loss limit reached (₹{-wk:,.0f} lost this week, limit ₹{cfg.max_weekly_loss:,.0f}) — no new trades until Monday")
        if cfg.max_monthly_loss:
            m0 = today.replace(day=1).isoformat()
            mo = sum(v for d, v in self.pnl_days.items() if d >= m0)
            if mo <= -cfg.max_monthly_loss:
                out.append(f"Monthly loss limit reached (₹{-mo:,.0f} lost this month, limit ₹{cfg.max_monthly_loss:,.0f}) — "
                           f"no new trades this month; review the Stats tab")
        return out

    def all_positions(self) -> list[ManagedPosition]:
        return [p for p in [*self.others, self.position] if p is not None]

    def active_positions(self) -> list[ManagedPosition]:
        return [p for p in self.all_positions() if p.is_active]

    def slot_free(self) -> bool:
        return len(self.active_positions()) < max(1, self.cfg.max_open_positions)

    def open_risk(self) -> float:
        """₹ the open trades could still lose if every stop is hit (counts against the day's loss budget)."""
        out = 0.0
        for p in self.active_positions():
            try:
                q = (p.filled_qty or p.spec.qty)
                if p.entry_price and p.stop is not None:
                    out += max(0.0, (p.entry_price - p.stop) * p.spec.side) * q
            except Exception:
                pass
        return out

    def _account_closed(self):
        for pos in self.all_positions():
            self._account_one(pos)
        self.others = [p for p in self.others if p.is_active]

    def _account_one(self, pos: ManagedPosition):
        if pos is not None and pos.state in ("POSITION_CLOSED", "ORDER_REJECTED") and pos.id not in self.processed:
            self.processed.append(pos.id)
            if pos.state == "POSITION_CLOSED":
                self.day_pnl += pos.net
                d = (self.day or datetime.now().date()).isoformat()
                self.pnl_days[d] = round(self.pnl_days.get(d, 0.0) + pos.net, 2)
                self.pnl_days = dict(sorted(self.pnl_days.items())[-70:])
                self.loss_streak = self.loss_streak + 1 if pos.net <= 0 else 0
                self.log(f"Position closed: {pos.exit_reason}, net ₹{pos.net:,.2f}", "trade")
                if self.cfg.reentry_after_exit:
                    # agreement / strong-trend entries may fire again on a fresh candle (instead of waiting for them to fade)
                    self.processed = [k for k in self.processed if not str(k).startswith(("consensus-used:", "override-used:"))]
                if self.day_pnl <= -abs(self.cfg.max_daily_loss):
                    self.day_blocked = f"Daily loss limit ₹{self.cfg.max_daily_loss:,.0f} reached"
                    self.log(self.day_blocked, "risk")
                elif self.cfg.max_consecutive_losses and self.loss_streak >= self.cfg.max_consecutive_losses:
                    self.day_blocked = (f"{self.loss_streak} losing trades in a row — stopped for today "
                                        f"(limit {self.cfg.max_consecutive_losses}); a fresh start tomorrow")
                    self.log(self.day_blocked, "risk")
            self._set("POSITION_CLOSED" if pos.state == "POSITION_CLOSED" else "ORDER_REJECTED", pos.message)

    def _cancel_entry(self, pos: ManagedPosition, why: str):
        pos.cancel_sent = True  # send the cancel once; the order update / reconciliation finishes it
        try:
            pos.kite.cancel_order(variety="regular", order_id=pos.entry_order_id)
            pos.event("order", f"Entry order cancelled ({why})")
        except Exception as e:
            pos.event("alert", f"Could not cancel entry ({e}); reconciling")
            pos.reconcile()

    # ------------------------------------------------------------ signals
    def _on_candle(self, candle: dict, now: datetime):
        row = pd.DataFrame([candle])
        row["date"] = pd.to_datetime(row["date"])
        self.history = pd.concat([self.history, row], ignore_index=True).tail(3000).reset_index(drop=True)
        if len(self.history) < 60:
            return
        for p in self.active_positions():
            if p.is_open:
                self.smart_manage(p, candle)
        for p in self.active_positions():
            if p.is_open:
                self.check_quick_target(candle, p)
        for p in self.active_positions():
            if p.is_open:
                self.check_time_stop(candle, p)
        for p in self.active_positions():
            if p.is_open:
                rule = getattr(p, "exit_rule", None) or (tuple(p.stats["exit_rule"]) if p.stats.get("exit_rule") else None)
                self._check_exit(p, rule or self.pos_strategy, candle)
        if not self.slot_free():
            return  # all position slots in use: keep managing them, no new entry
        if self.try_pullback(candle, now):
            return
        act = self.active_strategy
        if act is None:
            # these have their own rules, so they run even while the AI is choosing or found nothing
            if not self.try_trend_day(candle, now):
                self.try_day_built(candle, now)
            return
        sig = get_strategy(act[0]).signals(self.history, self._p(act[1])).iloc[-1]
        direction = 1 if sig["entry_long"] else -1 if sig["entry_short"] else 0
        if direction == 0:
            self.try_late_join(now)
            if self.try_trend_day(candle, now) or self.try_day_built(candle, now):
                return
            if not self.try_any_strategy(candle, now):
                self.try_trend_override(candle, now)
                self.try_consensus(candle, now)
            return
        ph = phase(now, self.cfg.analysis_start, self.cfg.entry_start, self.cfg.entry_end, self.cfg.exit_time, self.deps.data_dir)
        if ph != "ENTRY":
            self.log(f"{'Bullish' if direction > 0 else 'Bearish'} setup at {candle['date']:%H:%M} ignored ({ph.lower().replace('_', ' ')} phase)", "signal")
            return
        self.execute_signal(direction, candle, now)

    def _check_exit(self, pos: ManagedPosition, rule, candle: dict):
        """Exit rule of the setup that opened this position (each open position keeps its own)."""
        if rule and rule[0] == "consensus":
            pos_dir = 1 if (pos.spec.instrument_type == "CE") == (pos.spec.side > 0) else -1
            c = self.consensus()
            same, against = (len(c["up"]), len(c["down"])) if pos_dir > 0 else (len(c["down"]), len(c["up"]))
            if same < 2 or (self.cfg.consensus_min and against >= self.cfg.consensus_min):
                msg = (f"Exit: strategy agreement faded on the {candle['date']:%H:%M} candle "
                       f"({same} still agree, {against} now point the other way)")
                self.log(msg, "signal")
                try:
                    pos.event("signal", msg)
                except Exception:
                    pass
                pos.request_exit("signal_exit")
            return
        if rule and rule[0] == "trend_override":
            pos_dir = 1 if (pos.spec.instrument_type == "CE") == (pos.spec.side > 0) else -1
            same = sum(c["ok"] for c in confirmations(self.history, pos_dir))
            against = sum(c["ok"] for c in confirmations(self.history, -pos_dir))
            if against >= 4 or same <= 2:
                msg = (f"Exit: the trend that opened this trade faded on the {candle['date']:%H:%M} candle "
                       f"({same}/6 still agree, {against}/6 now point the other way)")
                self.log(msg, "signal")
                try:
                    pos.event("signal", msg)
                except Exception:
                    pass
                pos.request_exit("signal_exit")
            return
        if True:
            key, params = rule or self.active_strategy or (self.cfg.strategy, self.cfg.params)
            sig = get_strategy(key).signals(self.history, self._p(params)).iloc[-1]
            pos_dir = 1 if (pos.spec.instrument_type == "CE") == (pos.spec.side > 0) else -1
            if (pos_dir > 0 and (sig["exit_long"] or sig["entry_short"])) or (pos_dir < 0 and (sig["exit_short"] or sig["entry_long"])):
                name = self._label(key, params)
                msg = f"Exit: {name} exit rule fired on the {candle['date']:%H:%M} candle ({'trend turned down' if pos_dir > 0 else 'trend turned up'})"
                self.log(msg, "signal")
                try:
                    pos.event("signal", msg)
                except Exception:
                    pass
                pos.request_exit("signal_exit")
            return

    def _best_params(self) -> dict[str, dict]:
        """Each library strategy's best settings from today's AI test (defaults when it wasn't listed)."""
        best: dict[str, dict] = {}
        for t in (self.pick or {}).get("tested") or []:
            if t.get("strategy") in REGISTRY and t["strategy"] not in best and \
                    (not t.get("index") or (t["index"] == self.cfg.index and t.get("interval") == self.cfg.interval)):
                best[t["strategy"]] = t.get("params") or {}
        if self.pick and self.pick.get("strategy") in REGISTRY:
            best[self.pick["strategy"]] = self.pick.get("params") or {}
        return best

    # ------------------------------------------------------------ automatic money management
    AUTO_RISK_PCT = 1.0          # % of capital risked per trade (normal day)
    AUTO_DAY_LOSS_PCT = 2.0      # stop for the day
    AUTO_WEEK_LOSS_PCT = 5.0
    AUTO_MONTH_LOSS_PCT = 10.0
    AUTO_MAX_PREMIUM_PCT = 20.0  # most of the capital one trade's premium may use
    AUTO_TRADES_PER_DAY = 3

    # how much of the normal 1% risk each kind of setup gets (proof quality)
    SETUP_RISK = {"strict_luck": 1.25, "strict": 1.0, "daybuilt": 1.0, "daybuilt_practice": 0.5, "trendday": 0.75,
                  "practice": 0.5, "unproven": 0.5, "extra": 0.5}
    MAX_RISK_PCT = 1.5

    def _size_for_setup(self, direction: int, etype: str) -> dict | None:
        """Bigger risk for stronger setups, smaller for weaker ones — never above 1.5% of capital per trade.
        Lots then follow from that ₹ risk and this trade's stop distance: a tight stop on a strong setup → more lots."""
        cfg, m = self.cfg, getattr(self, "money", None)
        if not (cfg.autopilot and cfg.auto_money) or not m:
            return None
        pk = self.pick or {}
        if etype == "daybuilt":
            kind = "daybuilt_practice" if getattr(self, "_day_built_practice", False) else "daybuilt"
        elif etype == "trendday":
            kind = "trendday"
        elif etype in ("any", "agreement", "override"):
            kind = "extra"
        elif pk.get("practice"):
            kind = "practice"
        elif pk.get("unproven"):
            kind = "unproven"
        else:
            kind = "strict_luck" if (pk.get("luck") or {}).get("passed") else "strict"
        q = self.SETUP_RISK[kind]
        conf = None
        try:
            conf, _ = confidence_for(self.history, direction)
            q *= 1.2 if conf >= 80 else 0.8 if conf < 60 else 1.0
        except Exception:
            pass
        pct = round(min(self.MAX_RISK_PCT, max(0.25, m["risk_pct"] * q)), 2)
        cfg.risk_per_trade_pct = pct
        self.sizing = {"setup": kind, "quality": round(q, 2), "confidence": conf, "risk_pct": pct,
                       "risk_rupees": round(m["capital"] * pct / 100, 0)}
        return self.sizing

    AUTO_MAX_LOTS = 10

    def _lot_cap(self) -> int:
        """Most lots one order may use. Automatic money: up to 10 (the risk budget usually allows fewer);
        expiry/event 'careful' days: 1."""
        if getattr(self, "_careful", False):
            return 1
        if self.cfg.autopilot and self.cfg.auto_money:
            return max(1, min(int(self.cfg.max_auto_lots or self.AUTO_MAX_LOTS), 50))
        return self.cfg.lots

    def _entry_band(self, premium: float) -> float:
        """LIMIT entry room above the quote. Automatic: 0.5% of the premium, between ₹0.5 and ₹5
        (a ₹800 BANKNIFTY option often has a ₹2-3 spread, so ₹1 of room misses fills)."""
        if self.cfg.autopilot and self.cfg.auto_money:
            return round(min(5.0, max(0.5, premium * 0.005)), 2)
        return self.cfg.entry_tolerance_pts

    def apply_auto_money(self, now: datetime) -> dict | None:
        """Autopilot sizing: everything comes from the account size and today's conditions, on the safe side.
        Halves the risk on nervous days (VIX > 20), expiry/event days, after 2 losses in a row, and when the month
        is already down more than 5%."""
        cfg = self.cfg
        if not (cfg.autopilot and cfg.auto_money):
            return None
        cap, src = None, ""
        if cfg.mode == "live" and self.deps.margin_available:
            try:
                cap, src = float(self.deps.margin_available()), "your Kite funds"
            except Exception:
                cap = getattr(self, "_last_live_cap", None)
                src = "last known Kite funds"
            if cap:
                self._last_live_cap = cap
        if not cap:  # paper: the starting amount plus everything this Auto Trade has made or lost so far
            made = round(sum(self.pnl_days.values()), 2)
            cap = max(float(cfg.paper_capital) + made, 0.0)
            src = (f"paper capital ₹{cfg.paper_capital:,.0f} {'+' if made >= 0 else '−'} ₹{abs(made):,.0f} from closed trades"
                   if made else "paper capital")
        reasons, mult = [], 1.0
        v = self.vix()
        if v is not None and v > 20:
            mult *= 0.5
            reasons.append(f"VIX {v:.0f} > 20")
        caution = self.day_caution(now)
        if caution:
            mult *= 0.5
            reasons.append(" + ".join(caution))
        if self.loss_streak >= 2:
            mult *= 0.5
            reasons.append(f"{self.loss_streak} losses in a row")
        m0 = now.date().replace(day=1).isoformat()
        month = sum(x for d, x in self.pnl_days.items() if d >= m0)
        if month < -0.05 * cap:
            mult *= 0.5
            reasons.append(f"month down ₹{-month:,.0f}")
        risk_pct = max(0.25, self.AUTO_RISK_PCT * mult)
        cfg.account_capital = round(cap, 0)
        cfg.risk_per_trade_pct = round(risk_pct, 2)
        cfg.max_daily_loss = round(cap * self.AUTO_DAY_LOSS_PCT / 100, 0)
        cfg.max_weekly_loss = round(cap * self.AUTO_WEEK_LOSS_PCT / 100, 0)
        cfg.max_monthly_loss = round(cap * self.AUTO_MONTH_LOSS_PCT / 100, 0)
        cfg.max_capital = round(cap * self.AUTO_MAX_PREMIUM_PCT / 100, 0)
        # your own "max trades per day" is kept; lots come from the risk budget (see _lot_cap)
        cfg.max_consecutive_losses = 3
        self.money = {"capital": round(cap, 0), "source": src, "risk_pct": round(risk_pct, 2),
                      "risk_per_trade": round(cap * risk_pct / 100, 0), "day_stop": cfg.max_daily_loss,
                      "week_stop": cfg.max_weekly_loss, "month_stop": cfg.max_monthly_loss, "max_premium": cfg.max_capital,
                      "trades_per_day": cfg.max_trades_per_day, "why_lower": reasons}
        return self.money

    # ------------------------------------------------------------ Day Builder
    def _builder_allowed(self) -> bool:
        return self.cfg.day_builder == "always" or (self.cfg.day_builder == "paper" and self.cfg.mode == "paper")

    def maybe_day_build(self, now: datetime, ph: str):
        if not self._builder_allowed() or self.building or ph != "ENTRY":
            return
        if self.active_positions():
            return
        times = sorted(x.strip() for x in self.cfg.day_build_times.split(",") if x.strip())
        due = [t for t in times if now.strftime("%H:%M") >= t]
        if not due:
            return
        b = self.day_build or {}
        if b.get("day") == now.date().isoformat() and b.get("checkpoint") == due[-1]:
            return
        self.building = True
        self.log(f"Day Builder ({due[-1]}): reading today's market and testing strategies on similar past days…", "ai")
        if self.run_async:
            threading.Thread(target=self._day_build, args=(now, due[-1]), daemon=True).start()
        else:
            self._day_build(now, due[-1])

    def build_now(self, now: datetime):
        """Run the Day Builder right now (button), using today's candles so far."""
        if self.building:
            raise ValueError("Day Builder is already running")
        if now.time() < parse_hm("09:45"):
            raise ValueError("Wait until 09:45 — the Day Builder needs at least half an hour of today's candles")
        self.building = True
        self.log(f"Day Builder ({now:%H:%M}, on request): reading today's market and testing strategies on similar past days…", "ai")
        cp = now.strftime("%H:%M")
        if self.run_async:
            threading.Thread(target=self._day_build, args=(now, cp), daemon=True).start()
        else:
            self._day_build(now, cp)

    def _day_build(self, now: datetime, checkpoint: str):
        from .backtest import BacktestConfig
        from .candles import INTERVAL_MINUTES
        from .charges import segment_for
        from .daybuilder import build_for_today
        from .quality import calibrated_slippage_pct
        cfg = self.cfg
        try:
            inst = self.deps.instruments()
            spot = spot_instrument(inst, cfg.index)
            mins = INTERVAL_MINUTES[cfg.interval]
            rows = self.deps.history(spot["instrument_token"], cfg.interval, now - timedelta(days=AI_LOOKBACK_DAYS.get(cfg.interval, 90)), now)
            rows = [r for r in rows if pd.Timestamp(r["date"]).tz_localize(None) + pd.Timedelta(minutes=mins) <= pd.Timestamp(now)]
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            for col in ("open", "high", "low", "close"):
                df[col] = df[col].astype(float)
            if "volume" not in df:
                df["volume"] = 0.0
            try:
                fut = nearest_future(inst, cfg.index, now.date())
                lot = int(fut["lot_size"]) if fut else DEFAULT_LOT.get(cfg.index, 65)
            except Exception:
                lot = DEFAULT_LOT.get(cfg.index, 65)
            method = self.trade_strike_method()
            slip_pct, _ = calibrated_slippage_pct(self.deps.data_dir, OPTION_SLIPPAGE_PCT)
            px = float(df["close"].iloc[-1])
            base = BacktestConfig(strategy="sma_cross", product="MIS", segment=segment_for("MIS", "EQ"),
                                  exchange="BSE" if cfg.index == "SENSEX" else "NSE", lot_size=1, allow_short=True,
                                  sizing="fixed_qty", quantity=max(1, round(lot * delta_for(method))),
                                  fixed_cost=option_round_trip_cost(cfg.index, px, lot, self.vix(), method, slip_pct),
                                  bar_cost=option_theta_per_bar(px, mins, lot, self.vix(),
                                                                EXPIRY_DAYS_LEFT.get(index_cfg(cfg.index).get("expiry_rule"), 3.0)),
                                  slippage_bps=1.0, no_entry_after=cfg.entry_end, squareoff_time=cfg.exit_time)

            def prog(m):
                self.pick_progress = {"stage": m, "started": now.isoformat(timespec="seconds")}
            res = build_for_today(df, now.date(), parse_hm(checkpoint), base, cfg.entry_end, progress=prog)
            with self.lock:
                self.day_build = {**res, "day": now.date().isoformat(), "built_at": now.isoformat(timespec="seconds"),
                                  "index": cfg.index, "interval": cfg.interval}
                pf = res["profile"]
                self.log(f"Day Builder: today looks like a {pf['label']} day (move {pf['move_pct']:+.2f}%, "
                         f"{pf['above_vwap'] * 100:.0f}% of candles above VWAP, {pf['vwap_crosses']} VWAP crossings). "
                         + res["why"], "ai")
        except Exception as e:
            with self.lock:
                self.day_build = {"day": now.date().isoformat(), "checkpoint": checkpoint, "best": None,
                                  "built_at": now.isoformat(timespec="seconds"), "why": f"Day Builder could not run: {e}"[:200]}
                self.log(f"Day Builder failed: {e}"[:200], "alert")
        finally:
            self.building = False
            self.pick_progress = None
            self._changed()

    def try_day_built(self, candle: dict, now: datetime) -> bool:
        """Trade today's Day Builder strategy on its own fresh signal."""
        cfg = self.cfg
        b = self.day_build or {}
        best = b.get("best")
        practice = False
        if not best and cfg.mode == "paper" and cfg.paper_practice and b.get("practice"):
            best, practice = b["practice"], True  # paper practice: passed everything except the luck check
        if not self._builder_allowed() or not best or b.get("day") != now.date().isoformat():
            return False
        if b.get("index") != cfg.index or b.get("interval") != cfg.interval:
            return False
        if not self.enabled or self.paused or len(self.history) < 60:
            return False
        if not self.slot_free():
            return False
        if phase(now, cfg.analysis_start, cfg.entry_start, cfg.entry_end, cfg.exit_time, self.deps.data_dir) != "ENTRY":
            return False
        try:
            sg = REGISTRY[best["strategy"]].signals(self.history, self._p(best["params"])).iloc[-1]
        except Exception:
            return False
        direction = 1 if sg["entry_long"] else -1 if sg["entry_short"] else 0
        if not direction:
            return False
        self._day_built_practice = practice
        self.log(f"Day-built {'PRACTICE ' if practice else ''}strategy {best['name']} signalled {'BUY → CE' if direction > 0 else 'SELL → PE'} on the "
                 f"{candle['date']:%H:%M} candle ({b['profile']['label']} day)", "signal")
        blocked = self.day_blocked
        if blocked and blocked.startswith("AI found"):
            self.day_blocked = None
        try:
            self.execute_signal(direction, candle, now, late_key=f"daybuilt:{candle['date']}:{direction}",
                                fired_by=(best["strategy"], best["params"]),
                                late_note=f"Day Builder ({b['checkpoint']}): {b['why']}")
        finally:
            if blocked and blocked.startswith("AI found"):
                self.day_blocked = blocked
        return True

    def try_trend_day(self, candle: dict, now: datetime) -> bool:
        """Trend Day setup: on a strong one-way day, buy the dip to EMA20 — runs even when the AI picked nothing."""
        cfg = self.cfg
        if cfg.trend_day_entry == "off" or (cfg.trend_day_entry == "paper" and cfg.mode != "paper"):
            return False
        if not self.enabled or self.paused or len(self.history) < 60:
            return False
        if not self.slot_free():
            return False
        if phase(now, cfg.analysis_start, cfg.entry_start, cfg.entry_end, cfg.exit_time, self.deps.data_dir) != "ENTRY":
            return False
        params = self._best_params().get("trend_day", {})
        try:
            sg = REGISTRY["trend_day"].signals(self.history, self._p(params)).iloc[-1]
        except Exception:
            return False
        direction = 1 if sg["entry_long"] else -1 if sg["entry_short"] else 0
        if not direction:
            return False
        self.log(f"Trend Day setup: strong {'UP' if direction > 0 else 'DOWN'} day and a pullback bounce on the "
                 f"{candle['date']:%H:%M} candle → {'BUY CE' if direction > 0 else 'BUY PE'}", "signal")
        blocked = self.day_blocked
        if blocked and blocked.startswith("AI found"):
            self.day_blocked = None  # this setup has its own rule; the AI's WAIT applies to its daily pick only
        try:
            self.execute_signal(direction, candle, now, late_key=f"trendday:{candle['date']}:{direction}",
                                fired_by=("trend_day", params),
                                late_note=("Trend Day setup (your setting): strong one-way day, entered on the pullback bounce. "
                                           "Exits when price closes back through VWAP, or at stop/target/time."))
        finally:
            if blocked and blocked.startswith("AI found"):
                self.day_blocked = blocked
        return True

    def try_any_strategy(self, candle: dict, now: datetime) -> bool:
        """Enter on a fresh buy/sell signal from ANY library strategy on this closed candle (opposite fresh signals
        cancel out). The trade then follows that strategy's own exit rule."""
        cfg = self.cfg
        if not cfg.any_strategy_entry or not self.enabled or self.paused or len(self.history) < 60:
            return False
        if not self.slot_free():
            return False
        if phase(now, cfg.analysis_start, cfg.entry_start, cfg.entry_end, cfg.exit_time, self.deps.data_dir) != "ENTRY":
            return False
        best = self._best_params()
        ups, dns = [], []
        for k, strat in REGISTRY.items():
            try:
                sg = strat.signals(self.history, self._p(best.get(k, {}))).iloc[-1]
            except Exception:
                continue
            if sg["entry_long"]:
                ups.append(k)
            elif sg["entry_short"]:
                dns.append(k)
        if bool(ups) == bool(dns):  # none, or conflicting fresh signals
            return False
        direction, keys = (1, ups) if ups else (-1, dns)
        k = keys[0]
        names = ", ".join(REGISTRY[x].name.split(" (")[0] for x in keys)
        self.log(f"Any-strategy entry: {names} gave a fresh {'BUY (→ CE)' if direction > 0 else 'SELL (→ PE)'} signal on the "
                 f"{candle['date']:%H:%M} candle", "signal")
        self.execute_signal(direction, candle, now, late_key=f"any:{candle['date']}:{direction}",
                            fired_by=(k, best.get(k, {})),
                            late_note=f"Any-strategy entry (your setting): fresh signal from {names}. "
                                      f"Exits by {REGISTRY[k].name}'s own exit rule, or at stop/target/time.")
        return True

    def consensus(self, df: pd.DataFrame | None = None) -> dict:
        """Which way each library strategy currently points (its own entry/exit state today), using the best
        settings from today's AI test. Cached per closed candle."""
        from .analysis import current_signal
        df = self.history if df is None else df
        if len(df) < 60:
            return {"up": [], "down": [], "total": 0}
        key = (len(df), str(df["date"].iloc[-1]))
        if getattr(self, "_cons_key", None) == key:
            return self._cons
        best = self._best_params()
        up, down, total = [], [], 0
        for k, strat in REGISTRY.items():
            try:
                cs = current_signal(df, k, self._p(best.get(k, {})), allow_short=True)
            except Exception:
                continue
            total += 1
            if cs["position"] == "LONG":
                up.append(strat.name.split(" (")[0])
            elif cs["position"] == "SHORT":
                down.append(strat.name.split(" (")[0])
        self._cons_key, self._cons = key, {"up": up, "down": down, "total": total}
        return self._cons

    def try_consensus(self, candle: dict, now: datetime):
        """Enter when at least `consensus_min` strategies point the same way (and more than point the other way).
        Re-arms for a direction after its count drops below the threshold."""
        cfg = self.cfg
        if cfg.consensus_min <= 0 or not self.enabled or self.paused:
            return
        c = self.consensus()
        nu, nd, n = len(c["up"]), len(c["down"]), cfg.consensus_min
        day = now.date()
        for d, cnt in ((1, nu), (-1, nd)):
            mark = f"consensus-used:{day}:{d}"
            if cnt < n and mark in self.processed:
                self.processed.remove(mark)
        if phase(now, cfg.analysis_start, cfg.entry_start, cfg.entry_end, cfg.exit_time, self.deps.data_dir) != "ENTRY":
            return
        direction = 1 if nu >= n and nu > nd else -1 if nd >= n and nd > nu else 0
        if not direction or not self.slot_free():
            return
        mark = f"consensus-used:{day}:{direction}"
        if mark in self.processed:
            return
        self.processed.append(mark)
        names = c["up"] if direction > 0 else c["down"]
        other = c["down"] if direction > 0 else c["up"]
        self.log(f"Strategy agreement: {len(names)}/{c['total']} strategies point {'UP' if direction > 0 else 'DOWN'} "
                 f"({', '.join(names)}) on the {candle['date']:%H:%M} candle — entering", "signal")
        self.execute_signal(direction, candle, now, late_key=f"consensus:{day}:{direction}:{candle['date']}", override="consensus",
                            late_note=f"Strategy agreement (your setting {n}+): {len(names)} of {c['total']} strategies point "
                                      f"{'UP' if direction > 0 else 'DOWN'} — {', '.join(names)}"
                                      + (f"; against: {', '.join(other)}" if other else "; none against")
                                      + ". Exits when fewer than 2 still agree or the other side reaches the threshold, or at stop/target/time.")

    OVERRIDE_REARM_AT = 3  # after an override trade, re-arm once agreement for that direction drops to this or lower

    def try_trend_override(self, candle: dict, now: datetime):
        """Strong-trend override: when the strategy has no signal but nearly all indicator checks agree on the
        closed candle, enter in that direction. After one override trade in a direction, the next one needs the
        trend to cool off (agreement drops to 3/6 or less) and build up again — so it re-enters a fresh push, not
        the same one. Daily trade/loss limits and stop-loss still apply."""
        cfg = self.cfg
        if cfg.trend_override_min <= 0 or not self.enabled or self.paused or len(self.history) < 60:
            return
        up = confirmations(self.history, 1)
        dn = confirmations(self.history, -1)
        nu, nd = sum(c["ok"] for c in up), sum(c["ok"] for c in dn)
        day = now.date()
        for d, n_d in ((1, nu), (-1, nd)):  # re-arm a direction once its agreement has cooled off
            mark = f"override-used:{day}:{d}"
            if n_d <= self.OVERRIDE_REARM_AT and mark in self.processed:
                self.processed.remove(mark)
                self.log(f"Strong-trend override re-armed for {'UP' if d > 0 else 'DOWN'} ({n_d}/6 now agree)", "signal")
        if phase(now, cfg.analysis_start, cfg.entry_start, cfg.entry_end, cfg.exit_time, self.deps.data_dir) != "ENTRY":
            return
        direction = 1 if nu >= cfg.trend_override_min and nd == 0 else -1 if nd >= cfg.trend_override_min and nu == 0 else 0
        if not direction:
            return
        mark = f"override-used:{day}:{direction}"
        if mark in self.processed or not self.slot_free():
            return
        self.processed.append(mark)
        key = f"override:{day}:{direction}:{candle['date']}"
        n = nu if direction > 0 else nd
        name = self._label(*self.active_strategy) if self.active_strategy else "the strategy"
        self.log(f"Strong-trend override: {n}/6 indicator checks point {'UP' if direction > 0 else 'DOWN'} on the "
                 f"{candle['date']:%H:%M} candle while {name} has no signal — entering", "signal")
        self.execute_signal(direction, candle, now, late_key=key, override=True,
                            late_note=f"Strong-trend override (your setting): {n}/6 indicators agree "
                                      f"{'UP' if direction > 0 else 'DOWN'}; {name} had no signal of its own. "
                                      f"Exits when the trend fades (≤2/6 agree or ≥4/6 turn against), or at stop/target/time. "
                                      f"Not part of the tested results.")

    MAX_JOIN_DRIFT_PCT = 0.3  # don't chase: skip if the index already moved this far since the signal
    MAX_JOIN_DRIFT_ATR = 0.5  # ... or more than half a normal candle's range (ATR) in the signal's direction
    STRETCH_ATR, STRETCH_ATR_MAX = 2.5, 4.0

    def stretch_check(self, direction: int) -> dict | None:
        """Is the move already stretched? Far from today's VWAP (in candle ranges) with RSI at an extreme means the
        easy part is gone and a snap-back is likely — buying now usually means buying the top/bottom."""
        h = self.history
        if h is None or len(h) < 40 or not direction:
            return None
        try:
            last_day = h["date"].iloc[-1].date()
            today = h[h["date"].dt.date == last_day]
            close = float(h["close"].iloc[-1])
            r = float(ind.rsi(h["close"], 14).iloc[-1])
            a = float(ind.atr(h, 14).iloc[-1])
            vw = float(ind.session_vwap(today).iloc[-1]) if len(today) else close
            e20 = float(ind.ema(h["close"], 20).iloc[-1])
        except Exception:
            return None
        if not a or a != a:
            return None
        dist = (close - vw) / a * direction
        extreme = (direction > 0 and r >= 70) or (direction < 0 and r <= 30)
        stretched = (dist >= self.STRETCH_ATR and extreme) or dist >= self.STRETCH_ATR_MAX
        return {"rsi": round(r, 1), "dist_atr": round(dist, 1), "vwap": round(vw, 2), "ema20": round(e20, 2), "atr": round(a, 2),
                "stretched": stretched, "close": close,
                "label": (f"Move already stretched: {self.cfg.index} is {dist:.1f}x a candle's range "
                          f"{'above' if direction > 0 else 'below'} today's VWAP {vw:,.0f}, RSI {r:.0f} — chasing here "
                          f"usually buys the {'top' if direction > 0 else 'bottom'} before a snap-back")}

    def try_pullback(self, candle: dict, now: datetime) -> bool:
        """A stretched signal was parked: enter only when price pulls back to the 20-EMA zone and that bounce fails
        (a candle closing back in the trend's direction). Cancelled if price crosses VWAP the other way or time runs out."""
        pb = self.pending_pullback
        if not pb or not (self.enabled and not self.paused):
            return False
        d = pb["direction"]
        if candle["date"] > pb["until"]:
            self.pending_pullback = None
            self.log(f"Pullback entry cancelled: no clean retest within {self.cfg.pullback_wait_bars} candles of the "
                     f"{pb['since']:%H:%M} signal — skipped instead of chasing", "signal")
            return False
        st = self.stretch_check(d)
        if not st:
            return False
        c, o, hi, lo = float(candle["close"]), float(candle["open"]), float(candle["high"]), float(candle["low"])
        if (c - st["vwap"]) * d < 0:
            self.pending_pullback = None
            self.log(f"Pullback entry cancelled: {self.cfg.index} crossed back through VWAP {st['vwap']:,.0f} — the "
                     f"{'up' if d > 0 else 'down'}-move has failed", "signal")
            return False
        zone = st["ema20"] + d * 0.3 * st["atr"]  # within 0.3 candle-range of the 20-EMA counts as a retest
        touched = (lo <= zone) if d > 0 else (hi >= zone)
        resumed = (c > o and c > st["ema20"] - 0.1 * st["atr"]) if d > 0 else (c < o and c < st["ema20"] + 0.1 * st["atr"])
        if not (touched and resumed):
            return False
        self.pending_pullback = None
        re = pb.get("reentry_of")
        note = (f"Trend re-entry: the {re:%H:%M} {'up' if d > 0 else 'down'}-move was missed (too late to chase), so it waited for "
                f"a pullback. Price came back to the 20-EMA {st['ema20']:,.0f} and this candle turned {'up' if d > 0 else 'down'} "
                f"again with the trend intact → joining at a better price" if re else
                f"Pullback entry: the {pb['since']:%H:%M} {'BUY' if d > 0 else 'SELL'} signal was stretched "
                f"(RSI {pb['rsi']:.0f}, {pb['dist_atr']:.1f}x a candle from VWAP), so it waited. Price pulled back "
                f"to the 20-EMA {st['ema20']:,.0f} and this candle turned {'up' if d > 0 else 'down'} again → "
                f"entering with the trend at a better price")
        self.execute_signal(d, candle, now, late_key=f"pullback:{pb['since']}:{d}", late_note=note)
        return True

    def try_late_join(self, now: datetime):
        """Enter a move the strategy signalled a little earlier (e.g. while the app was off or a limit blocked it),
        once per signal, if it is recent and the index hasn't already run away from the signal price."""
        cfg = self.cfg
        if not (self.enabled and not self.paused and cfg.join_window_minutes > 0):
            return
        if not self.slot_free():
            return
        act = self.active_strategy
        if act is None or len(self.history) < 60:
            return
        if phase(now, cfg.analysis_start, cfg.entry_start, cfg.entry_end, cfg.exit_time, self.deps.data_dir) != "ENTRY":
            return
        from .analysis import current_signal
        cs = current_signal(self.history, act[0], self._p(act[1]), allow_short=True)
        if cs["position"] == "FLAT" or not cs["since"]:
            return
        since = pd.Timestamp(cs["since"]).to_pydatetime()
        if since.date() != now.date():
            return
        age = (now - (since + timedelta(minutes=self.builder.minutes))).total_seconds() / 60
        key = f"late:{since}"
        traded = any(g.get("signal_time", "").startswith(since.isoformat()[:16]) and g.get("risk_check") == "PASS"
                     for g in self.signals)
        direction = 1 if cs["position"] == "LONG" else -1
        if traded:
            return
        if age > cfg.join_window_minutes or key in self.processed:
            self._arm_trend_pullback(direction, since, now)
            return
        row = self.history[self.history["date"] == pd.Timestamp(since)]
        sig_close = float(row["close"].iloc[-1]) if len(row) else None
        price = self.last_spot or float(self.history["close"].iloc[-1])
        drift = (price - sig_close) / sig_close * 100 * direction if sig_close else 0.0
        try:
            a = float(ind.atr(self.history, 14).iloc[-1])
        except Exception:
            a = 0.0
        drift_pts = (price - sig_close) * direction if sig_close else 0.0
        if drift > self.MAX_JOIN_DRIFT_PCT or (a and drift_pts > self.MAX_JOIN_DRIFT_ATR * a):
            self.processed.append(key)
            self.log(f"Not joining the {since:%H:%M} {'up' if direction > 0 else 'down'}-move: {cfg.index} already moved "
                     f"{drift_pts:+.0f} pts ({drift:.2f}%{f', {drift_pts / a:.1f}x a candle' if a else ''}) since the signal "
                     f"(limit {self.MAX_JOIN_DRIFT_PCT}% / {self.MAX_JOIN_DRIFT_ATR}x a candle's range) — too late to chase", "signal")
            self._arm_trend_pullback(direction, since, now)
            return
        self.log(f"Joining the {since:%H:%M} {'BUY → CE' if direction > 0 else 'SELL → PE'} signal {age:.0f} min late "
                 f"({cfg.index} {price - (sig_close or price):+.0f} pts since)", "signal")
        self.execute_signal(direction, {"date": since}, now, late_key=key,
                            late_note=f"Late entry: {self._label(*act)} signalled at {since:%H:%M}; joined at {now:%H:%M}, "
                                      f"{cfg.index} {price - (sig_close or price):+.0f} pts since (max {self.MAX_JOIN_DRIFT_PCT}% allowed)")

    REENTRY_TRIES = 2

    def trend_intact(self, direction: int) -> tuple[bool, str]:
        """Is the move still a trend worth joining? Index on the trend side of VWAP, 20-EMA on the right side of the
        50-EMA and ADX not in chop."""
        h = self.history
        try:
            today = h[h["date"].dt.date == h["date"].iloc[-1].date()]
            c = float(h["close"].iloc[-1])
            vw = float(ind.session_vwap(today).iloc[-1])
            e20, e50 = float(ind.ema(h["close"], 20).iloc[-1]), float(ind.ema(h["close"], 50).iloc[-1])
            adx = float(ind.adx(h, 14)[0].iloc[-1])
        except Exception:
            return False, "not enough data"
        ok_v, ok_e, ok_a = (c - vw) * direction > 0, (e20 - e50) * direction > 0, adx >= max(15.0, self.cfg.min_adx)
        why = f"VWAP {'✓' if ok_v else '✗'}, 20/50-EMA {'✓' if ok_e else '✗'}, ADX {adx:.0f} {'✓' if ok_a else '✗'}"
        return ok_v and ok_e and ok_a, why

    def _arm_trend_pullback(self, direction: int, since: datetime, now: datetime):
        """The strategy is still in a move we missed: rather than chase it or give up, wait for a pullback to the
        20-EMA and join when that bounce fails — a better price with a tighter, more logical stop."""
        cfg = self.cfg
        if not (cfg.trend_reentry and cfg.stretch_mode != "off") or self.pending_pullback or not self.slot_free():
            return
        tries = sum(1 for k in self.processed if str(k).startswith(f"reentry:{since}:"))
        if tries >= self.REENTRY_TRIES:
            return
        ok, why = self.trend_intact(direction)
        if not ok:
            return
        last = self.history["date"].iloc[-1].to_pydatetime()
        st = self.stretch_check(direction) or {}
        self.processed.append(f"reentry:{since}:{tries + 1}")
        self.pending_pullback = {"direction": direction, "since": last, "rsi": st.get("rsi", 50.0), "dist_atr": st.get("dist_atr", 0.0),
                                 "until": last + timedelta(minutes=self.builder.minutes * cfg.pullback_wait_bars), "reentry_of": since}
        self.log(f"Trend re-entry armed: the {since:%H:%M} {'up' if direction > 0 else 'down'}-move is still intact ({why}) — "
                 f"joining on the next pullback to the 20-EMA that turns {'up' if direction > 0 else 'down'} again "
                 f"(try {tries + 1}/{self.REENTRY_TRIES}, waits up to {cfg.pullback_wait_bars} candles)", "signal")

    SIZE_PROBLEMS = ("1 lot is too big", "Over the daily risk budget", "Premium ₹")

    def build_signal(self, direction: int, candle_time: datetime | None, now: datetime) -> tuple[dict, ManagedPosition | None, float | None]:
        """Build the order. Autopilot: when the ONLY reason to skip is that 1 lot is too big for the money limits,
        try a strike 1-2 steps out of the money — a cheaper premium with the same index-point stop costs fewer ₹ —
        instead of skipping the signal."""
        sig, pos, ref = self._build_signal(direction, candle_time, now)
        cfg = self.cfg
        if pos is not None or not cfg.autopilot or not self._only_size(sig.get("risk_problems")):
            return sig, pos, ref
        for off in (1, 2):
            s2, p2, r2 = self._build_signal(direction, candle_time, now, strike=("OTM", off))
            if p2 is not None:
                s2["why"].insert(0, {"group": "contract", "ok": True, "label":
                                     f"1 lot of {sig.get('tradingsymbol')} risked ₹{sig.get('est_max_loss') or 0:,.0f} — too big for your "
                                     f"money limits, so {s2.get('tradingsymbol')} ({off} strike{'s' if off > 1 else ''} out of the money) "
                                     f"is used: same index-point stop, ₹{s2.get('est_max_loss') or 0:,.0f} risk"})
                return s2, p2, r2
            if not self._only_size(s2.get("risk_problems")):
                break
        return sig, pos, ref

    def _only_size(self, problems) -> bool:
        return bool(problems) and all(str(p).startswith(self.SIZE_PROBLEMS) for p in problems)

    def _build_signal(self, direction: int, candle_time: datetime | None, now: datetime,
                      strike: tuple[str, int] | None = None) -> tuple[dict, ManagedPosition | None, float | None]:
        """Deterministic signal JSON + a ready (not yet opened) position. Never invents data."""
        cfg = self.cfg
        pside = 1 if cfg.side == "BUY" else -1
        ot = ("CE" if direction > 0 else "PE") if pside > 0 else ("PE" if direction > 0 else "CE")
        conf, reasons = confidence_for(self.history, direction) if len(self.history) >= 60 else (0, [])
        sig = {
            "instrument": cfg.index, "market_direction": "BULLISH" if direction > 0 else "BEARISH",
            "action": f"{cfg.side}_{ot}", "option_type": ot, "expiry": None, "strike": None, "tradingsymbol": None,
            "entry_price": None, "stop_loss": None, "target_price": None, "lots": self._lot_cap(), "quantity": None,
            "risk_reward_ratio": None, "est_max_loss": None, "est_charges": None, "confidence": conf,
            "reason": "; ".join(reasons), "strategy": cfg.strategy, "signal_time": (candle_time or now).isoformat(),
            "valid_until": (now + timedelta(seconds=cfg.signal_valid_seconds)).isoformat(), "risk_check": "PENDING",
            "risk_problems": [], "mode": cfg.mode,
        }
        act = self.active_strategy or (cfg.strategy, cfg.params)
        gate_conf = act[0] not in REVERSION
        if act[0] == "combo" and len(self.history):
            try:
                from .strategies import COMBO
                fired_keys = [k for k, sg in COMBO.member_signals(self.history, self._p(act[1]))
                              if sg["entry_long"].iloc[-1] or sg["entry_short"].iloc[-1]]
                gate_conf = not any(k in REVERSION for k in fired_keys)
            except Exception:
                pass
        sig["strategy"], sig["params"] = act[0], act[1]
        why: list[dict] = []
        if cfg.strategy_source == "ai" and self.pick and self.pick.get("strategy"):
            t = self.pick["test"]
            why.append({"group": "strategy", "ok": True, "label":
                        f"{'AI picked' if not self.pick.get('unproven') else 'Practice pick (not proven)'} {self.pick['name']} today — "
                        f"on unseen data: ₹{t.get('net_pnl', 0):,.0f} per lot, {t['trades']} trades, {t['win_rate_pct']}% wins"})
        else:
            why.append({"group": "strategy", "ok": True, "label": f"Your chosen strategy: {self._label(*act)}"})
        if act[0] == "combo" and len(self.history):
            try:
                from .strategies import COMBO
                fired = COMBO.fired(self.history, self._p(act[1]))
                if fired:
                    why.append({"group": "strategy", "ok": True, "label": "Fired by: " + ", ".join(fired)})
            except Exception:
                pass
        why.append({"group": "strategy", "ok": True, "label":
                    f"Its {'BUY' if direction > 0 else 'SELL'} rule fired on the {(candle_time or now):%H:%M} candle → "
                    f"market looks {'bullish' if direction > 0 else 'bearish'} → {cfg.side} {ot}"})
        if self.last_spot and self.levels.get("prev_close"):
            ch = self.last_spot - self.levels["prev_close"]
            why.append({"group": "market", "ok": (ch > 0) == (direction > 0), "label":
                        f"{cfg.index} {self.last_spot:,.0f}, {ch:+,.0f} pts ({ch / self.levels['prev_close'] * 100:+.2f}%) today"})
        if len(self.history) >= 60:
            why += confirmations(self.history, direction)
        why.append({"group": "risk", "ok": (conf >= cfg.min_confidence) or not gate_conf, "label":
                    f"Confidence {conf}% (minimum {cfg.min_confidence}%)" if gate_conf else
                    f"Trend agreement {conf}% — not required: a counter-trend strategy fired"})
        sig["why"] = why
        problems = []
        if cfg.option_rule != "AUTO" and cfg.option_rule != ot:
            problems.append(f"Signal needs {ot} but this strategy is limited to {cfg.option_rule}")
        if gate_conf and conf < cfg.min_confidence:
            problems.append(f"Confidence {conf}% below minimum {cfg.min_confidence}%")
        if self.day_blocked:
            problems.append(self.day_blocked)
        if self.trades_today >= cfg.max_trades_per_day:
            problems.append(f"Max {cfg.max_trades_per_day} trades/day reached")
        problems += self.period_limits(now.date())
        spot = self.last_spot
        if spot is None:
            problems.append("No live index price")
        prev_careful = getattr(self, "_careful", False)
        same = [p for p in self.active_positions() if p.spec.instrument_type == ot and p.spec.side == pside]
        if same and cfg.same_direction_mode == "block":
            problems.append(f"Already holding a {ot} — won't double the same way")
        elif same and cfg.same_direction_mode == "small":
            self._careful = True
            why.append({"group": "risk", "ok": True, "label":
                        f"Already holding a {ot}: this second same-direction trade is 1 lot only (together they are one bigger bet)"})
        pos, ref = None, None
        if not problems:
            try:
                inst = self.deps.instruments()
                exps = expiries(inst, cfg.index, now.date())
                if cfg.expiry == "NEAREST":
                    exps2 = [e for e in exps if not (cfg.skip_expiry_day and e == now.date().isoformat())]
                    if not exps2:
                        raise ValueError("No valid expiry listed")
                    expiry = exps2[0]
                else:
                    if cfg.expiry not in exps:
                        raise ValueError(f"Expiry {cfg.expiry} is not listed")
                    expiry = cfg.expiry
                s_method, s_off = strike or (cfg.strike_method, cfg.strike_offset)
                c = select_contract(inst, cfg.index, expiry, ot, spot, s_method, s_off)
                sig.update(expiry=c.expiry, strike=c.strike, tradingsymbol=c.tradingsymbol, moneyness=c.moneyness,
                           lot_size=c.lot_size, quantity=c.lot_size * self._lot_cap())
                key = f"{c.exchange}:{c.tradingsymbol}"
                q = self.deps.quote([key]).get(key) or {}
                ltp, bid, ask = _best_prices(q)
                if not ltp:
                    raise ValueError("No live premium for the contract")
                if bid and ask:
                    spread = (ask - bid) / ((ask + bid) / 2) * 100
                    sig["spread_pct"] = round(spread, 2)
                    if spread > cfg.max_spread_pct:
                        problems.append(f"Spread {spread:.1f}% above {cfg.max_spread_pct}% (illiquid)")
                ref = (ask if pside > 0 else bid) or ltp
                ivc = self._iv_check(c, ltp, spot, now, ot)
                delta = delta_for(cfg.strike_method)
                risk0 = cfg.risk
                if strike:  # a cheaper strike: same index-point stop, so fewer premium points (scaled by delta)
                    delta = {1: 0.4, 2: 0.3, 3: 0.22}.get(s_off, 0.4)
                if ivc:
                    sig["iv"], sig["iv_level"] = ivc["iv"], ivc["level"]
                    delta = ivc.get("delta") or delta
                    why.append({"group": "contract", "ok": ivc["action"] != "skip", "label": ivc["label"]})
                    if ivc["action"] == "skip":
                        problems.append(ivc["label"])
                    elif ivc["action"] == "small":
                        self._careful = True
                if strike and cfg.risk.get("sl_mode") == "points":
                    k = min(1.0, delta / delta_for(cfg.strike_method))
                    risk0 = {**cfg.risk, "sl_value": max(round(cfg.risk["sl_value"] * k, 1), 1.0)}
                    if cfg.risk.get("trail_mode") == "points":
                        risk0["trail_value"] = risk0["sl_value"]
                sig.update(lots=self._lot_cap(), quantity=c.lot_size * self._lot_cap())
                wall = self._oi_wall(c, spot, ot, now)
                if wall:
                    sig["oi_wall"] = wall
                spec = PositionSpec(exchange=c.exchange, tradingsymbol=c.tradingsymbol, instrument_token=c.instrument_token,
                                    lots=self._lot_cap(), lot_size=c.lot_size, side=pside, product=cfg.product, tick_size=c.tick_size,
                                    instrument_type=ot, strike=c.strike, expiry=c.expiry, source=f"auto:{self.id}", tag=f"ka{self.id}")
                pos = ManagedPosition(spec, RiskConfig.from_dict(risk0), cfg.mode, self.deps.kite_getter, slippage_bps=cfg.slippage_bps)
                pos.entry_band_pts = self._entry_band(ref)
                pv = pos.preview(ref)
                base_risk = risk0
                ss = self._structure_stop(spot, 1 if ot == "CE" else -1, abs(pv["entry"] - pv["stop_loss"]), delta) if pside > 0 else None
                if ss:
                    pts, lab = ss
                    base_risk = {**risk0, "sl_mode": "points", "sl_value": pts}
                    if risk0.get("trail_mode") == "points":
                        base_risk["trail_value"] = pts
                    pos = ManagedPosition(spec, RiskConfig.from_dict(base_risk), cfg.mode, self.deps.kite_getter, slippage_bps=cfg.slippage_bps)
                    pos.entry_band_pts = self._entry_band(ref)
                    pv = pos.preview(ref)
                    sig["why"].append({"group": "risk", "ok": True, "label": lab})
                budget0 = cfg.max_daily_loss + min(self.day_pnl, 0) - self.open_risk()
                if cfg.autopilot:  # keep room for at least two trades a day
                    budget0 = min(budget0, cfg.max_daily_loss / 2)
                risk_cap = cfg.account_capital * cfg.risk_per_trade_pct / 100 if cfg.account_capital else None
                risk_dict = base_risk
                if risk_cap:
                    budget0 = min(budget0, risk_cap)
                if cfg.fit_lots_to_limits and self._lot_cap() > 1 and pside > 0 and (pv["order_value"] > cfg.max_capital or pv["max_loss"] > budget0):
                    per_val, per_loss = pv["order_value"] / self._lot_cap(), pv["max_loss"] / self._lot_cap()
                    fit = min(self._lot_cap(), int(cfg.max_capital // per_val) if per_val else self._lot_cap(),
                              int(budget0 // per_loss) if per_loss else self._lot_cap())
                    if fit < 1 and cfg.autopilot and cfg.auto_money and cfg.account_capital:
                        # one lot is just above the normal risk: allow it up to the 1.5% hard cap (and the day's budget),
                        # otherwise still show 1 lot so the reason below is clear
                        hard = cfg.account_capital * self.MAX_RISK_PCT / 100
                        day_left = cfg.max_daily_loss + min(self.day_pnl, 0) - self.open_risk()
                        if per_loss <= hard and per_loss <= day_left and per_val <= cfg.max_capital:
                            risk_cap = hard
                            sig["why"].append({"group": "risk", "ok": True, "label":
                                               f"1 lot risks ₹{per_loss:,.0f} — a little above the normal ₹{budget0:,.0f}, within the "
                                               f"{self.MAX_RISK_PCT:g}% hard limit (₹{hard:,.0f}), so 1 lot is allowed"})
                        elif per_val <= cfg.max_capital:
                            # the stop comes from the market (volatility + levels), never from the money: squeezing it
                            # to fit the budget puts it inside normal noise and it gets hit far more often
                            problems.append(f"1 lot is too big: risks ₹{per_loss:,.0f} at a market-based stop "
                                            f"(cap {self.MAX_RISK_PCT:g}% = ₹{hard:,.0f}"
                                            + (f", ₹{day_left:,.0f} left today" if day_left < hard else "") + ")")
                        fit = 1
                    if fit >= 1:
                        spec.lots = fit
                        pos = ManagedPosition(spec, RiskConfig.from_dict(risk_dict), cfg.mode, self.deps.kite_getter, slippage_bps=cfg.slippage_bps)
                        pos.entry_band_pts = self._entry_band(ref)
                        pv = pos.preview(ref)
                        sig.update(lots=fit, quantity=c.lot_size * fit)
                        sig["why"].append({"group": "risk", "ok": True, "label":
                                           f"Lots reduced {self._lot_cap()} → {fit} so the order fits max capital ₹{cfg.max_capital:,.0f} "
                                           f"and the remaining daily risk ₹{budget0:,.0f}"})
                if wall and pside > 0:
                    room = round(delta * abs(wall["strike"] - spot) * 0.9, 2)  # premium points until just before the wall
                    wall["room_pts"] = room
                    stop_pts = abs(pv["entry"] - pv["stop_loss"])
                    tgt_pts = abs(pv["target"] - pv["entry"]) if pv["target"] is not None else None
                    if room < 0.6 * stop_pts:
                        problems.append(f"{ot} open-interest wall at {wall['strike']:g} ({wall['oi_x']}x usual) only "
                                        f"{abs(wall['strike'] - spot):,.0f} pts away — {room:.1f} pts room vs {stop_pts:.1f}-pt stop")
                    elif tgt_pts is None or room < tgt_pts:
                        pos.stats["target_cap_pts"] = room
                        pv = pos.preview(ref)
                        sig["why"].append({"group": "contract", "ok": True, "label":
                                           f"Target set just before the big {ot} OI wall at {wall['strike']:g} "
                                           f"({wall['oi_x']}x usual OI) → +{room:.1f} pts instead of "
                                           f"{'riding with the stop' if tgt_pts is None else f'+{tgt_pts:.1f}'}"})
                    else:
                        sig["why"].append({"group": "contract", "ok": True, "label":
                                           f"Nearest big {ot} OI wall {wall['strike']:g} is beyond the target — room to run"})
                if pv["target"] is not None and pv["qty"]:
                    tgt_pts = abs(pv["target"] - pv["entry"])
                    cost_pts = round(pv["est_charges"] / pv["qty"] + 2 * cfg.slippage_bps / 10000 * pv["entry"], 2)
                    sig["cost_pts"] = cost_pts
                    if cfg.min_move_cost_ratio and tgt_pts < cfg.min_move_cost_ratio * cost_pts:
                        problems.append(f"Target +{tgt_pts:.1f} pts is under {cfg.min_move_cost_ratio:g}x its costs "
                                        f"({cost_pts:.1f} pts) — not worth it")
                    elif cfg.min_move_cost_ratio:
                        sig["why"].append({"group": "risk", "ok": True, "label":
                                           f"Target +{tgt_pts:.1f} pts is {tgt_pts / cost_pts:.1f}x the costs ({cost_pts:.1f} pts) — worth taking"})
                sig["why"].append({"group": "contract", "ok": True, "label":
                                   f"{c.tradingsymbol}: {c.moneyness} strike {c.strike:g} for spot {spot:,.0f}, expiry {c.expiry}"})
                sig.update(entry_price=pv["entry"], stop_loss=pv["stop_loss"], target_price=pv["target"],
                           risk_reward_ratio=pv["risk_reward"], est_max_loss=pv["max_loss"], est_charges=pv["est_charges"],
                           order_value=pv["order_value"])
                if pside > 0 and pv["order_value"] > cfg.max_capital:
                    problems.append(f"Premium ₹{pv['order_value']:,.0f} above max capital ₹{cfg.max_capital:,.0f}")
                if risk_cap and pv["max_loss"] > risk_cap:
                    problems.append(f"1 lot is too big: could lose ₹{pv['max_loss']:,.0f}, limit ₹{risk_cap:,.0f} per trade "
                                    f"({risk_cap / cfg.account_capital * 100:.1f}% of ₹{cfg.account_capital:,.0f})")
                budget = cfg.max_daily_loss + min(self.day_pnl, 0) - self.open_risk()
                if pv["max_loss"] > budget:
                    problems.append(f"Over the daily risk budget: could lose ₹{pv['max_loss']:,.0f}, ₹{max(budget, 0):,.0f} left today"
                                    + (" (open trades counted)" if self.active_positions() else ""))
                if cfg.mode == "live" and self.deps.margin_available and self.deps.order_margin:
                    need = self.deps.order_margin({"exchange": c.exchange, "tradingsymbol": c.tradingsymbol,
                                                   "transaction_type": "BUY" if pside > 0 else "SELL", "variety": "regular",
                                                   "product": cfg.product, "order_type": "MARKET", "quantity": sig["quantity"]})
                    have = self.deps.margin_available()
                    sig["margin_required"], sig["margin_available"] = round(need, 2), round(have, 2)
                    if need > have:
                        problems.append(f"Insufficient margin: need ₹{need:,.0f}, available ₹{have:,.0f}")
            except (ValueError, RiskError, KeyError) as e:
                problems.append(str(e))
            except Exception as e:  # data/broker failure -> fail safe (no order)
                problems.append(f"{type(e).__name__}: {e}")
        sig["risk_problems"] = problems
        sig["risk_check"] = "PASS" if not problems else "FAIL"
        if sig.get("entry_price") is not None and cfg.entry_tolerance_pts:
            band = max(sig["entry_price"] + (1 if pside > 0 else -1) * cfg.entry_tolerance_pts, 0.05)
            sig["entry_limit"] = round(band, 2)
            sig["why"].append({"group": "contract", "ok": True, "label":
                               f"LIMIT entry: {'buy at or below' if pside > 0 else 'sell at or above'} ₹{band:.2f} "
                               f"(quote {sig['entry_price']} {'+' if pside > 0 else '−'} ₹{cfg.entry_tolerance_pts:g}); cancelled if not filled in {cfg.signal_valid_seconds}s"})
        if sig.get("est_max_loss") is not None:
            sig["why"].append({"group": "risk", "ok": not problems, "label":
                               f"Stop-loss {sig['stop_loss']}, target {sig['target_price']} → max loss ≈ ₹{sig['est_max_loss']:,.0f}, "
                               f"reward:risk 1:{sig['risk_reward_ratio']}"})
        for pmsg in problems:
            sig["why"].append({"group": "risk", "ok": False, "label": pmsg})
        self._careful = prev_careful
        return sig, (pos if not problems else None), ref

    IV_VIX_MULT = {"NIFTY": 1.0, "BANKNIFTY": 1.2, "SENSEX": 1.05}  # usual index IV relative to India VIX

    def _structure_stop(self, spot: float, d: int, stop_pts: float, delta: float) -> tuple[float, str] | None:
        """Don't park the stop right at a level price likes to test (VWAP, pivots, yesterday's / today's high-low):
        a retest wicks a little through it and takes the stop, then the move carries on. Move the stop a buffer beyond
        such a level (0.4 of a candle's range). Only ever widens, at most 1.6x; lots are then sized from the wider stop."""
        h = self.history
        if h is None or len(h) < 30 or not stop_pts or not delta or not spot:
            return None
        try:
            a = float(ind.atr(h, 14).iloc[-1])
            today = h[h["date"].dt.date == h["date"].iloc[-1].date()]
            vw = float(ind.session_vwap(today).iloc[-1]) if len(today) else None
        except Exception:
            return None
        if not a or a != a:
            return None
        lv = self.levels or {}
        levels = [("today's VWAP", vw), ("yesterday's pivot", lv.get("P")), ("yesterday's high", lv.get("prev_high")),
                  ("yesterday's low", lv.get("prev_low")), ("R1", lv.get("R1")), ("S1", lv.get("S1"))]
        if len(today):
            levels += [("today's high", float(today["high"].max())), ("today's low", float(today["low"].min()))]
        dist = stop_pts / delta  # stop distance in index points
        S = spot - d * dist  # index level where the stop sits (below for a CE, above for a PE)
        near = [(n, float(L)) for n, L in levels if L
                and (spot - float(L)) * d > 0.2 * a  # on the stop side of the price
                and -0.4 * a <= (float(L) - S) * d <= 0.6 * a]  # the stop is just before / just past it
        if not near:
            return None
        name, L = min(near, key=lambda x: x[1] * d)  # the one farthest out
        new_dist = (spot - (L - d * 0.4 * a)) * d
        if new_dist <= dist or new_dist > 1.6 * dist:
            return None
        pts = round(new_dist * delta, 1)
        return pts, (f"Stop placed beyond {name} {L:,.0f} (index stop ≈{L - d * 0.4 * a:,.0f}) instead of right at it — "
                     f"{stop_pts:.1f} → {pts} premium pts, so a normal retest of that level doesn't take it out")

    def _iv_check(self, c, premium: float, spot: float, now: datetime, ot: str) -> dict | None:
        """Is the option expensive right now? Its implied volatility vs. this index's own recent IV (once ~5 days of
        samples exist) or vs. India VIX. Buying expensive premium loses even when the direction is right (IV falls back)."""
        if self.cfg.iv_mode == "off" or not premium or not spot:
            return None
        try:
            yrs = years_to_expiry(now, date.fromisoformat(str(c.expiry)[:10]))
        except Exception:
            return None
        if yrs * 365 < 0.6:
            return None  # expiry day: IV numbers swing wildly and mean little
        iv = implied_vol(premium, spot, c.strike, yrs, ot)
        if iv is None:
            return None
        day = now.date().isoformat()
        hist = self.iv_hist.setdefault(self.cfg.index, [])
        past = [v for d, v in hist if d < day]
        pct = None
        if len(past) >= 20 and len({d for d, _ in hist if d < day}) >= 5:
            pct = round(sum(1 for v in past if v < iv) / len(past) * 100)
        last = getattr(self, "_iv_sampled", {}).get(self.cfg.index)
        if last is None or (now - last).total_seconds() >= 900:  # one sample per 15 min
            hist.append([day, iv])
            self.iv_hist[self.cfg.index] = hist[-300:]
            self._iv_sampled = {**getattr(self, "_iv_sampled", {}), self.cfg.index: now}
        vix = self.vix()
        ratio = round(iv / (vix * self.IV_VIX_MULT.get(self.cfg.index, 1.0)), 2) if vix else None
        if pct is not None:
            level = "extreme" if pct >= 95 else "high" if pct >= 85 else "normal" if pct >= 20 else "cheap"
            basis = f"higher than {pct}% of its recent readings"
        elif ratio is not None:
            level = "extreme" if ratio >= 1.6 else "high" if ratio >= 1.35 else "normal" if ratio >= 0.85 else "cheap"
            basis = f"{ratio:.2f}x the usual level for VIX {vix:.1f}"
        else:
            level, basis = "normal", "no comparison yet"
        if ratio is not None and ratio >= 1.6:
            level = "extreme"
        action = ("skip" if level == "extreme" or (level == "high" and self.cfg.iv_mode == "skip")
                  else "small" if level == "high" else "ok")
        what = {"skip": "skipped — buying this premium now means overpaying", "small": "1 lot only",
                "ok": "fair price to buy" if level != "cheap" else "cheap — good time to buy options"}[action]
        return {"iv": iv, "pct": pct, "ratio": ratio, "level": level, "action": action,
                "delta": round(bs_delta(spot, c.strike, yrs, iv, ot), 2),
                "label": f"Option IV {iv:.1f}% is {level} ({basis}) → {what}"}

    def _oi_wall(self, c, spot: float, ot: str, now: datetime) -> dict | None:
        """Biggest open-interest strike in the trade's direction (call OI above spot for a CE, put OI below for a PE):
        option sellers defend it, so the index often stalls there. Only a clear wall counts (>= 1.5x the usual OI)."""
        if not self.cfg.oi_walls:
            return None
        ck = (self.cfg.index, str(c.expiry), ot)
        hit = self._oi_cache.get(ck)
        if hit and (now - hit[0]).total_seconds() < 60:
            rows = hit[1]
        else:
            try:
                ch = chain(self.deps.instruments(), self.cfg.index, str(c.expiry)[:10])
                side = ch[ch["instrument_type"] == ot]
                ks = sorted(set(float(k) for k in side["strike"]))
                step = float(np.median(np.diff(ks))) if len(ks) > 2 else 0.0
                # the at-the-money strike always carries big OI — only strikes at least one step beyond the price can be a wall
                side = side[side["strike"] >= spot + step] if ot == "CE" else side[side["strike"] <= spot - step]
                side = side.sort_values("strike", ascending=(ot == "CE")).head(8)
                keys = [f"{r['exchange']}:{r['tradingsymbol']}" for _, r in side.iterrows()]
                if len(keys) < 4:
                    return None
                q = self.deps.quote(keys)
                rows = [(float(r["strike"]), float((q.get(k) or {}).get("oi") or 0)) for (_, r), k in zip(side.iterrows(), keys)]
            except Exception:
                return None
            self._oi_cache[ck] = (now, rows)
        ois = [o for _, o in rows if o > 0]
        if len(ois) < 4:
            return None
        med = float(np.median(ois))
        strike, oi = max(rows, key=lambda x: x[1])
        if med <= 0 or oi < 1.5 * med:
            return None
        return {"strike": strike, "oi": oi, "oi_x": round(oi / med, 1)}

    VIX_KEY = "NSE:INDIA VIX"

    def vix(self) -> float | None:
        """India VIX, cached for 5 minutes. None when it can't be read (filters then let trades through)."""
        cache = getattr(self, "_vix_cache", None)
        if cache and (_time.monotonic() - cache[0]) < 300:
            return cache[1]
        val = None
        try:
            q = self.deps.quote([self.VIX_KEY]).get(self.VIX_KEY) or {}
            val = float(q.get("last_price")) if q.get("last_price") else None
        except Exception:
            val = None
        self._vix_cache = (_time.monotonic(), val)
        return val

    def entry_filters(self, now: datetime, keys: list[str]) -> list[str]:
        """Market-condition checks for a NEW entry. Returns the reasons to stay out (empty = OK)."""
        cfg, out = self.cfg, []
        from .events import event_on
        if cfg.cooldown_after_loss_bars:
            last = None
            for p in self.all_positions():
                if p.state == "POSITION_CLOSED" and p.net <= 0 and p.exit_time:
                    try:
                        t = datetime.fromisoformat(p.exit_time).replace(tzinfo=None)
                    except ValueError:
                        continue
                    if t <= now and (last is None or t > last):
                        last = t
            wait = timedelta(minutes=self.builder.minutes * cfg.cooldown_after_loss_bars)
            if last and now - last < wait:
                out.append(f"Cooling down after a losing trade (closed {last:%H:%M}) — no new entry until {last + wait:%H:%M}, "
                           f"so one bad move doesn't trigger a quick second loss")
        if cfg.expiry_day_mode != "normal" and self.is_expiry_day(now.date()):
            if cfg.expiry_day_mode == "skip":
                out.append(f"{cfg.index} expiry day — your setting skips new trades today (prices jump and decay fast)")
            elif now.time() >= parse_hm("13:30"):
                out.append(f"{cfg.index} expiry day — no new trades after 13:30 (careful mode)")
        if cfg.event_day_mode == "skip":
            ev = event_on(now.date(), self.deps.data_dir)
            if ev:
                out.append(f"{ev} today — your setting skips new trades on event days (sudden gaps)")
        if cfg.skip_lunch and "trend_day" not in keys and parse_hm(cfg.lunch_start) <= now.time() < parse_hm(cfg.lunch_end):
            out.append(f"Lunch-hour filter: no new entries {cfg.lunch_start}–{cfg.lunch_end} (slow, choppy moves)")
        if cfg.max_vix:
            v = self.vix()
            if v is not None and v > cfg.max_vix:
                out.append(f"India VIX {v:.1f} is above your limit {cfg.max_vix:g} — market too nervous for new entries")
        if cfg.min_adx and not any(k in REVERSION for k in keys) and len(self.history) >= 40:
            a = float(ind.adx(self.history, 14)[0].iloc[-1])
            if a == a and a < cfg.min_adx:  # NaN-safe
                out.append(f"Chop filter: index ADX {a:.1f} is below {cfg.min_adx:g} — no clear trend, trend entries skipped")
        return out

    def trade_strike_method(self) -> str:
        if self.cfg.autopilot:
            return "ITM" if self.cfg.autopilot_itm else "ATM"
        return self.cfg.strike_method if self.cfg.strike_method in ("ATM", "ITM", "OTM") else "ATM"

    def is_expiry_day(self, d: date) -> bool:
        try:
            return d.isoformat() in expiries(self.deps.instruments(), self.cfg.index, d)[:1]
        except Exception:
            return False

    def day_caution(self, now: datetime) -> list[str]:
        """Why today needs care: the index's expiry day and/or a market-moving event."""
        from .events import event_on
        out = []
        if self.cfg.expiry_day_mode != "normal" and self.is_expiry_day(now.date()):
            out.append(f"{self.cfg.index} expiry day")
        if self.cfg.event_day_mode != "off":
            ev = event_on(now.date(), self.deps.data_dir)
            if ev:
                out.append(ev)
        return out

    def smart_manage(self, pos: ManagedPosition, candle: dict) -> str | None:
        """Re-decide an open trade on every closed candle, from what the index is doing now:
        1. market turned against the trade (crossed VWAP the wrong way and most indicators now disagree) → exit;
        2. in profit but momentum has faded (few indicators still agree) → bank the profit;
        3. lock in profit as it grows (+1R → stop at +0.5R, +1.5R → stop at +1R);
        4. strong, still-building move near the target → remove the fixed target and ride it with the trailing stop.
        Returns what it did (for tests/logs) or None."""
        if not self.cfg.smart_exit or not pos.is_open or pos.ltp is None or not pos.entry_price or not pos.risk_unit:
            return None
        if len(self.history) < 60:
            return None
        d = 1 if pos.spec.instrument_type == "CE" else -1  # we only buy options: CE = bet up, PE = bet down
        try:
            with_pct, _ = confidence_for(self.history, d)
            vw = float(ind.session_vwap(self.history).iloc[-1])
        except Exception:
            return None
        self._agree[pos.id] = with_pct
        px = float(candle["close"])
        r = (pos.ltp - pos.entry_price) * pos.spec.side / pos.risk_unit
        wrong_side = (px < vw) if d > 0 else (px > vw)
        rule = getattr(pos, "exit_rule", None) or pos.stats.get("exit_rule") or [None]
        reversion = (rule[0] if rule else None) in REVERSION

        def note(msg):
            self.log(f"AI exit check ({pos.spec.tradingsymbol}): {msg}", "risk")

        if wrong_side and with_pct <= 33 and not reversion:
            note(f"market turned — {self.cfg.index} closed {'below' if d > 0 else 'above'} VWAP {vw:,.1f} and only {with_pct}% of "
                 f"indicators still agree → closing at {r:+.2f}R")
            pos.event("risk", f"AI exit: market turned against the trade ({with_pct}% agree, wrong side of VWAP)")
            pos.request_exit("ai_exit")
            return "exit_turned"
        if r >= 0.5 and with_pct <= 33:
            note(f"momentum faded — only {with_pct}% of indicators agree now → booking {r:+.2f}R")
            pos.event("risk", f"AI exit: momentum faded while in profit ({with_pct}% agree)")
            pos.request_exit("ai_exit")
            return "exit_faded"
        did = None
        lock = 1.0 if r >= 1.5 else 0.5 if r >= 1.0 else None
        if lock is not None:
            new_stop = pos.entry_price + pos.spec.side * lock * pos.risk_unit
            if pos.tighten_stop(new_stop, f"AI locks +{lock}R profit (trade is at {r:+.2f}R)"):
                note(f"locked +{lock}R profit — stop moved to {new_stop:.2f}")
                did = "locked"
        if pos.target is not None and with_pct >= 83 and not wrong_side:
            to_target = (pos.target - pos.ltp) * pos.spec.side
            if to_target <= 0.3 * pos.risk_unit and r >= 0.8:
                if pos.lift_target(f"strong move still building ({with_pct}% of indicators agree) — riding it with the stop"):
                    pos.tighten_stop(pos.entry_price + pos.spec.side * max(0.5, r - 1.0) * pos.risk_unit, "protect the ride")
                    note(f"target lifted — {with_pct}% agree, riding the move with a stop at least +0.5R")
                    did = "lifted"
        return did

    RISK_EVERY_S = 10

    def live_risk(self, pos: ManagedPosition, now: datetime, force: bool = False) -> dict | None:
        """Continuous risk check of an open trade (every ~10 s, between candles too), in ₹ for its actual lots:
        - scale out: 2+ lots at +1R → book half, stop to entry on the rest (the rest is now a free ride);
        - give-back: was +1R or better and has handed back 60% of the best profit → close and keep the rest;
        - sharp move against: index ran 0.8 candle-ranges against the trade in 2 min, trade at -0.4R or worse and on the
          wrong side of VWAP → close early instead of waiting for the full stop;
        - stalling at the target: within 15% of the target and the index is turning back → book now.
        Every check is written to pos.stats["risk_live"] so the app shows what it sees."""
        cfg = self.cfg
        if not (cfg.smart_exit and cfg.live_risk) or not pos.is_open or pos.ltp is None or not pos.entry_price or not pos.risk_unit:
            return None
        t = _time.monotonic()
        if not force and t - self._risk_at.get(pos.id, 0) < self.RISK_EVERY_S:
            return None
        self._risk_at[pos.id] = t
        s, q = pos.spec.side, pos.filled_qty
        d = 1 if pos.spec.instrument_type == "CE" else -1
        r = (pos.ltp - pos.entry_price) * s / pos.risk_unit
        peak = (pos.stats.get("mfe_pts") or 0) / pos.risk_unit
        try:
            a = float(ind.atr(self.history, 14).iloc[-1]) if len(self.history) >= 20 else 0.0
            today = self.history[self.history["date"].dt.date == self.history["date"].iloc[-1].date()]
            vw = float(ind.session_vwap(today).iloc[-1]) if len(today) else None
        except Exception:
            a, vw = 0.0, None
        spot = self.last_spot
        ago = [p for ts, p in self._spot_trail if (now - ts).total_seconds() >= 110] if self._spot_trail else []
        base = ago[-1] if ago else (self._spot_trail[0][1] if self._spot_trail else None)
        vel = ((spot - base) * d / a) if (a and spot and base) else 0.0  # + = index moving our way, in candle ranges / 2 min
        wrong_vwap = bool(vw and spot and (spot - vw) * d < 0)
        agree = self._agree.get(pos.id)
        at_risk = max(0.0, (pos.ltp - pos.stop) * s * q) if pos.stop is not None else None
        locked = (pos.stop - pos.entry_price) * s * q if pos.stop is not None else 0.0
        danger = 0
        danger += 35 if r <= -0.5 else 20 if r < 0 else 0
        danger += 25 if vel <= -0.8 else 12 if vel <= -0.4 else 0
        danger += 20 if wrong_vwap else 0
        danger += 20 if (agree is not None and agree <= 33) else 0
        danger = min(100, danger)
        info = {"at": now.isoformat(timespec="seconds"), "r": round(r, 2), "peak_r": round(peak, 2), "velocity": round(vel, 2),
                "wrong_vwap": wrong_vwap, "agree_pct": agree, "danger": danger, "open_pnl": round((pos.ltp - pos.entry_price) * s * q, 2),
                "at_risk": None if at_risk is None else round(at_risk, 2), "locked": round(max(0.0, locked), 2), "lots_running": q // pos.spec.lot_size,
                "action": None}
        pos.stats["risk_live"] = info

        def act(what, msg, exit_reason=None):
            info["action"] = msg
            self.log(f"Risk monitor ({pos.spec.tradingsymbol}): {msg}", "risk")
            pos.event("risk", f"AI risk monitor: {msg}")
            if exit_reason:
                pos.request_exit(exit_reason)
            return what

        lots = q // pos.spec.lot_size if pos.spec.lot_size else 0
        # 1. scale out half the lots at +1R, rest rides free
        if cfg.scale_out and lots >= 2 and r >= 1.0 and not pos.stats.get("scaled_out"):
            half = (lots // 2) * pos.spec.lot_size
            if pos.book_partial(half, "scale_out"):
                pos.stats["scaled_out"] = True
                pos.tighten_stop(pos.entry_price + s * max(0.05 * pos.risk_unit, pos.spec.tick_size), "rest rides free after booking half")
                gain = (pos.ltp - pos.entry_price) * s * half
                act("scaled_out", f"+{r:.2f}R: booked {half // pos.spec.lot_size} of {lots} lots (≈₹{gain:,.0f}) and moved the stop to "
                                  f"entry for the other {(q - half) // pos.spec.lot_size} — the rest can't lose now")
                return info
        # 2. give-back: was well in profit, handing most of it back
        if peak >= 1.0 and r <= 0.4 * peak:
            kept = (pos.ltp - pos.entry_price) * s * q
            act("giveback", f"was +{peak:.2f}R, now {r:+.2f}R — handed back {100 - (r / peak * 100 if peak else 0):.0f}% of the best "
                            f"profit; closing to keep ₹{kept:,.0f}", "ai_giveback")
            return info
        # 3. sharp move against the trade
        if r <= -0.4 and vel <= -0.8 and wrong_vwap:
            saved = (at_risk or 0)
            act("sharp", f"{cfg.index} ran {abs(vel):.1f} candle-ranges against the trade in 2 min and is on the wrong side of VWAP "
                         f"(trade {r:+.2f}R) — closing now, saving up to ₹{saved:,.0f} of the remaining stop risk", "ai_sharp_move")
            return info
        # 4. close to the target and turning back
        if pos.target is not None:
            full = abs(pos.target - pos.entry_price)
            left = (pos.target - pos.ltp) * s
            if full and 0 < left <= 0.15 * full and vel <= -0.3 and r > 0:
                act("near_target", f"{left:.1f} pts from the target but {cfg.index} is turning back — booking ₹"
                                   f"{(pos.ltp - pos.entry_price) * s * q:,.0f} now", "ai_near_target")
                return info
        return info

    def check_quick_target(self, candle: dict, pos: ManagedPosition | None = None) -> str | None:
        """Slow trade: the full target is far and still not hit after N candles → bring it down to entry + X points
        (book a sure, smaller profit instead of waiting while time decay eats the premium). Once per trade.
        A target the AI already removed to ride a strong move is left alone."""
        pos, pts, n = pos or self.position, self.cfg.quick_target_pts, self.cfg.quick_target_after_bars
        if not pts or pos is None or not pos.is_open or not pos.entry_time or pos.ltp is None or not pos.entry_price:
            return None
        if pos.target is None or pos.stats.get("quick_target"):
            return None
        pts = self.quick_target_points(pos)
        s = pos.spec.side
        if (pos.target - pos.entry_price) * s <= pts:
            return None  # target is already that close
        held = (candle["date"] + timedelta(minutes=self.builder.minutes) - datetime.fromisoformat(pos.entry_time)).total_seconds()
        bars = held / 60 / self.builder.minutes
        if bars < n:
            return None
        new = pos.entry_price + s * pts
        pos.stats["quick_target"] = round(new, 2)
        gain = (pos.ltp - pos.entry_price) * s
        if gain >= pts:
            msg = (f"Quick target: full target {pos.target:.2f} still not hit after {bars:.0f} candles and the trade is already "
                   f"+{gain:.1f} pts → booking now")
            self.log(f"{msg} ({pos.spec.tradingsymbol})", "risk")
            pos.event("risk", msg)
            pos.request_exit("quick_target")
            return "booked"
        if pos.lower_target(new, f"slow trade — full target {pos.target:.2f} not hit after {bars:.0f} candles, "
                                 f"taking +{pts:g} pts instead"):
            self.log(f"Quick target ({pos.spec.tradingsymbol}): target lowered to {new:.2f} (+{pts:g} pts) after {bars:.0f} slow candles", "risk")
            return "lowered"
        return None

    def quick_target_points(self, pos: ManagedPosition) -> float:
        """Quick target in premium points. Auto: sized to this trade — the smaller of 0.8x its stop distance and 15% of the
        premium (20 pts is a lot on a ₹90 option and little on a ₹400 one), but never below 2.5x its costs."""
        if not self.cfg.quick_target_auto:
            return float(self.cfg.quick_target_pts)
        risk = pos.risk_unit or abs(pos.entry_price - (pos.stop0 or pos.entry_price))
        pts = min(0.8 * risk, 0.15 * pos.entry_price) if risk else 0.15 * pos.entry_price
        cost = pos.stats.get("cost_pts") or (2 * self.cfg.slippage_bps / 10000 * pos.entry_price)
        return round(max(pts, 2.5 * cost, 1.0), 1)

    def check_time_stop(self, candle: dict, pos: ManagedPosition | None = None) -> bool:
        """Exit a trade that has gone nowhere after N candles (frees the slot, limits time decay)."""
        pos, n = pos or self.position, self.cfg.time_stop_bars
        if not n or pos is None or not pos.is_open or not pos.entry_time or not pos.risk_unit or pos.ltp is None:
            return False
        held = (candle["date"] + timedelta(minutes=self.builder.minutes) - datetime.fromisoformat(pos.entry_time)).total_seconds()
        bars = held / 60 / self.builder.minutes
        if bars < n:
            return False
        r = (pos.ltp - pos.entry_price) * pos.spec.side / pos.risk_unit
        if r >= self.cfg.time_stop_min_r:
            return False
        msg = (f"Time-stop: after {bars:.0f} candles the trade is only {r:+.2f}R (needs {self.cfg.time_stop_min_r:g}R) — "
               f"exiting before time decay eats the premium")
        self.log(msg, "risk")
        try:
            pos.event("risk", msg)
        except Exception:
            pass
        pos.request_exit("time_stop")
        return True

    @staticmethod
    def entry_type_for(late_key: str | None, override: bool | str, fired_by: tuple | None) -> str:
        if fired_by and fired_by[0] == "trend_day" and str(late_key or "").startswith("trendday:"):
            return "trendday"
        if str(late_key or "").startswith("pullback:"):
            return "pullback"
        if str(late_key or "").startswith("daybuilt:"):
            return "daybuilt"
        if override == "consensus":
            return "agreement"
        if override:
            return "override"
        if fired_by:
            return "any"
        if late_key:
            return "late"
        return "strategy"

    def execute_signal(self, direction: int, candle: dict, now: datetime, late_key: str | None = None, late_note: str | None = None,
                       override: bool | str = False, fired_by: tuple | None = None):
        key = late_key or f"{candle['date']}"
        if key in self.processed:
            return  # duplicate signal guard
        self.processed.append(key)
        if not self.enabled or self.paused:
            self.log(f"Signal at {candle['date']:%H:%M} not traded ({'paused' if self.paused else 'disabled'})", "signal")
            return
        if not self.slot_free():
            return  # all position slots in use
        self._set("SIGNAL_READY", f"{'Bullish' if direction > 0 else 'Bearish'} setup confirmed at {candle['date']:%H:%M}")
        self._set("RISK_VALIDATION", "Selecting contract and checking risk")
        self._account_closed()  # a trade that just closed on this same candle must count in today's P&L / limits first
        try:
            self.apply_auto_money(now)  # re-check risk (losing streak, VIX) right before sizing this trade
            self._size_for_setup(direction, self.entry_type_for(late_key, override, fired_by))
        except Exception:
            pass
        caution = [c for c in self.day_caution(now)
                   if (c.endswith("expiry day") and self.cfg.expiry_day_mode == "careful")
                   or (not c.endswith("expiry day") and self.cfg.event_day_mode == "careful")]
        self._careful = bool(caution)
        try:
            sig, pos, ref = self.build_signal(direction, candle["date"], now)
        finally:
            self._careful = False
        if caution:
            sig["why"].append({"group": "risk", "ok": True, "label": f"{' + '.join(caution)}: careful mode, 1 lot only"})
        sz = getattr(self, "sizing", None)
        if sz and self.cfg.autopilot and self.cfg.auto_money:
            names = {"strict_luck": "proven + passed the luck check", "strict": "proven", "daybuilt": "Day-built (proven)",
                     "daybuilt_practice": "Day-built practice", "trendday": "Trend Day rule", "practice": "practice pick",
                     "unproven": "unproven pick", "extra": "extra entry type"}
            sig["why"].append({"group": "risk", "ok": True, "label":
                               f"Size: {names.get(sz['setup'], sz['setup'])} setup"
                               + (f", indicators {sz['confidence']}% agree" if sz.get("confidence") is not None else "")
                               + f" → risk {sz['risk_pct']}% = ₹{sz['risk_rupees']:,.0f}; lots = that ÷ (stop distance × lot size), "
                                 f"so a tighter stop or stronger setup gets more lots"})
        candle_end = candle["date"] + timedelta(minutes=self.builder.minutes)
        etype = self.entry_type_for(late_key, override, fired_by)
        sig["entry_type"] = etype
        if late_note:
            sig["why"].insert(1, {"group": "strategy", "ok": True, "label": late_note})
        keys = [fired_by[0]] if fired_by else [] if override else [(self.active_strategy or (sig.get("strategy"), {}))[0]]
        if self.active_strategy and self.active_strategy[0] == "combo":
            keys = [m.get("strategy") for m in (self.active_strategy[1] or {}).get("members", [])] or keys
        blocks = self.entry_filters(now, [k for k in keys if k])
        lk = str(late_key or "")
        if (self.cfg.stretch_mode != "off" and pos is not None and not blocks and not lk.startswith(("pullback:", "trendday:"))
                and not any(k in REVERSION for k in keys if k)):
            st = self.stretch_check(direction)
            if st and st["stretched"]:
                if self.cfg.stretch_mode == "pullback":
                    self.pending_pullback = {"direction": direction, "since": candle["date"], "rsi": st["rsi"], "dist_atr": st["dist_atr"],
                                             "until": candle["date"] + timedelta(minutes=self.builder.minutes * self.cfg.pullback_wait_bars)}
                    blocks.append(st["label"] + f" → waiting up to {self.cfg.pullback_wait_bars} candles for a pullback to the "
                                                f"20-EMA {st['ema20']:,.0f} and enter when it turns {'up' if direction > 0 else 'down'} again")
                else:
                    blocks.append(st["label"] + " → skipped (your setting)")
        for b in blocks:
            sig["why"].append({"group": "market", "ok": False, "label": b})
        if pos is not None and blocks:
            sig["risk_check"], pos = "FAIL", None
            sig["risk_problems"] += blocks
        if pos is not None and (not late_key or override or fired_by) and (now - candle_end).total_seconds() > self.cfg.signal_valid_seconds:
            sig["risk_check"], pos = "FAIL", None
            sig["risk_problems"].append("Signal went stale before execution")
        self.signals.append(sig)
        self.signals = self.signals[-50:]
        if pos is None:
            from .quality import skip_category
            day = now.date().isoformat()
            dd = self.skips.setdefault(day, {})
            for cat in {skip_category(p) for p in sig["risk_problems"]} or {"other"}:
                dd[cat] = dd.get(cat, 0) + 1
            self.skips = dict(sorted(self.skips.items())[-40:])
            self._set("SIGNAL_REJECTED", "; ".join(sig["risk_problems"])[:300])
            return
        pos.on_change = lambda p: self._changed()
        pos.stats["entry_type"] = etype
        # kept with the position for reports: why it was placed, against what, and at which index level
        pos.stats["why"] = [str(w.get("label", ""))[:160] for w in sig.get("why", []) if w.get("ok", True)][:14]
        pos.stats["strategy_name"] = (REGISTRY[fired_by[0]].name if fired_by and fired_by[0] in REGISTRY
                                      else (self.pick or {}).get("name") or sig.get("strategy"))
        pos.stats["index"] = self.cfg.index
        pos.stats["spot_at_entry"] = self.last_spot
        pos.stats["quote_at_entry"] = ref
        pos.stats["interval"] = self.cfg.interval
        pos.stats["settings_v"] = self.settings_v
        for k in ("iv", "iv_level", "cost_pts", "oi_wall"):
            if sig.get(k) is not None:
                pos.stats[k] = sig[k]
        if self.pick and self.pick.get("test"):
            pos.stats["expected_win_pct"] = self.pick["test"].get("win_rate_pct")
        pk = self.pick or {}
        pos.stats["proof"] = ("practice" if pk.get("practice") else "unproven" if pk.get("unproven")
                              else "strict" if pk.get("strategy") else "manual")
        if etype == "daybuilt":
            pos.stats["proof"] = "practice" if getattr(self, "_day_built_practice", False) else "strict"
        elif etype == "trendday":
            pos.stats["proof"] = "rule"
        if self.position is not None and self.position.is_active:
            self.others.append(self.position)  # still managed with its own exit rule
        self.position = pos
        self.pos_strategy = ((override if isinstance(override, str) else "trend_override", {}) if override
                             else (fired_by[0], fired_by[1]) if fired_by else (sig["strategy"], sig.get("params") or {}))
        pos.exit_rule = self.pos_strategy
        pos.stats["exit_rule"] = list(self.pos_strategy)
        if override:
            sig["override"] = True
        sig["position_id"] = pos.id
        self.position_ids_today.append(pos.id)
        self.trades_today += 1
        self.register_position(pos)
        try:
            pos.open(ref, now)
        except Exception as e:
            self.log(f"Entry failed: {e}", "alert")
        self.log(f"{sig['action']} {sig['tradingsymbol']} x{sig['quantity']} @ ~{sig['entry_price']} SL {sig['stop_loss']} T {sig['target_price']}", "trade")
        self._account_closed()

    # ------------------------------------------------------------ controls
    def enable(self, now: datetime):
        with self.lock:
            self.enabled, self.paused = True, False
            self.enabled_at = now
            if self.day != now.date() or len(self.history) < 60:
                self.start_day(now)  # also after a server restart: reload candles instead of waiting for new ones
            elif self.cfg.strategy_source == "ai" and (self.pick is None or self.pick.get("day") != now.date().isoformat()):
                self.start_pick(now)
            self.log(f"Auto Trade ENABLED ({self.cfg.mode.upper()})", "control")
            self.on_clock(now)
            try:
                self.try_late_join(now)
            except Exception as e:
                self.log(f"Late-join check failed: {e}"[:200], "alert")
            # strategy agreement is a state, so act on it now instead of waiting for the next candle close
            try:
                if len(self.history) >= 60 and self.slot_free():
                    # treat it as decided now (agreement is a current state), so the freshness check passes
                    self.try_consensus({"date": now - timedelta(minutes=self.builder.minutes)}, now)
            except Exception as e:
                self.log(f"Agreement check failed: {e}"[:200], "alert")

    def pause(self):
        with self.lock:
            self.paused = True
            self.log("Paused — no new entries (open position still managed)", "control")

    def resume(self):
        with self.lock:
            self.paused = False
            self.log("Resumed", "control")

    def disable(self):
        with self.lock:
            self.enabled = False
            self.resume_pending = False
            pos = self.position
            for p in self.active_positions():
                if p.state == "ENTRY_PENDING" and p.mode == "live":
                    self._cancel_entry(p, "disabled")
            self._set("IDLE", "Disabled" + (" — open position still managed until closed" if pos and pos.is_open else ""))

    PICK_FIELDS = ("entry_source", "strategy_source", "autopilot", "mode", "strict_proof", "paper_practice", "autopilot_itm", "use_combo",
                   "autopilot_index")
    MANUAL_PICK_FIELDS = ("index", "interval", "strategy", "params", "entry_start", "entry_end", "exit_time")

    # fields the AI / automatic money rewrites by itself — changes there are not "your" settings changes
    AI_MANAGED = {"index", "interval", "strategy", "params", "risk", "option_rule", "strike_method", "strike_offset", "side",
                  "entry_start", "entry_end", "exit_time", "analysis_start", "lots", "expiry", "account_capital",
                  "risk_per_trade_pct", "max_daily_loss", "max_weekly_loss", "max_monthly_loss", "max_capital"}

    def _settings_sig(self, cfg: "AutoConfig | None" = None) -> dict:
        d = asdict(cfg or self.cfg)
        if d.get("autopilot"):
            d = {k: v for k, v in d.items() if k not in self.AI_MANAGED}
        return d

    @property
    def settings_v(self) -> int:
        return int(self.versions[-1]["v"]) if self.versions else 1

    def bump_version(self, old_cfg: "AutoConfig") -> bool:
        """New settings version when your settings really changed, so results are judged per version."""
        a, b = self._settings_sig(old_cfg), self._settings_sig()
        changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        if not changed:
            return False
        self.versions.append({"v": self.settings_v + 1, "since": datetime.now().isoformat(timespec="minutes"), "changes": changed[:15]})
        self.versions = self.versions[-30:]
        self.log(f"Settings version {self.settings_v} (changed: {', '.join(changed[:6])}{'…' if len(changed) > 6 else ''}) — "
                 f"Stats and the go-live scorecard now count trades from this version", "control")
        return True

    def reset_for_new_settings(self, old_cfg: "AutoConfig | None" = None):
        """After an edit: rebuild candles. Today's AI pick is kept when the edit can't change it (e.g. money or safety
        settings), so there is no multi-minute re-test; otherwise it re-picks on the next enable."""
        if old_cfg is not None:
            try:
                self.bump_version(old_cfg)
            except Exception:
                pass
        with self.lock:
            keep = None
            if old_cfg is not None and self.pick and self.pick.get("strategy") \
                    and self.pick.get("day") == datetime.now().date().isoformat():
                fields = self.PICK_FIELDS + (() if self.cfg.autopilot else self.MANUAL_PICK_FIELDS)
                if all(getattr(old_cfg, f) == getattr(self.cfg, f) for f in fields):
                    keep = self.pick
            self.day, self.pick, self.pos_strategy = None, keep, None
            if keep and keep.get("applied") and self.cfg.autopilot:
                for k, v in keep["applied"].items():
                    setattr(self.cfg, k, dict(v) if isinstance(v, dict) else v)
            self.processed = [k for k in self.processed if not str(k).startswith("late:")]
            self.builder = CandleBuilder(self.cfg.interval)
            self.history = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
            if self.day_blocked and self.day_blocked.startswith(("Opening gap", "AI found", "LIVE paused")):
                self.day_blocked = None  # re-checked with the new settings when candles reload
            self.log("Settings changed — " + (f"keeping today's pick ({keep.get('name')}); candles refresh on enable" if keep
                                              else "candles and strategy pick will refresh on enable"), "control")

    def repick(self, now: datetime):
        with self.lock:
            if self.cfg.strategy_source != "ai":
                raise ValueError("This strategy is not set to AI pick")
            if self.active_positions():
                raise ValueError("Close the open position before re-picking")
            if self.day_blocked and self.day_blocked.startswith(("AI found", "LIVE paused")):
                self.day_blocked = None
            self._keep_pick = self.pick if self.pick and self.pick.get("strategy") else None
            self.start_pick(now)

    def use_best_unproven(self, now: datetime):
        """Paper practice: trade the top-ranked strategy even though none passed the proof test."""
        with self.lock:
            if self.cfg.mode != "paper":
                raise PermissionError("Only allowed in PAPER mode — live trading needs a proven strategy")
            tested = (self.pick or {}).get("tested") or []
            cands = [t for t in tested if t["test"]["trades"] >= 3]
            if self.pick is None or self.pick.get("strategy") or not cands:
                raise ValueError("Nothing to choose: no tested strategy with 3+ trades")
            b = max(cands, key=lambda t: t["test"]["net_pnl"])
            self.pick = {**self.pick, "strategy": b["strategy"], "name": b["name"], "params": b["params"],
                         "test": {**b["test"], "total_return_pct": None}, "description": b["description"], "unproven": True}
            if self.day_blocked and self.day_blocked.startswith(("AI found", "LIVE paused")):
                self.day_blocked = None
            self.log(f"Paper practice: using best unproven strategy {b['name']} ({b['why']})", "ai")

    def clear_signals(self):
        """Clear the order/signal history (keeps the record of an order that is still open)."""
        with self.lock:
            keep = {p.id for p in self.active_positions()}
            self.signals = [g for g in self.signals if g.get("position_id") in keep]
            self._changed()

    def clear_log(self):
        with self.lock:
            self.timeline = []
            self._changed()

    def close_position(self):
        with self.lock:
            opn = [p for p in self.active_positions() if p.is_open]
            if not opn:
                raise ValueError("No open position")
            self.log(f"Manual close requested ({len(opn)} open position{'s' if len(opn) > 1 else ''})", "control")
            for p in opn:
                p.request_exit("manual_close")

    def snapshot(self) -> dict:
        pos = self.position
        return {
            "id": self.id, "config": asdict(self.cfg), "enabled": self.enabled, "paused": self.paused,
            "state": self.effective_state, "message": (pos.message if pos is not None and pos.is_active else self.message),
            "day": self.day.isoformat() if self.day else None, "trades_today": self.trades_today,
            "realized_pnl": round(self.day_pnl, 2), "unrealized_pnl": round(sum(p.unrealized() for p in self.active_positions()), 2),
            "open_positions": [{k: v for k, v in p.snapshot().items() if k not in ("path", "events")} for p in self.active_positions()],
            "remaining_risk_budget": round(self.cfg.max_daily_loss + min(self.day_pnl, 0) - self.open_risk(), 2),
            "resume_pending": bool(getattr(self, "resume_pending", False)), "skips": self.skips,
            "loss_streak": self.loss_streak, "period_blocks": self.period_limits(datetime.now().date()),
            "day_blocked": self.day_blocked, "levels": self.levels, "spot": self.last_spot,
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "position": pos.snapshot() if pos else None, "last_signal": self.signals[-1] if self.signals else None,
            "signals": self.signals[-10:], "timeline": self.timeline[-60:],
            "ai_pick": self.pick, "ai_picking": self.picking, "day_build": self.day_build, "day_building": self.building, "money": getattr(self, "money", None), "sizing": getattr(self, "sizing", None), "watch": self.watch, "pick_progress": self.pick_progress,
        }

    LIGHT_DROP = ("signals", "timeline", "watch", "last_signal", "day_build", "open_positions", "levels")

    def light_snapshot(self) -> dict:
        """Small summary for list screens (polled often): no logs, signals or analysis tables."""
        s = self.snapshot()
        for k in self.LIGHT_DROP:
            s.pop(k, None)
        if s.get("ai_pick"):
            s["ai_pick"] = {k: v for k, v in s["ai_pick"].items() if k not in ("tested", "risk_tuning", "applied")}
        if s.get("position"):
            s["position"] = {k: v for k, v in s["position"].items() if k not in ("path", "events")}
        return s

    def to_store(self) -> dict:
        return {"id": self.id, "config": asdict(self.cfg), "enabled": self.enabled, "paused": self.paused,
                "state": self.state, "message": self.message, "day": self.day.isoformat() if self.day else None,
                "trades_today": self.trades_today, "day_pnl": self.day_pnl, "day_blocked": self.day_blocked,
                "counts_for": self.counts_for.isoformat() if getattr(self, "counts_for", None) else None,
                "processed": self.processed[-50:], "signals": self.signals[-20:], "timeline": self.timeline[-60:],
                "position_id": self.position.id if self.position else None, "levels": self.levels,
                "ai_pick": self.pick, "pos_strategy": list(self.pos_strategy) if self.pos_strategy else None,
                "pnl_days": self.pnl_days, "loss_streak": self.loss_streak,
                "resume_pending": bool(getattr(self, "resume_pending", False)), "skips": self.skips,
                "day_build": self.day_build, "other_ids": [p.id for p in self.others if p.is_active],
                "versions": self.versions[-30:], "iv_hist": {k: v[-300:] for k, v in self.iv_hist.items()}}
