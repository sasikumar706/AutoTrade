"""Owns Auto Traders and manual option positions: persistence, restart recovery,
tick/order routing, morning analysis and execution analytics."""
from __future__ import annotations

import threading
import time
from dataclasses import fields
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .analysis import technical_snapshot, to_python
from .autotrade import AutoConfig, AutoTrader, Deps, _best_prices
from .config import settings
from .market_calendar import is_trading_day, phase
from .options import expiries, index_cfg, nearest_future, pivots, quantity, select_contract, spot_instrument
from .positions import ACTIVE_STATES, ManagedPosition, PositionSpec
from .risk import RiskConfig, RiskError
from .swing import SwingConfig, SwingTrader, backtest as swing_backtest

LIVE_CONFIRM = "I UNDERSTAND REAL MONEY"


ENTRY_TYPE_LABELS = {
    "strategy": "Main strategy signal", "any": "Any-strategy signal", "agreement": "Strategies agreeing",
    "override": "Strong-trend override", "late": "Late join", "pullback": "Pullback entry (waited for retest)", "trendday": "Trend Day setup", "daybuilt": "Day-built strategy", "manual": "Manual / other",
}


def _group(ps: list) -> dict:
    nets = [float(p.net) for p in ps]
    wins = [n for n in nets if n > 0]
    return {"trades": len(nets), "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(nets) * 100, 1) if nets else 0.0,
            "net": round(sum(nets), 2), "avg_net": round(sum(nets) / len(nets), 2) if nets else 0.0}


def performance_breakdown(closed: list) -> dict:
    """Results split by how each trade was entered, AI-test vs real win rate (drift), and a per-day journal."""
    by_type: dict[str, list] = {}
    for p in closed:
        by_type.setdefault((p.stats or {}).get("entry_type") or "manual", []).append(p)
    types = {k: {**_group(v), "label": ENTRY_TYPE_LABELS.get(k, k)} for k, v in by_type.items()}
    with_exp = [p for p in closed if (p.stats or {}).get("expected_win_pct") is not None]
    drift = None
    if with_exp:
        exp = sum(float(p.stats["expected_win_pct"]) for p in with_exp) / len(with_exp)
        act = sum(1 for p in with_exp if p.net > 0) / len(with_exp) * 100
        gap = act - exp
        drift = {"trades": len(with_exp), "expected_win_pct": round(exp, 1), "actual_win_pct": round(act, 1),
                 "gap_pct": round(gap, 1),
                 "verdict": ("too few trades to judge yet" if len(with_exp) < 10
                             else "living up to the AI test" if gap >= -10
                             else "doing worse than the AI test — consider stricter filters or paper only")}
    days: dict[str, list] = {}
    for p in closed:
        days.setdefault((p.exit_time or p.created or "")[:10], []).append(p)
    daily = []
    for d in sorted(days, reverse=True)[:30]:
        ps = days[d]
        nets = [float(p.net) for p in ps]
        daily.append({"day": d, **_group(ps), "best": round(max(nets), 2), "worst": round(min(nets), 2),
                      "charges": round(sum(float(p.charges) for p in ps), 2),
                      "by_type": {k: sum(1 for p in ps if ((p.stats or {}).get("entry_type") or "manual") == k)
                                  for k in sorted({(p.stats or {}).get("entry_type") or "manual" for p in ps})},
                      "exits": {r: sum(1 for p in ps if p.exit_reason == r) for r in sorted({p.exit_reason or "?" for p in ps})},
                      "trades_list": [{"symbol": p.spec.tradingsymbol, "net": round(float(p.net), 2), "exit_reason": p.exit_reason,
                                       "entry_type": (p.stats or {}).get("entry_type") or "manual",
                                       "entry_time": (p.entry_time or "")[11:16], "exit_time": (p.exit_time or "")[11:16]}
                                      for p in sorted(ps, key=lambda x: x.entry_time or "")]})
    by_proof: dict[str, list] = {}
    for p in closed:
        by_proof.setdefault((p.stats or {}).get("proof") or "strict", []).append(p)
    proof_labels = {"strict": "Strict proof passed", "practice": "Practice (weaker proof, paper)",
                    "unproven": "Unproven (your choice)", "manual": "Your own strategy", "rule": "Fixed rule (Trend Day)"}
    proofs = {k: {**_group(v), "label": proof_labels.get(k, k)} for k, v in by_proof.items()}
    by_v: dict[int, list] = {}
    for p in closed:
        by_v.setdefault(int((p.stats or {}).get("settings_v") or 1), []).append(p)
    versions = {str(k): {**_group(v), "profit_factor": _pf([float(p.net) for p in v])} for k, v in sorted(by_v.items())}
    return {"by_entry_type": types, "by_proof": proofs, "drift": drift, "daily": daily, "by_version": versions}


def _pf(nets: list[float]) -> float | None:
    gw, gl = sum(n for n in nets if n > 0), -sum(n for n in nets if n <= 0)
    return round(gw / gl, 2) if gl > 0 else None


def _max_dd(nets: list[float]) -> float:
    eq, peak, dd = 0.0, 0.0, 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return round(dd, 2)


GOLIVE = {"trades": 60, "days": 20, "pf": 1.3, "dd_pct": 8.0, "drift": -10.0, "cost_share": 40.0}


def golive_scorecard(closed: list, capital: float | None) -> dict:
    """Pass/fail checks a paper record must meet before real money — decided up front, not after seeing results."""
    ps = sorted([p for p in closed if p.mode == "paper"], key=lambda p: p.exit_time or "")
    nets = [float(p.net) for p in ps]
    days = len({(p.exit_time or "")[:10] for p in ps})
    net, charges = sum(nets), sum(float(p.charges) for p in ps)
    gross = net + charges
    pf, dd = _pf(nets), _max_dd(nets)
    with_exp = [p for p in ps if (p.stats or {}).get("expected_win_pct") is not None]
    gap = None
    if len(with_exp) >= 10:
        gap = sum(1 for p in with_exp if p.net > 0) / len(with_exp) * 100 - sum(float(p.stats["expected_win_pct"]) for p in with_exp) / len(with_exp)
    dd_pct = round(-dd / capital * 100, 1) if capital else None
    share = round(charges / gross * 100, 1) if gross > 0 else None
    g = GOLIVE
    items = [
        {"key": "trades", "label": "Enough trades", "value": len(ps), "need": f"≥ {g['trades']}", "ok": len(ps) >= g["trades"]},
        {"key": "days", "label": "Enough trading days", "value": days, "need": f"≥ {g['days']}", "ok": days >= g["days"]},
        {"key": "net", "label": "Profit after all costs", "value": round(net, 2), "need": "> ₹0", "ok": net > 0},
        {"key": "pf", "label": "Profit factor (₹ won ÷ ₹ lost)", "value": pf, "need": f"≥ {g['pf']}",
         "ok": (pf is None and net > 0) or (pf is not None and pf >= g["pf"])},
        {"key": "dd", "label": "Worst drawdown", "value": dd_pct, "need": f"≤ {g['dd_pct']:g}% of capital",
         "ok": dd_pct is not None and dd_pct <= g["dd_pct"]},
        {"key": "drift", "label": "Real win rate vs AI test", "value": None if gap is None else round(gap, 1),
         "need": f"no worse than {g['drift']:g} pts", "ok": None if gap is None else gap >= g["drift"]},
        {"key": "costs", "label": "Charges as share of gross profit", "value": share, "need": f"≤ {g['cost_share']:g}%",
         "ok": None if share is None else share <= g["cost_share"]},
    ]
    must = [i for i in items if i["ok"] is not None]
    ready = bool(ps) and all(i["ok"] for i in must) and len(must) >= 6
    left = [i["label"] for i in items if i["ok"] is False]
    return {"items": items, "ready": ready, "trades": len(ps), "days": days,
            "verdict": ("✅ Ready to consider LIVE — start with 1 lot" if ready else
                        "Not ready yet: " + ", ".join(left[:4]) if left else "Not enough data yet")}


TUNE_KS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)


def tuning_suggestions(closed: list, quick_now: float | None = None, min_trades: int = 30) -> dict:
    """What your own trades say about the stop, target and quick target (from how far each trade went for and against
    you while it was open). Suggestions only — nothing is changed by itself."""
    ps = [p for p in closed if (p.stats or {}).get("risk_pts") and (p.stats or {}).get("mfe_pts") is not None and p.entry_price]
    out = {"trades": len(ps), "need": min_trades, "ready": len(ps) >= min_trades, "items": []}
    if not ps:
        return out
    side = lambda p: p.spec.side  # noqa: E731
    act_r = [((p.exit_price or p.entry_price) - p.entry_price) * side(p) / p.stats["risk_pts"] for p in ps]
    mfe_r = [p.stats["mfe_pts"] / p.stats["risk_pts"] for p in ps]
    mae_r = [(p.stats.get("mae_pts") or 0) / p.stats["risk_pts"] for p in ps]
    with_t = [p for p in ps if p.stats.get("target_pts")]
    reach = round(sum(1 for p in with_t if p.stats["mfe_pts"] >= p.stats["target_pts"] - 0.05) / len(with_t) * 100, 1) if with_t else None
    out["target_reach_pct"] = reach
    out["avg_mfe_pts"] = round(float(np.mean([p.stats["mfe_pts"] for p in ps])), 2)
    out["avg_mae_pts"] = round(float(np.mean([p.stats.get("mae_pts") or 0 for p in ps])), 2)
    if not out["ready"]:
        return out
    now_r = float(np.mean(act_r))
    capped = [p.exit_reason in ("target", "quick_target") for p in ps]
    best_k, best_r = None, now_r
    for k in TUNE_KS:
        r = float(np.mean([k if (m >= k and not (c and k > m + 1e-9)) else a for m, a, c in zip(mfe_r, act_r, capped)]))
        if r > best_r + 0.05:
            best_k, best_r = k, r
    cur_t = [p.stats["target_pts"] / p.stats["risk_pts"] for p in with_t]
    out["items"].append({
        "key": "target", "title": "Target",
        "now": f"{np.median(cur_t):.2f}R (full target reached {reach}% of the time)" if cur_t else "no fixed target",
        "suggested": f"{best_k:g}R" if best_k else "keep",
        "why": (f"With a {best_k:g}R target your trades would have averaged {best_r:+.2f}R instead of {now_r:+.2f}R"
                if best_k else f"No other target size beats your current average of {now_r:+.2f}R per trade")})
    wins = [m for m, a in zip(mae_r, act_r) if a > 0]
    if len(wins) >= 10:
        q = float(np.percentile(wins, 10))  # 90% of winners never dipped below this
        out["items"].append({
            "key": "stop", "title": "Stop-loss",
            "now": "1R", "suggested": f"{min(1.0, abs(q) + 0.1):.2f}R" if abs(q) < 0.7 else "keep",
            "why": (f"90% of your winning trades never went more than {abs(q):.2f}R against you — a tighter stop would "
                    f"cut losers sooner (and allow more lots for the same ₹ risk)" if abs(q) < 0.7 else
                    f"Winners often dip {abs(q):.2f}R before working — the stop needs that room")})
    losers = [(m, a) for m, a in zip(mfe_r, act_r) if a <= 0]
    if len(losers) >= 10:
        gave = sum(1 for m, _ in losers if m >= 0.5) / len(losers) * 100
        out["items"].append({
            "key": "breakeven", "title": "Break-even stop",
            "now": "—", "suggested": "move stop to entry at +0.5R" if gave >= 30 else "keep",
            "why": f"{gave:.0f}% of losing trades were first +0.5R in profit" + (" — protecting them would turn many into scratches"
                                                                                   if gave >= 30 else "")})
    mfe_pts = [p.stats["mfe_pts"] for p in ps]
    qt = round(float(np.percentile(mfe_pts, 40)), 1)
    out["items"].append({
        "key": "quick", "title": "Quick target",
        "now": f"+{quick_now:g} pts" if quick_now else "auto", "suggested": f"+{qt:g} pts",
        "why": f"60% of your trades went at least +{qt:g} pts in your favour at some point"})
    return out


class OptionsManager:
    def __init__(self, kite_service, store, hub):
        self.ks, self.store, self.hub = kite_service, store, hub
        self.autos: dict[str, AutoTrader] = {}
        self.swings: dict[str, SwingTrader] = {}
        self.positions: dict[str, ManagedPosition] = {}
        self.lock = threading.RLock()
        self.deps = Deps(
            instruments=lambda: kite_service.instruments(),
            quote=lambda keys: kite_service.kite.quote(keys),
            history=lambda tok, iv, a, b: kite_service.historical(tok, iv, a, b),
            kite_getter=lambda: kite_service.kite,
            margin_available=lambda: float(kite_service.kite.margins("equity")["net"]),
            order_margin=lambda o: float(sum(x.get("total", 0) for x in kite_service.kite.order_margins([o]))),
            data_dir=settings.data_dir,
        )
        self.load()

    # ------------------------------------------------------------ persistence / recovery
    def _save_pos(self, p: ManagedPosition):
        self.store.set(f"optpos:{p.id}", p.snapshot())
        self.hub_refresh()

    def _save_auto(self, a: AutoTrader):
        self.store.set(f"optauto:{a.id}", a.to_store())

    def _save_swing(self, w: SwingTrader):
        self.store.set(f"optswing:{w.id}", w.to_store())

    def load(self):
        """Restart recovery: restore positions; LIVE ones are reconciled against Kite."""
        for k, snap in self.store.items("optpos:"):
            p = ManagedPosition.restore(snap, self.deps.kite_getter, None)
            p.on_change = self._save_pos
            self.positions[p.id] = p
        for k, d in self.store.items("optauto:"):
            try:
                known = {f.name for f in fields(AutoConfig)}
                cfgd = {k: v for k, v in d["config"].items() if k in known}  # settings renamed in updates are dropped
                if cfgd.get("entry_tolerance_pts") == 0.5:  # old default -> new default ₹1 (requested change)
                    cfgd["entry_tolerance_pts"] = 1.0
                a = AutoTrader(AutoConfig(**cfgd), self.deps, self._register, self._save_auto, aid=d["id"])
            except Exception:
                continue
            a.paused = d.get("paused", False)
            a.enabled = False  # entries never restart instantly; PAPER may resume via the clock (see _resume_after_restart)
            was_on = d.get("enabled") or d.get("resume_pending")
            a.resume_pending = bool(was_on and a.cfg.mode == "paper" and a.cfg.auto_resume_paper)
            a.state, a.message = "IDLE", ("Restored after restart — will switch back on by itself (paper)" if a.resume_pending
                                          else "Restored after restart — re-enable to trade")
            a.day = date.fromisoformat(d["day"]) if d.get("day") else None
            cf = d.get("counts_for") or d.get("day")
            a.counts_for = date.fromisoformat(cf) if cf else None  # today's trade count / P&L survive edits + restarts
            a.trades_today, a.day_pnl = d.get("trades_today", 0), d.get("day_pnl", 0.0)
            a.day_blocked, a.processed = d.get("day_blocked"), d.get("processed", [])
            a.signals, a.timeline, a.levels = d.get("signals", []), d.get("timeline", []), d.get("levels", {})
            a.pick = d.get("ai_pick")
            a.pnl_days, a.loss_streak = d.get("pnl_days", {}), d.get("loss_streak", 0)
            a.skips = d.get("skips", {})
            a.versions = d.get("versions") or a.versions
            a.iv_hist = d.get("iv_hist") or {}
            if a.cfg.autopilot and a.cfg.auto_money and a.cfg.lots == 10:
                a.cfg.lots = 1  # older versions stored the automatic lot cap in your settings
            a.day_build = d.get("day_build")
            a.pos_strategy = tuple(d["pos_strategy"]) if d.get("pos_strategy") else None
            if d.get("position_id") in self.positions:
                a.position = self.positions[d["position_id"]]
            a.others = [self.positions[i] for i in d.get("other_ids", []) if i in self.positions and self.positions[i].is_active]
            today = datetime.now().date()
            mine = [p for p in self.positions.values() if p.spec.source == f"auto:{a.id}" and (p.entry_time or p.created or "")[:10] == today.isoformat()
                    and p.state not in ("ORDER_REJECTED",)]
            if mine and a.counts_for != today:  # older saves lost today's count after a settings edit: rebuild it
                a.counts_for = today
                a.trades_today = len(mine)
                a.day_pnl = round(sum(float(p.net) for p in mine if p.state == "POSITION_CLOSED"), 2)
                a.processed = list(dict.fromkeys([*a.processed, *[p.id for p in mine if p.state == "POSITION_CLOSED"]]))
            if not a.pnl_days:  # older saves: rebuild from this strategy's closed positions
                for p in self.positions.values():
                    if p.spec.source == f"auto:{a.id}" and p.state == "POSITION_CLOSED" and p.exit_time:
                        a.pnl_days[p.exit_time[:10]] = round(a.pnl_days.get(p.exit_time[:10], 0.0) + float(p.net), 2)
            self.autos[a.id] = a
        self._load_swings()
        try:
            self._migrate_default_capital()
        except Exception:
            pass
        try:
            self._managed_upkeep()
        except Exception:
            pass

    def _load_swings(self):
        known = {f.name for f in fields(SwingConfig)}
        for k, d in self.store.items("optswing:"):
            try:
                w = SwingTrader(SwingConfig(**{x: v for x, v in d["config"].items() if x in known}), self.deps, self._register,
                                self._save_swing, sid=d["id"])
            except Exception:
                continue
            # a swing strategy holds positions for weeks: it keeps running after a restart (PAPER and LIVE alike —
            # it only acts once a day, and a live position keeps its exchange stop anyway)
            w.enabled = bool(d.get("enabled"))
            w.last_check, w.signal, w.timeline, w.created = d.get("last_check"), d.get("signal"), d.get("timeline", []), d.get("created", w.created)
            w.realized, w.counted = float(d.get("realized", 0.0)), d.get("counted", [])
            w.wait_cross = bool(d.get("wait_cross", False))
            if d.get("position_id") in self.positions:
                w.position = self.positions[d["position_id"]]
            w.closing = [self.positions[i] for i in d.get("closing_ids", []) if i in self.positions]
            self.swings[w.id] = w

    def reconcile_all(self) -> dict:
        out = {}
        for p in list(self.positions.values()):
            if p.mode == "live" and p.state in ACTIVE_STATES:
                if p.state != "RECONCILIATION_REQUIRED":
                    p.set_state("RECONCILIATION_REQUIRED", "Checking Kite after restart")
                out[p.id] = p.reconcile()
        return out

    def _register(self, p: ManagedPosition):
        prev = p.on_change
        def changed(x, prev=prev):
            if prev:
                prev(x)
            self._save_pos(x)
        p.on_change = changed
        self.positions[p.id] = p
        self._save_pos(p)

    # ------------------------------------------------------------ ticker routing
    def wanted_tokens(self) -> list[int]:
        toks = set()
        for a in self.autos.values():
            toks.update(a.wanted_tokens())
        for p in self.positions.values():
            if p.state in ACTIVE_STATES:
                toks.add(int(p.spec.instrument_token))
        return sorted(toks)

    def hub_refresh(self, force: bool = False):
        """(Re)subscribe market data only when the needed instrument set changes."""
        toks = tuple(self.wanted_tokens())
        if not force and toks == getattr(self, "_last_tokens", None):
            return
        try:
            self.hub.refresh_listener(self)
            self._last_tokens = toks
        except Exception:
            pass

    def on_raw_tick(self, tk: dict):
        tok = tk.get("instrument_token")
        price = tk.get("last_price")
        if price is None:
            return
        ts = tk.get("exchange_timestamp") or tk.get("last_trade_time") or datetime.now()
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.replace(tzinfo=None)
        for a in list(self.autos.values()):
            if a.spot and a.spot["instrument_token"] == tok:
                a.on_index_tick(ts, float(price))
        for p in list(self.positions.values()):
            if p.state in ACTIVE_STATES and int(p.spec.instrument_token) == tok:
                p._tick_at = time.monotonic()
                p.on_tick(float(price))

    def on_order_update(self, data: dict):
        for p in list(self.positions.values()):
            if p.state in ACTIVE_STATES:
                p.on_order_update(data)

    POLL_AFTER_S = 8  # no tick for this long -> fetch the premium directly so target/stop still work

    def on_clock(self, now: datetime):
        for a in list(self.autos.values()):
            try:
                a.on_clock(now)
            except Exception as e:
                a.log(f"Clock error: {e}", "alert")
        for w in list(self.swings.values()):
            try:
                w.on_clock(now)
            except Exception as e:
                w.log(f"Clock error: {e}"[:200], "alert")
        # subscribe prices for anything new (Auto Trade entries, a newly picked index); no-op when unchanged
        self.hub_refresh()
        self._poll_stale_positions()
        try:
            self._scheduled_checks(now)
        except Exception:
            pass

    # ------------------------------------------------------------ morning readiness + feed watchdog
    READY_FROM, READY_UNTIL = "09:00", "09:15"
    LOGIN_REMIND = "08:45"
    FEED_STALE_S = 120

    def readiness(self, now: datetime | None = None) -> dict:
        """Pre-market checklist for Auto Trade: trading day, Kite login, contracts, margin, live prices."""
        now = now or datetime.now()
        checks: list[dict] = []

        def add(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})
        tday = is_trading_day(now.date(), self.deps.data_dir)
        add("Market open today", tday, "Trading day" if tday else "Weekend / exchange holiday — no trading")
        try:
            prof = self.ks.kite.profile()
            add("Kite login", True, f"Logged in as {prof.get('user_name') or prof.get('user_id')}")
            logged = True
        except Exception as e:
            add("Kite login", False, f"Not logged in ({str(e)[:80]}) — open the app → Settings → Login with Kite")
            logged = False
        on = [a for a in self.autos.values() if a.enabled]
        pend = [a for a in self.autos.values() if getattr(a, "resume_pending", False) and not a.enabled]
        add("Auto Trade enabled", bool(on or pend), f"{len(on)} strategy(ies) enabled" if on
            else f"{len(pend)} PAPER strategy(ies) will switch back on by itself once Kite is logged in" if pend
            else "No Auto Trade strategy is enabled — open it and tap ▶ (LIVE always needs you after a restart)")
        if logged:
            try:
                inst = self.deps.instruments()
                missing = [a.cfg.index for a in on if not expiries(inst, a.cfg.index, now.date())]
                add("Option contracts loaded", not missing, "Contracts and expiries found" if not missing
                    else f"No option expiries listed for {', '.join(sorted(set(missing)))}")
            except Exception as e:
                add("Option contracts loaded", False, f"Could not load instruments ({str(e)[:80]})")
            live = [a for a in on if a.cfg.mode == "live"]
            if live:
                try:
                    have = self.deps.margin_available()
                    need = max(a.cfg.max_capital for a in live)
                    add("Margin for LIVE", have >= need, f"Available ₹{have:,.0f}, max per trade ₹{need:,.0f}"
                        + ("" if have >= need else " — add funds or lower max capital"))
                except Exception as e:
                    add("Margin for LIVE", False, f"Could not read margin ({str(e)[:80]})")
        cautions = sorted({c for a in on for c in a.day_caution(now)})
        if cautions:
            add("Special day", True, f"{', '.join(cautions)} — your expiry/event-day rules apply")
        if tday and now.time() >= datetime.strptime("09:16", "%H:%M").time() and on:
            stale = [a for a in on if self._feed_stale(a, now)]
            add("Live prices arriving", not stale, "Prices are updating" if not stale
                else f"No price for {self.FEED_STALE_S // 60}+ min on {', '.join(a.cfg.index for a in stale)} — check internet / Kite login")
        res = {"at": now.isoformat(timespec="seconds"), "ok": all(c["ok"] for c in checks if c["name"] != "Auto Trade enabled" or tday),
               "checks": checks}
        self.health = res
        return res

    # ------------------------------------------------------------ summaries + backups
    def _skips_for(self, source: str | None) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.autos.values():
            if source is None or f"auto:{a.id}".startswith(source):
                for cats in a.skips.values():
                    for c, n in cats.items():
                        out[c] = out.get(c, 0) + n
        return dict(sorted(out.items(), key=lambda x: -x[1]))

    def _closed_between(self, d0: date, d1: date) -> list[ManagedPosition]:
        return [p for p in self.positions.values() if p.spec.source.startswith("auto:") and p.state == "POSITION_CLOSED"
                and p.exit_time and d0.isoformat() <= p.exit_time[:10] <= d1.isoformat()]

    def _skips_between(self, d0: date, d1: date) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.autos.values():
            for d, cats in a.skips.items():
                if d0.isoformat() <= d <= d1.isoformat():
                    for c, n in cats.items():
                        out[c] = out.get(c, 0) + n
        return dict(sorted(out.items(), key=lambda x: -x[1]))

    def _summary_text(self, ps: list, skips: dict, label: str) -> str:
        nets = [float(p.net) for p in ps]
        lines = [f"{label}: {len(nets)} trade(s), net ₹{sum(nets):,.0f}"
                 + (f", win {sum(1 for x in nets if x > 0) / len(nets) * 100:.0f}%" if nets else "")]
        if nets:
            lines.append(f"Best ₹{max(nets):,.0f} · worst ₹{min(nets):,.0f}")
        if skips:
            lines.append("Skipped signals: " + ", ".join(f"{c} {n}" for c, n in list(skips.items())[:5]))
        return "\n".join(lines)

    def day_summary_text(self, d: date) -> str:
        return self._summary_text(self._closed_between(d, d), self._skips_between(d, d), "Today")

    def week_summary_text(self, d: date) -> str:
        d0 = d - timedelta(days=d.weekday())
        txt = self._summary_text(self._closed_between(d0, d), self._skips_between(d0, d), "This week")
        dr = performance_breakdown(self._closed_between(date(2000, 1, 1), d)).get("drift")
        if dr:
            txt += f"\nAI test win {dr['expected_win_pct']}% vs real {dr['actual_win_pct']}% — {dr['verdict']}"
        return txt

    def backup(self, now: datetime | None = None, keep: int = 14):
        from pathlib import Path
        now = now or datetime.now()
        d = Path(self.deps.data_dir) / "backups"
        try:
            d.mkdir(parents=True, exist_ok=True)
            self.store.backup(d / f"kitealgo-{now:%Y-%m-%d}.db")
            for old in sorted(d.glob("kitealgo-*.db"))[:-keep]:
                old.unlink()
        except Exception:
            pass

    def _feed_stale(self, a: AutoTrader, now: datetime) -> bool:
        try:
            lt = a.last_tick.replace(tzinfo=None) if a.last_tick else None
            return lt is None or (now - lt).total_seconds() > self.FEED_STALE_S
        except Exception:
            return True

    def _resume_after_restart(self, now: datetime):
        """PAPER strategies that were ON before a restart switch back on once Kite works (trading days, before exit)."""
        pend = [a for a in self.autos.values() if getattr(a, "resume_pending", False) and not a.enabled]
        if not pend or not is_trading_day(now.date(), self.deps.data_dir):
            return
        last = getattr(self, "_resume_try", None)
        if last and (now - last).total_seconds() < 60:
            return
        self._resume_try = now
        try:
            self.ks.kite.profile()  # needs a valid Kite login (it expires every morning)
        except Exception:
            return
        for a in pend:
            if now.strftime("%H:%M") >= a.cfg.exit_time or a.cfg.mode != "paper":
                continue
            try:
                a.enable(now)
                a.resume_pending = False
                a.log("Switched back on automatically after a server restart (PAPER)", "alert")  # also a phone alert
            except Exception as e:
                a.log(f"Auto-resume failed: {e}"[:200], "alert")

    def _scheduled_checks(self, now: datetime):
        from . import notify
        self._resume_after_restart(now)
        t = now.strftime("%H:%M")
        if self.LOGIN_REMIND <= t < self.READY_FROM and getattr(self, "_login_day", None) != now.date():
            self._login_day = now.date()
            if is_trading_day(now.date(), self.deps.data_dir) and any(a.enabled or a.cfg.auto_resume_paper for a in self.autos.values()):
                try:
                    self.ks.kite.profile()
                except Exception:
                    notify.send("Kite Algo: please log in to Zerodha",
                                "Not logged in yet for today — open Kite Algo → Settings → Login with Kite before 09:15, "
                                "or Auto Trade can't see the market.", "high", "key")
        if self.READY_FROM <= t < self.READY_UNTIL and getattr(self, "_ready_day", None) != now.date():
            self._ready_day = now.date()
            if not is_trading_day(now.date(), self.deps.data_dir):
                return
            r = self.readiness(now)
            bad = [c for c in r["checks"] if not c["ok"]]
            for a in self.autos.values():
                for c in bad:
                    a.log(f"Morning check: {c['name']} — {c['detail']}", "risk")
            if bad:
                notify.send("Kite Algo: morning check FAILED", "\n".join(f"✗ {c['name']}: {c['detail']}" for c in bad), "high", "warning")
            else:
                notify.send("Kite Algo: ready", "All morning checks passed — Auto Trade is ready for 09:15.", "low", "white_check_mark")
        if t >= "15:35" and getattr(self, "_summary_day", None) != now.date() and is_trading_day(now.date(), self.deps.data_dir):
            self._summary_day = now.date()
            if any(a.enabled or a.trades_today or a.skips.get(now.date().isoformat()) for a in self.autos.values()):
                notify.send("Kite Algo: today's summary", self.day_summary_text(now.date()), "default", "bar_chart")
                if now.weekday() == 4:  # Friday: the week
                    notify.send("Kite Algo: this week", self.week_summary_text(now.date()), "default", "calendar")
        if t >= "15:40" and getattr(self, "_acct_day", None) != now.date() and is_trading_day(now.date(), self.deps.data_dir):
            self._acct_day = now.date()
            try:  # keep a copy of the Zerodha day (the API only ever gives today)
                from . import account
                from .config import settings as _st
                account.save_day(self.deps.data_dir, account.day_summary(self.ks.kite, _st.order_tag, now))
            except Exception:
                pass
        if t >= "15:45" and getattr(self, "_backup_day", None) != now.date():
            self._backup_day = now.date()
            self.backup(now)
        # feed watchdog during market hours
        if "09:17" <= t <= "15:25" and is_trading_day(now.date(), self.deps.data_dir):
            for a in self.autos.values():
                if not a.enabled:
                    continue
                stale = self._feed_stale(a, now)
                if stale and not getattr(a, "_stall_alerted", False):
                    a._stall_alerted = True
                    msg = f"No live price for {a.cfg.index} for over {self.FEED_STALE_S // 60} min — Auto Trade can't see the market"
                    a.log(msg, "alert")  # the log also sends a phone alert
                elif not stale and getattr(a, "_stall_alerted", False):
                    a._stall_alerted = False
                    a.log(f"Live prices for {a.cfg.index} are back", "risk")

    def _poll_stale_positions(self):
        """Fallback when the live feed is silent: quote open positions so stop-loss/target exits still fire."""
        stale = [p for p in self.positions.values()
                 if p.state in ACTIVE_STATES and time.monotonic() - getattr(p, "_tick_at", 0) > self.POLL_AFTER_S]
        if not stale:
            return
        keys = {f"{p.spec.exchange}:{p.spec.tradingsymbol}": p for p in stale}
        try:
            qs = self.deps.quote(list(keys))
        except Exception:
            return
        for k, p in keys.items():
            ltp = (qs.get(k) or {}).get("last_price")
            if ltp:
                p._tick_at = time.monotonic()
                try:
                    p.on_tick(float(ltp))
                except Exception:
                    pass

    # ------------------------------------------------------------ option chain helpers
    def chain_info(self, index: str) -> dict:
        inst = self.ks.instruments()
        today = datetime.now().date()
        spot = spot_instrument(inst, index)
        exps = expiries(inst, index, today)
        fut = nearest_future(inst, index, today)
        return {"index": index.upper(), "spot": spot, "expiries": exps, "future": fut,
                "step": index_cfg(index)["step"], "exchange": index_cfg(index)["opt_exchange"]}

    def spot_price(self, index: str) -> float:
        inst = self.ks.instruments()
        s = spot_instrument(inst, index)
        key = f"{s['exchange']}:{s['tradingsymbol']}"
        return float(self.ks.kite.ltp([key])[key]["last_price"])

    def contract_quote(self, index: str, expiry: str, option_type: str, method: str, offset: int, lots: int,
                       side: str = "BUY", product: str = "MIS", risk: dict | None = None) -> dict:
        """Everything the UI shows when strike/expiry/type/lots change."""
        inst = self.ks.instruments()
        spot = self.spot_price(index)
        c = select_contract(inst, index, expiry, option_type, spot, method, offset)
        qty = quantity(lots, c.lot_size)
        key = f"{c.exchange}:{c.tradingsymbol}"
        q = self.ks.kite.quote([key]).get(key, {})
        ltp, bid, ask = _best_prices(q)
        pside = 1 if side == "BUY" else -1
        ref = (ask if pside > 0 else bid) or ltp
        out = {"contract": c.to_dict(), "spot": spot, "lots": lots, "quantity": qty, "ltp": ltp, "bid": bid, "ask": ask,
               "prev_close": (q.get("ohlc") or {}).get("close"), "oi": q.get("oi"), "volume": q.get("volume")}
        if bid and ask:
            out["spread_pct"] = round((ask - bid) / ((ask + bid) / 2) * 100, 2)
        if ref:
            spec = PositionSpec(exchange=c.exchange, tradingsymbol=c.tradingsymbol, instrument_token=c.instrument_token,
                                lots=lots, lot_size=c.lot_size, side=pside, product=product, tick_size=c.tick_size,
                                instrument_type=option_type, strike=c.strike, expiry=c.expiry)
            mp = ManagedPosition(spec, RiskConfig.from_dict(risk), "paper")
            try:
                out["preview"] = mp.preview(ref)
            except RiskError as e:
                out["risk_error"] = str(e)
        try:
            out["margin_required"] = round(self.deps.order_margin({"exchange": c.exchange, "tradingsymbol": c.tradingsymbol,
                "transaction_type": side, "variety": "regular", "product": product, "order_type": "MARKET", "quantity": qty}), 2)
            out["margin_available"] = round(self.deps.margin_available(), 2)
        except Exception as e:
            out["margin_error"] = f"Margin check unavailable: {e}"[:160]
        return to_python(out)

    # ------------------------------------------------------------ manual trades
    def manual_open(self, mode: str, index: str, expiry: str, option_type: str, method: str, offset: int, lots: int,
                    side: str, product: str, risk: dict, confirm_live: str = "", allow_option_selling: bool = False) -> ManagedPosition:
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY (buy to open) or SELL (sell to open)")
        if side == "SELL" and not allow_option_selling:
            raise PermissionError("Selling options to open is disabled. Turn on 'allow option selling' first.")
        self._live_gate(mode, lots, confirm_live)
        inst = self.ks.instruments()
        spot = self.spot_price(index)
        c = select_contract(inst, index, expiry, option_type, spot, method, offset)
        for p in self.positions.values():
            if p.state in ACTIVE_STATES and p.spec.tradingsymbol == c.tradingsymbol:
                raise PermissionError(f"You already have an active position in {c.tradingsymbol}. "
                                      "Close it first (prevents accidental reversal/duplication).")
        key = f"{c.exchange}:{c.tradingsymbol}"
        ltp, bid, ask = _best_prices(self.ks.kite.quote([key]).get(key, {}))
        pside = 1 if side == "BUY" else -1
        ref = (ask if pside > 0 else bid) or ltp
        if not ref:
            raise ValueError("No live price for this contract")
        spec = PositionSpec(exchange=c.exchange, tradingsymbol=c.tradingsymbol, instrument_token=c.instrument_token,
                            lots=lots, lot_size=c.lot_size, side=pside, product=product, tick_size=c.tick_size,
                            instrument_type=option_type, strike=c.strike, expiry=c.expiry, source="manual", tag="kamanual")
        p = ManagedPosition(spec, RiskConfig.from_dict(risk), mode, self.deps.kite_getter)
        p.preview(ref)  # mandatory SL / risk checks before registering
        self._register(p)
        p.open(ref)
        self.hub_refresh()
        return p

    def _live_gate(self, mode: str, lots: int, confirm: str):
        if mode == "live":
            if not settings.live_trading_enabled:
                raise PermissionError("Live trading is disabled on the server (LIVE_TRADING_ENABLED=false)")
            if confirm != LIVE_CONFIRM:
                raise PermissionError(f"Live orders need confirm_live = '{LIVE_CONFIRM}'")
            if lots > settings.max_live_lots:
                raise PermissionError(f"Lots above server limit MAX_LIVE_LOTS={settings.max_live_lots}")
        elif mode != "paper":
            raise ValueError("mode must be paper or live")

    def close_position(self, pid: str) -> ManagedPosition:
        p = self.get_position(pid)
        if not p.is_open and p.state != "EXIT_FAILED":
            raise ValueError(f"Position is {p.state}, nothing to close")
        p.request_exit("manual_close")
        return p

    def get_position(self, pid: str) -> ManagedPosition:
        if pid not in self.positions:
            raise KeyError("Position not found")
        return self.positions[pid]

    # ------------------------------------------------------------ auto traders
    def create_auto(self, cfg: AutoConfig) -> AutoTrader:
        cfg.validate()
        if cfg.mode == "live":
            self._live_gate("live", cfg.lots, LIVE_CONFIRM)
            cfg.max_daily_loss = min(cfg.max_daily_loss, settings.max_live_daily_loss)
        a = AutoTrader(cfg, self.deps, self._register, self._save_auto)
        self.autos[a.id] = a
        self._save_auto(a)
        return a

    def update_auto(self, aid: str, cfg: AutoConfig) -> AutoTrader:
        a = self.get_auto(aid)
        if a.active_positions():
            raise PermissionError("Close the open trade (or wait for it to exit) before changing settings")
        cfg.validate()
        restart = a.enabled and a.cfg.mode == "paper" and cfg.mode == "paper"
        if a.enabled and not restart:
            raise PermissionError("Stop Auto Trade before changing LIVE settings")
        if restart:
            a.disable()  # paper: stop → apply → start again automatically
        old, a.cfg = a.cfg, cfg
        a.reset_for_new_settings(old)  # today's trade count and loss are kept (daily limits still hold)
        if a.day_blocked and a.day_blocked.startswith("AI found"):
            a.day_blocked = None
        if restart:
            a.enable(datetime.now())
            a.log("Settings saved and Auto Trade restarted with them", "control")
        self._save_auto(a)
        return a

    def get_auto(self, aid: str) -> AutoTrader:
        if aid not in self.autos:
            raise KeyError("Auto strategy not found")
        return self.autos[aid]

    def control_auto(self, aid: str, action: str, confirm_live: str = "") -> AutoTrader:
        a = self.get_auto(aid)
        if action == "enable":
            if a.cfg.mode == "live":
                self._live_gate("live", a.cfg.lots, confirm_live)
            a.enable(datetime.now())
        elif action == "pause":
            a.pause()
        elif action == "resume":
            a.resume()
        elif action == "disable":
            a.disable()
        elif action == "close_position":
            a.close_position()
        elif action == "repick":
            a.repick(datetime.now())
        elif action == "use_best_unproven":
            a.use_best_unproven(datetime.now())
        elif action == "clear_signals":
            a.clear_signals()
        elif action == "clear_log":
            a.clear_log()
        elif action == "day_build":
            a.build_now(datetime.now())
        else:
            raise ValueError("action must be enable, pause, resume, disable, close_position, repick, use_best_unproven, "
                             "clear_signals, clear_log or day_build")
        self._save_auto(a)
        self.hub_refresh()
        return a

    def clear_history(self, mode: str | None = None, source: str | None = None) -> int:
        """Delete finished positions (closed or rejected) from the app's records. Open ones are never touched.
        source: 'manual', 'auto' (all Auto Trade) or 'auto:<id>'."""
        removed = 0
        for pid, p in list(self.positions.items()):
            if p.state in ACTIVE_STATES:
                continue
            if mode and p.mode != mode:
                continue
            if source and not p.spec.source.startswith(source):
                continue
            del self.positions[pid]
            self.store.delete(f"optpos:{pid}")
            removed += 1
        for a in self.autos.values():
            if a.position is not None and a.position.id not in self.positions:
                a.position = None
                self._save_auto(a)
        for w in self.swings.values():
            if w.position is not None and w.position.id not in self.positions:
                w.position = None
                self._save_swing(w)
        return removed

    def delete_auto(self, aid: str):
        a = self.get_auto(aid)
        if a.enabled or (a.position and a.position.is_active):
            raise PermissionError("Disable and close the position first")
        self.autos.pop(aid)
        self.store.delete(f"optauto:{aid}")

    def kill(self) -> dict:
        """Emergency: disable every auto strategy and close every managed position."""
        out = {"disabled": [], "closing": []}
        for a in self.autos.values():
            if a.enabled:
                a.disable()
                out["disabled"].append(a.id)
        for w in self.swings.values():
            if w.enabled:
                w.enabled = False
                w.log("Stopped by the emergency stop", "control")
                out["disabled"].append(w.id)
        st = self._app_raw()
        if st.get("auto"):
            st["auto"] = False  # the emergency stop also switches the app to Manual
            self.store.set("app_settings", st)
        out["cancelled"] = []
        for p in self.positions.values():
            if p.is_open or p.state == "EXIT_FAILED":
                p.request_exit("kill_switch")
                out["closing"].append(p.id)
            elif p.state == "ENTRY_PENDING":
                try:
                    if p.mode == "live" and p.entry_order_id:
                        p.kite.cancel_order(variety="regular", order_id=p.entry_order_id)
                    p.event("order", "Entry cancelled by kill switch")
                    out["cancelled"].append(p.id)
                except Exception as e:
                    p.event("alert", f"Kill switch could not cancel the entry ({e}); reconciling")
                    try:
                        p.reconcile()
                    except Exception:
                        pass
        return out

    # ------------------------------------------------------------ app settings: Paper/Live + Auto/Manual
    APP_DEFAULTS = {"money": "paper", "auto": False, "intraday": True, "swing": True, "capital": 1_000_000.0,
                    "swing_index": "BANKNIFTY"}

    OLD_DEFAULT_CAPITAL = 300_000.0

    def _migrate_default_capital(self):
        """Default capital is now ₹10,00,000. A setup still on the old ₹3,00,000 default (never changed) moves up
        once, and the app's own Autopilot / Swing follow it. A capital you typed yourself is left alone."""
        st = self.store.get("app_settings") or {}
        if st.get("capital_default_v2"):
            return
        if float(st.get("capital", self.OLD_DEFAULT_CAPITAL)) == self.OLD_DEFAULT_CAPITAL:
            st["capital"] = self.APP_DEFAULTS["capital"]
            for ids in (st.get("managed") or {}).values():
                a = self.autos.get(ids.get("auto", ""))
                if a is not None and a.cfg.paper_capital == self.OLD_DEFAULT_CAPITAL:
                    a.cfg.paper_capital = st["capital"]
                    self._save_auto(a)
                w = self.swings.get(ids.get("swing", ""))
                if w is not None and float(w.cfg.capital) in (self.OLD_DEFAULT_CAPITAL, 200_000.0):
                    w.cfg.capital = st["capital"]
                    self._save_swing(w)
        st["capital_default_v2"] = True
        self.store.set("app_settings", st)

    def _app_raw(self) -> dict:
        d = dict(self.APP_DEFAULTS)
        d.update(self.store.get("app_settings") or {})
        d.setdefault("managed", {})
        return d

    MANAGED_TREND_OVERRIDE = 5  # Auto mode: on a clear trend day (5 of 6 indicators agree, none against) join it even
    # when the day's picked strategy is silent — one-signal-a-day setups (CPR, ORB) otherwise sit out the whole trend

    def _managed_upkeep(self):
        """Auto mode runs ONE Autopilot per money mode: stop any other Autopilot of that mode (duplicates or
        older ones) so the same trade isn't taken 2-3 times, and keep the managed one's trend-day entries on."""
        s = self._app_raw()
        if not s.get("auto"):
            return
        mid = s["managed"].get(s["money"], {}).get("auto")
        a = self.autos.get(mid or "")
        if a is None:
            return
        if a.cfg.trend_override_min == 0:
            a.cfg.trend_override_min = self.MANAGED_TREND_OVERRIDE
            self._save_auto(a)
        for o in self.autos.values():
            if o.id != mid and (o.enabled or getattr(o, "resume_pending", False)) and o.cfg.mode == s["money"] and o.cfg.autopilot:
                o.disable()  # also clears "switch back on after restart"
                o.log("Stopped: Auto mode runs the app's own Autopilot — a second one would take the same trades again", "control")
                self._save_auto(o)

    def _managed_auto_cfg(self, money: str, s: dict) -> AutoConfig:
        """The app's own intraday Autopilot: AI picks index, strategy, CE/PE, strike, stop/target and lots."""
        return AutoConfig(mode=money, autopilot=True, autopilot_index="ANY", strategy_source="ai", entry_source="strategy",
                          index="NIFTY", strategy="morning_momentum", interval="5minute", side="BUY", option_rule="AUTO",
                          expiry="NEAREST", strike_method="ATM", lots=1, product="MIS", entry_start="09:30", entry_end="14:45",
                          exit_time="15:10", auto_money=True, paper_capital=float(s["capital"]),
                          trend_override_min=self.MANAGED_TREND_OVERRIDE)

    def _managed_swing_cfg(self, money: str, s: dict) -> SwingConfig:
        return SwingConfig(index=s["swing_index"], mode=money, capital=float(s["capital"]))

    def app_settings(self) -> dict:
        s = self._app_raw()
        ids = s["managed"].get(s["money"], {})
        a, w = self.autos.get(ids.get("auto", "")), self.swings.get(ids.get("swing", ""))
        # LIVE Autopilot never switches itself back on after a server restart: the app asks you once
        needs_resume = bool(s["auto"] and ((s["intraday"] and a is not None and not a.enabled
                                            and not getattr(a, "resume_pending", False)) or
                                           (s["swing"] and w is not None and not w.enabled)))
        return {k: s[k] for k in self.APP_DEFAULTS} | {
            "auto_id": a.id if a else None, "swing_id": w.id if w else None, "needs_resume": needs_resume,
            "intraday_on": bool(a and a.enabled), "swing_on": bool(w and w.enabled),
            "live_trading_enabled": settings.live_trading_enabled, "errors": s.get("errors", {}),
            "starting": bool(getattr(self, "_syncing", False))}

    def set_app_settings(self, upd: dict, confirm_live: str = "", now: datetime | None = None, background: bool = False) -> dict:
        """Save the app's mode and run (or stop) the app's own strategies to match. You never create or enable them.
        background=True (the app): answer at once and start the strategies in a thread — loading candles and the
        AI test can take minutes on a phone, and the app must not sit waiting for that."""
        now = now or datetime.now()
        with self.lock:
            s = self._app_raw()
            for k in self.APP_DEFAULTS:
                if k in upd and upd[k] is not None:
                    s[k] = upd[k]
            s["money"] = str(s["money"]).lower()
            if s["money"] not in ("paper", "live"):
                raise ValueError("Money must be paper or live")
            s["capital"] = float(s["capital"])
            if not (10_000 <= s["capital"] <= 100_000_000):
                raise ValueError("Capital must be between ₹10,000 and ₹10 crore")
            if s["swing_index"] not in ("NIFTY", "BANKNIFTY"):
                raise ValueError("Swing index must be NIFTY or BANKNIFTY")
            s["auto"], s["intraday"], s["swing"] = bool(s["auto"]), bool(s["intraday"]), bool(s["swing"])
            if s["money"] == "live" and s["auto"] and (s["intraday"] or s["swing"]):
                self._live_gate("live", 1, confirm_live)
            self.store.set("app_settings", s)
            if not background:
                self._sync_and_save(s, now)
                return self.app_settings()
            self._sync_gen = getattr(self, "_sync_gen", 0) + 1
            self._syncing = True
            gen = self._sync_gen
        threading.Thread(target=self._sync_and_save, args=(s, now, gen), daemon=True).start()
        return self.app_settings()

    def _sync_and_save(self, s: dict, now: datetime, gen: int | None = None):
        """Create/stop under the lock (fast); the slow part — loading candles, the AI test, the swing's first check —
        runs after it, so a second tap or a status request never waits minutes behind it."""
        try:
            with self.lock:
                if gen is not None and gen != self._sync_gen:
                    return  # a newer change arrived; that one wins
                s = self._app_raw() if gen is not None else s
                later: list = []
                s["errors"] = self._sync_managed(s, now, later)
                self.store.set("app_settings", s)
            errors = {}
            for key, fn in later:
                if gen is not None and gen != self._sync_gen:
                    break
                try:
                    fn()
                except Exception as e:
                    errors[key] = str(e)[:200]
            if errors:
                with self.lock:
                    st = self._app_raw()
                    st["errors"] = {**st.get("errors", {}), **errors}
                    self.store.set("app_settings", st)
        except Exception as e:
            with self.lock:
                st = self._app_raw()
                st["errors"] = {"start": str(e)[:200]}
                self.store.set("app_settings", st)
        finally:
            if gen is None or gen == self._sync_gen:
                self._syncing = False
        self.hub_refresh()

    def _sync_managed(self, s: dict, now: datetime, later: list) -> dict:
        errors: dict[str, str] = {}
        for money in ("paper", "live"):
            ids = s["managed"].setdefault(money, {})
            run = s["auto"] and money == s["money"]
            # --- intraday Autopilot
            a = self.autos.get(ids.get("auto", ""))
            if run and s["intraday"]:
                try:
                    if a is None:
                        a = self.create_auto(self._managed_auto_cfg(money, s))
                        a.log("Created by the app (Auto mode) — it runs itself", "control")
                        ids["auto"] = a.id
                    a.cfg.paper_capital = float(s["capital"])
                    if not a.enabled:
                        later.append(("intraday", lambda a=a: (a.enable(now), self._save_auto(a))))
                except Exception as e:
                    errors["intraday"] = str(e)[:200]
                if a is not None:
                    self._save_auto(a)
                    self.store.set("app_settings", s)
                    self._managed_upkeep()
            elif a is not None and a.enabled:
                a.disable()  # an open trade is still managed to its exit
                self._save_auto(a)
            # --- swing trend
            w = self.swings.get(ids.get("swing", ""))
            if run and s["swing"]:
                try:
                    if w is None:
                        w = self.create_swing(self._managed_swing_cfg(money, s))
                        ids["swing"] = w.id
                    w.cfg.capital = float(s["capital"])
                    if not w.active and w.cfg.index != s["swing_index"]:
                        w.cfg.index = s["swing_index"]
                    if not w.enabled:
                        w.enabled = True
                        w.log(f"Started by the app (Auto mode, {money}) — decides once a day at {w.cfg.check_time}", "control")
                        if w.cfg.check_time <= now.strftime("%H:%M") <= "15:25" and is_trading_day(now.date(), self.deps.data_dir):
                            later.append(("swing", lambda w=w: (w.check(now), self._save_swing(w))))
                except Exception as e:
                    errors["swing"] = str(e)[:200]
                if w is not None:
                    self._save_swing(w)
            elif w is not None and w.enabled:
                w.enabled = False
                w.log("Stopped by the app (Auto off or money mode changed) — an open position stays until it exits", "control")
                self._save_swing(w)
        return errors

    # ------------------------------------------------------------ swing trend (weeks, monthly ITM CE)
    def create_swing(self, cfg: SwingConfig) -> SwingTrader:
        cfg.validate()
        w = SwingTrader(cfg, self.deps, self._register, self._save_swing)
        self.swings[w.id] = w
        self._save_swing(w)
        return w

    def get_swing(self, sid: str) -> SwingTrader:
        if sid not in self.swings:
            raise KeyError("Swing strategy not found")
        return self.swings[sid]

    def update_swing(self, sid: str, cfg: SwingConfig) -> SwingTrader:
        w = self.get_swing(sid)
        cfg.validate()
        if w.active and (cfg.index != w.cfg.index or cfg.mode != w.cfg.mode):
            raise PermissionError("Close the open position before changing the index or paper/live")
        if cfg.mode == "live" and w.enabled and w.cfg.mode != "live":
            raise PermissionError("Stop it first, then switch to live and start again (you'll confirm real money)")
        w.cfg = cfg
        self._save_swing(w)
        return w

    def control_swing(self, sid: str, action: str, confirm_live: str = "", now: datetime | None = None) -> SwingTrader:
        w = self.get_swing(sid)
        now = now or datetime.now()
        if action == "enable":
            self._live_gate(w.cfg.mode, w.cfg.lots, confirm_live)
            w.enabled = True
            w.log(f"Started ({w.cfg.mode}) — decides once a day at {w.cfg.check_time} on trading days", "control")
            if w.cfg.check_time <= now.strftime("%H:%M") <= "15:25" and is_trading_day(now.date(), self.deps.data_dir):
                w.check(now)
        elif action == "disable":
            w.enabled = False
            w.log("Stopped — no new buys. An open position stays (use Close position to sell it)", "control")
        elif action == "close_position":
            w.close_now()
        elif action == "check_now":
            w.check(now)
        else:
            raise ValueError("action must be enable, disable, close_position or check_now")
        self._save_swing(w)
        self.hub_refresh()
        return w

    def delete_swing(self, sid: str):
        w = self.get_swing(sid)
        if w.active:
            raise PermissionError("Close the open position first")
        del self.swings[sid]
        self.store.delete(f"optswing:{sid}")

    def swing_trades(self, sid: str) -> list[dict]:
        out = []
        for p in sorted(self.positions.values(), key=lambda x: x.entry_time or x.created or "", reverse=True):
            if p.spec.source == f"swing:{sid}":
                out.append({"id": p.id, "tradingsymbol": p.spec.tradingsymbol, "strike": p.spec.strike, "expiry": p.spec.expiry,
                            "state": p.state, "qty": p.filled_qty or p.spec.qty, "entry_price": p.entry_price, "exit_price": p.exit_price,
                            "entry_time": p.entry_time, "exit_time": p.exit_time, "exit_reason": p.exit_reason,
                            "net": round(float(p.net), 2) if p.state == "POSITION_CLOSED" else None,
                            "unrealized": round(p.unrealized(), 2) if p.is_open else None})
        return out

    SWING_BT_DAYS = 3650

    def swing_backtest(self, index: str, fast: int = 10, slow: int = 50, days: int | None = None, cfg: SwingConfig | None = None) -> dict:
        """The same rule on Kite's daily history (up to ~10 years), option priced from India VIX history."""
        cfg = (cfg or SwingConfig(index=index, fast=fast, slow=slow)).validate()
        inst = self.ks.instruments()
        sp = spot_instrument(inst, cfg.index)
        end = datetime.now()
        start = end - timedelta(days=days or self.SWING_BT_DAYS)
        rows = self.deps.history(sp["instrument_token"], "day", start, end)
        if len(rows) < cfg.slow + 30:
            raise ValueError("Not enough daily history from Kite")
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        daily = df.set_index("date")[["high", "low", "close"]].astype(float)
        vix = None
        try:
            vrow = inst[(inst["exchange"] == "NSE") & (inst["tradingsymbol"] == "INDIA VIX")]
            if not vrow.empty:
                vr = pd.DataFrame(self.deps.history(int(vrow.iloc[0]["instrument_token"]), "day", start, end))
                vr["date"] = pd.to_datetime(vr["date"]).dt.tz_localize(None).dt.normalize()
                vix = vr.set_index("date")["close"].astype(float)
        except Exception:
            vix = None
        try:
            fut = nearest_future(inst, cfg.index, end.date())
            lot = int(fut["lot_size"]) if fut else {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}[cfg.index]
        except Exception:
            lot = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}[cfg.index]
        res = swing_backtest(daily, cfg, lot, float(index_cfg(cfg.index)["step"]), vix)
        res["index"], res["vix_history"] = cfg.index, vix is not None
        return to_python(res)

    # ------------------------------------------------------------ honest engine replay
    def replay_auto(self, aid: str, days: int, job=None) -> dict:
        from dataclasses import replace as dc_replace

        from .autotrade import AI_LOOKBACK_DAYS
        from .replay import replay_engine
        a = self.get_auto(aid)
        if not (5 <= days <= 60):
            raise ValueError("Replay 5-60 days")
        cfg = dc_replace(a.cfg, params=dict(a.cfg.params), risk=dict(a.cfg.risk))
        if job:
            job.progress = "Loading index candles"
        spot = spot_instrument(self.deps.instruments(), cfg.index)
        look = AI_LOOKBACK_DAYS.get(cfg.interval, 90) if cfg.strategy_source == "ai" else 20
        now = datetime.now()
        rows = self.deps.history(spot["instrument_token"], cfg.interval, now - timedelta(days=look + int(days * 1.5) + 10), now)
        if now.time() < datetime.strptime("15:30", "%H:%M").time():  # today is not finished: replay full days only
            rows = [r for r in rows if pd.Timestamp(r["date"]).date() < now.date()]
        iv = a.vix() or 14.0

        def prog(m):
            if job:
                job.progress = m
        res = replay_engine(cfg, rows, days=days, iv=iv, progress=prog, cancelled=(job.cancel.is_set if job else None))
        res["ran_at"] = now.isoformat(timespec="seconds")
        res = to_python(res)
        self.store.set(f"replay:{aid}", res)
        return res

    def last_replay(self, aid: str) -> dict | None:
        self.get_auto(aid)
        return self.store.get(f"replay:{aid}")

    # ------------------------------------------------------------ morning analysis
    def morning_analysis(self, index: str, interval: str = "5minute") -> dict:
        now = datetime.now()
        inst = self.ks.instruments()
        spot = spot_instrument(inst, index)
        rows = self.ks.historical(spot["instrument_token"], interval, now - timedelta(days=12), now)
        if len(rows) < 60:
            raise ValueError("Not enough index candles yet")
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        today = now.date()
        prev = df[df["date"].dt.date < today]
        cur = df[df["date"].dt.date == today]
        lv = {}
        if len(prev):
            pdd = prev[prev["date"].dt.date == prev["date"].dt.date.max()]
            h, l, c = float(pdd["high"].max()), float(pdd["low"].min()), float(pdd["close"].iloc[-1])
            lv = {"prev_day": pdd["date"].dt.date.iloc[0].isoformat(), "open": float(pdd["open"].iloc[0]), "high": h,
                  "low": l, "close": c, "range_pct": round((h - l) / c * 100, 2), **pivots(h, l, c),
                  "trend": "UP" if c > float(pdd["open"].iloc[0]) else "DOWN"}
        today_info = {}
        if len(cur):
            o = float(cur["open"].iloc[0])
            today_info = {"open": o, "high": float(cur["high"].max()), "low": float(cur["low"].min()),
                          "last": float(cur["close"].iloc[-1]), "candles": len(cur)}
            if lv:
                today_info["gap_pct"] = round((o - lv["close"]) / lv["close"] * 100, 2)
                today_info["gap"] = "GAP UP" if today_info["gap_pct"] > 0.25 else "GAP DOWN" if today_info["gap_pct"] < -0.25 else "FLAT"
                last = today_info["last"]
                today_info["vs_levels"] = ("above R1" if last > lv["R1"] else "above pivot" if last > lv["P"]
                                           else "below S1" if last < lv["S1"] else "below pivot")
        snap = technical_snapshot(rows)
        # ATM option quotes for the nearest valid expiry
        atm = {}
        try:
            exps = [e for e in expiries(inst, index, today) if e != today.isoformat()] or expiries(inst, index, today)
            spot_px = today_info.get("last") or snap["close"]
            keys = {}
            for ot in ("CE", "PE"):
                c = select_contract(inst, index, exps[0], ot, spot_px, "ATM", 0)
                keys[ot] = (c, f"{c.exchange}:{c.tradingsymbol}")
            qs = self.ks.kite.quote([k for _, k in keys.values()])
            for ot, (c, k) in keys.items():
                q = qs.get(k, {})
                ltp, bid, ask = _best_prices(q)
                pc = (q.get("ohlc") or {}).get("close")
                atm[ot] = {"tradingsymbol": c.tradingsymbol, "strike": c.strike, "expiry": c.expiry, "lot_size": c.lot_size,
                           "ltp": ltp, "bid": bid, "ask": ask, "prev_close": pc,
                           "change_pct": round((ltp - pc) / pc * 100, 2) if ltp and pc else None,
                           "spread_pct": round((ask - bid) / ((ask + bid) / 2) * 100, 2) if bid and ask else None}
        except Exception as e:
            atm = {"error": f"{e}"[:200]}
        ph = phase(now, "09:15", "09:30", "10:30", "10:30", settings.data_dir)
        direction = 1 if snap["score"] >= 25 else -1 if snap["score"] <= -25 else 0
        suggestion = "WAIT"
        if direction and ph in ("ENTRY",) and not (abs(today_info.get("gap_pct", 0)) > 1.5):
            suggestion = "BUY_CE" if direction > 0 else "BUY_PE"
        return to_python({
            "index": index.upper(), "time": now.isoformat(timespec="seconds"), "phase": ph,
            "trading_day": is_trading_day(today, settings.data_dir),
            "previous_day": lv, "today": today_info, "technical": snap, "atm_options": atm,
            "bias_suggestion": suggestion,
            "note": ("Bias only. Auto Trade executes only on a confirmed strategy signal inside 09:30-10:30 "
                     "with mandatory stop-loss and risk checks. Not investment advice."),
        })

    # ------------------------------------------------------------ analytics
    def analytics(self, source: str | None = None, mode: str | None = None) -> dict:
        ps = [p for p in self.positions.values()
              if (source is None or p.spec.source.startswith(source)) and (mode is None or p.mode == mode)]
        closed = [p for p in ps if p.state == "POSITION_CLOSED"]
        closed.sort(key=lambda p: p.exit_time or "")
        nets = np.array([p.net for p in closed], dtype=float)
        wins, losses = nets[nets > 0], nets[nets <= 0]
        eq = np.cumsum(nets) if len(nets) else np.array([0.0])
        peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))
        dd = float((np.concatenate([[0.0], eq]) - peak).min())
        st = lambda k: sum(p.stats.get(k, 0) or 0 for p in ps)  # noqa: E731
        sl_closes = [p for p in closed if p.exit_reason in ("stop_loss", "trailing_stop")]
        tgt_closes = [p for p in closed if p.exit_reason == "target"]
        slips = [p.stats["entry_slippage_pct"] for p in ps if p.stats.get("entry_slippage_pct") is not None]
        sigs = []
        for a in self.autos.values():
            if source is None or f"auto:{a.id}".startswith(source):
                sigs += a.signals
        return to_python({
            "positions": len(ps), "closed_trades": len(closed), "open_or_pending": sum(1 for p in ps if p.state in ACTIVE_STATES),
            "win_rate_pct": round(len(wins) / len(nets) * 100, 1) if len(nets) else 0.0,
            "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
            "avg_win_loss_ratio": round(float(wins.mean() / -losses.mean()), 2) if len(wins) and len(losses) and losses.mean() < 0 else None,
            "net_after_costs": round(float(nets.sum()), 2), "total_charges": round(sum(p.charges for p in closed), 2),
            "max_drawdown": round(dd, 2),
            "signals": len(sigs), "signals_passed": sum(1 for s in sigs if s.get("risk_check") == "PASS"),
            "signal_to_order_pct": round(sum(1 for s in sigs if s.get("risk_check") == "PASS") / len(sigs) * 100, 1) if sigs else None,
            "orders_sent": st("orders_sent"), "orders_rejected": st("orders_rejected"), "orders_filled": st("orders_filled"),
            "order_acceptance_pct": round((st("orders_sent") - st("orders_rejected")) / st("orders_sent") * 100, 1) if st("orders_sent") else None,
            "fill_rate_pct": round(st("orders_filled") / st("orders_sent") * 100, 1) if st("orders_sent") else None,
            "avg_entry_slippage_pct": round(float(np.mean(slips)), 3) if slips else None,
            "stop_exits": len(sl_closes), "stop_recoveries_needed": st("stop_recoveries"),
            "stop_execution_success_pct": round((len(sl_closes) - st("stop_recoveries")) / len(sl_closes) * 100, 1) if sl_closes else None,
            "target_exits": len(tgt_closes),
            "exit_reasons": {r: sum(1 for p in closed if p.exit_reason == r) for r in sorted({p.exit_reason for p in closed if p.exit_reason})},
            **performance_breakdown(closed),
            **self._costs(closed),
            **self._auto_extras(source, closed),
            "skipped": self._skips_for(source),
        })

    @staticmethod
    def _costs(closed: list) -> dict:
        net, charges = sum(float(p.net) for p in closed), sum(float(p.charges) for p in closed)
        gross = net + charges
        return {"gross_before_costs": round(gross, 2),
                "costs_share_pct": round(charges / gross * 100, 1) if gross > 0 else None,
                "avg_cost_per_trade": round(charges / len(closed), 2) if closed else None}

    def _auto_extras(self, source: str | None, closed: list) -> dict:
        """For one Auto Trade: settings versions, the go-live scorecard on the CURRENT version, and self-tuning hints."""
        if not source or not source.startswith("auto:"):
            return {}
        a = self.autos.get(source.split(":", 1)[1])
        if a is None:
            return {}
        v = a.settings_v
        cur = [p for p in closed if int((p.stats or {}).get("settings_v") or 1) == v]
        m = getattr(a, "money", None) or {}
        cap = m.get("capital") or a.cfg.account_capital or (a.cfg.paper_capital if a.cfg.mode == "paper" else None)
        qnow = None if a.cfg.quick_target_auto else (a.cfg.quick_target_pts or None)
        return {"settings_version": v, "versions": a.versions[-10:],
                "scorecard": {**golive_scorecard(cur, cap), "version": v},
                "tuning": tuning_suggestions(cur if len(cur) >= 30 else closed, qnow)}
