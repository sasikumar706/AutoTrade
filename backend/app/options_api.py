"""HTTP endpoints for index options: chain, contract quote, morning analysis, options
backtest jobs (RUN/STOP), manual paper/live trades, Auto Trade and analytics."""
from __future__ import annotations

import threading
import time
from dataclasses import fields
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .autotrade import AutoConfig
from .jobs import JobRunner
from .options import INDICES, chain, index_cfg, spot_instrument
from .options_backtest import OptionsBacktestConfig, RealPremiums, run_options_backtest
from .options_manager import OptionsManager
from .risk import RiskConfig


class RiskModel(BaseModel):
    sl_mode: str = "pct"
    sl_value: float = 20.0
    target_mode: str = "rr"
    target_value: float = 2.1
    trail_mode: str = "none"
    trail_value: float = 0.0
    trail_activate_rr: float = 1.0
    max_risk_per_trade: float | None = None
    min_sl_pct: float = 2.0
    max_sl_pct: float = 60.0


class OptionsBacktestRequest(BaseModel):
    index: str = "NIFTY"
    interval: str = "5minute"
    from_date: str
    to_date: str
    strategy: str = "morning_momentum"
    params: dict[str, Any] = Field(default_factory=dict)
    side: str = "BUY"
    option_rule: str = "AUTO"
    strike_method: str = "ATM"
    strike_offset: int = 1
    lots: int = 1
    premium_source: str = "estimated"
    iv_pct: float | None = None
    min_days_to_expiry: int = 0
    expiry: str | None = None
    initial_capital: float = 100_000
    risk: RiskModel = Field(default_factory=RiskModel)
    slippage_bps: float = 30
    entry_start: str = "09:30"
    entry_end: str = "10:30"
    exit_time: str = "10:30"
    max_trades_per_day: int = 2
    max_daily_loss: float | None = None
    allow_option_selling: bool = False
    reverse_on_opposite: bool = False


class ContractQuoteRequest(BaseModel):
    index: str = "NIFTY"
    expiry: str
    option_type: str = "CE"
    strike_method: str = "ATM"
    strike_offset: int = 1
    lots: int = 1
    side: str = "BUY"
    product: str = "MIS"
    risk: RiskModel = Field(default_factory=RiskModel)


class ManualOrderRequest(ContractQuoteRequest):
    mode: str = "paper"
    confirm_live: str = ""
    allow_option_selling: bool = False


class AutoRequest(BaseModel):
    mode: str = "paper"
    index: str = "NIFTY"
    strategy: str = "morning_momentum"
    params: dict[str, Any] = Field(default_factory=dict)
    strategy_source: str = "manual"
    autopilot: bool = False
    use_combo: bool = True
    interval: str = "5minute"
    side: str = "BUY"
    option_rule: str = "AUTO"
    expiry: str = "NEAREST"
    skip_expiry_day: bool = True
    strike_method: str = "ATM"
    strike_offset: int = 1
    lots: int = 1
    product: str = "MIS"
    risk: RiskModel = Field(default_factory=RiskModel)
    analysis_start: str = "09:15"
    entry_start: str = "09:30"
    entry_end: str = "10:30"
    exit_time: str = "10:30"
    max_trades_per_day: int = 2
    max_daily_loss: float = 2000
    max_capital: float = 50_000
    max_spread_pct: float = 3.0
    max_open_gap_pct: float = 1.5
    signal_valid_seconds: int = 90
    entry_tolerance_pts: float = 1.0
    fit_lots_to_limits: bool = True
    join_window_minutes: int = 15
    trend_override_min: int = 6
    consensus_min: int = 3
    any_strategy_entry: bool = True
    level_tolerance_pct: float = 0.0
    min_confidence: int = 60
    allow_option_selling: bool = False
    slippage_bps: float = 30


class ReplayReq(BaseModel):
    days: int = 20


class ReportReq(BaseModel):
    days: int = 30
    mode: str = "all"
    source: str = "all"


class EventItem(BaseModel):
    day: str
    event: str = "Event"


class SwingRequest(BaseModel):
    index: str = "NIFTY"
    fast: int = 10
    slow: int = 50
    mode: str = "paper"
    lots: int = 1
    min_days_to_expiry: int = 20
    roll_days: int = 7
    check_time: str = "15:15"
    disaster_stop_pct: float = 60.0
    max_loss_pct: float = 2.0
    max_premium: float = 0.0
    sizing: str = "capital"
    capital: float = 200000.0
    alloc_pct: float = 10.0
    max_lots: int = 10
    itm_strikes: int = 2
    book_half_pct: float = 6.0
    trail_atr: float = 0.0
    crash_guard_atr: float = 2.0


class AppSettingsReq(BaseModel):
    money: str | None = None
    auto: bool | None = None
    intraday: bool | None = None
    swing: bool | None = None
    capital: float | None = None
    swing_index: str | None = None
    confirm_live: str = ""


class ControlRequest(BaseModel):
    action: str
    confirm_live: str = ""


def _auto_cfg(req: AutoRequest) -> AutoConfig:
    known = {f.name for f in fields(AutoConfig)}
    d = {k: v for k, v in req.model_dump().items() if k in known}
    d["risk"] = req.risk.model_dump()
    return AutoConfig(**d)


def register(app, auth, kite, store, hub, errors, dt):
    om = OptionsManager(kite, store, hub)
    jobs = JobRunner()
    app.state_options_manager = om

    def clock():
        while True:
            time.sleep(3)
            try:
                om.on_clock(datetime.now())
            except Exception:
                pass

    threading.Thread(target=clock, daemon=True).start()

    @app.get("/options/indices", dependencies=auth)
    def options_indices():
        return [{"key": k, "step": v["step"], "exchange": v["opt_exchange"], "expiry_rule": v["expiry_rule"]} for k, v in
                INDICES.items()]

    @app.get("/options/chain-info", dependencies=auth)
    def options_chain_info(index: str = "NIFTY"):
        return errors(lambda: om.chain_info(index))

    @app.get("/options/strikes", dependencies=auth)
    def options_strikes(index: str, expiry: str):
        def go():
            ch = chain(kite.instruments(), index, expiry)
            spot = om.spot_price(index)
            strikes = sorted(set(float(s) for s in ch["strike"]))
            near = sorted(strikes, key=lambda s: abs(s - spot))[:41]
            return {"spot": spot, "strikes": sorted(near), "lot_size": int(ch["lot_size"].iloc[0])}
        return errors(go)

    @app.post("/options/quote", dependencies=auth)
    def options_quote(req: ContractQuoteRequest):
        return errors(lambda: om.contract_quote(req.index, req.expiry, req.option_type, req.strike_method, req.strike_offset,
                                                req.lots, req.side, req.product, req.risk.model_dump()))

    @app.get("/options/analysis", dependencies=auth)
    def options_analysis(index: str = "NIFTY", interval: str = "5minute"):
        return errors(lambda: om.morning_analysis(index, interval))

    # ---------------- backtest jobs: RUN / STOP
    @app.post("/options/backtest", dependencies=auth)
    def options_backtest(req: OptionsBacktestRequest):
        def prepare():
            known = {f.name for f in fields(OptionsBacktestConfig)}
            d = {k: v for k, v in req.model_dump().items() if k in known}
            d["risk"] = req.risk.model_dump()
            cfg = OptionsBacktestConfig(**d)
            inst = kite.instruments()
            if cfg.premium_source == "real":
                if not cfg.expiry:
                    raise ValueError("Real-premium mode needs a listed expiry")
                ch = chain(inst, cfg.index, cfg.expiry)
                cfg.lot_size = int(ch["lot_size"].iloc[0])
            else:
                from .options import _opts
                o = _opts(inst, cfg.index)
                if o.empty:
                    raise ValueError("No listed contracts to read the current lot size from")
                cfg.lot_size = int(o["lot_size"].iloc[0])
            cfg.validate()
            return cfg, inst, ch if cfg.premium_source == "real" else None
        cfg, inst, ch = errors(prepare)

        def run(job):
            spot = spot_instrument(inst, cfg.index)
            start, end = dt(req.from_date), dt(req.to_date, end=True)
            job.progress = "Downloading index candles"
            idx = kite.historical(spot["instrument_token"], req.interval, start, end)
            if len(idx) < 30:
                raise ValueError(f"Only {len(idx)} index candles in range")
            src = None
            if cfg.premium_source == "real":
                rows = ch[["strike", "instrument_type", "instrument_token", "tradingsymbol", "lot_size"]].to_dict("records")

                def fetch(token):
                    if job.cancel.is_set():
                        from .options_backtest import Cancelled
                        raise Cancelled()
                    job.progress = f"Downloading option candles ({token})"
                    return kite.historical(int(token), req.interval, start, end)
                import pandas as pd
                from .backtest import prepare_candles
                src = RealPremiums(cfg, prepare_candles(idx), rows, fetch)
            job.progress = "Simulating trades"
            res = run_options_backtest(idx, cfg, src, cancel=job.cancel)
            res["index"] = cfg.index
            res["interval"] = req.interval
            return res

        j = jobs.start("options_backtest", run)
        return j.snapshot()

    @app.get("/jobs/{jid}", dependencies=auth)
    def job_get(jid: str):
        return errors(lambda: jobs.get(jid).snapshot())

    @app.post("/jobs/{jid}/cancel", dependencies=auth)
    def job_cancel(jid: str):
        return errors(lambda: jobs.cancel(jid).snapshot(with_result=False))

    # ---------------- manual trades (Paper tab / Live tab)
    @app.post("/options/order", dependencies=auth)
    def options_order(req: ManualOrderRequest):
        return errors(lambda: om.manual_open(req.mode, req.index, req.expiry, req.option_type, req.strike_method,
                                             req.strike_offset, req.lots, req.side, req.product, req.risk.model_dump(),
                                             req.confirm_live, req.allow_option_selling).snapshot())

    def _light(p):
        """List-row snapshot: no event log / price path (those are only needed on the position screen)."""
        d = p.snapshot()
        d.pop("events", None)
        d.pop("path", None)
        return d

    def _pday(p) -> str:
        return str(p.exit_time or p.entry_time or p.created or "")[:10]

    @app.get("/positions", dependencies=auth)
    def positions_list(mode: str | None = None, active_only: bool = False, day: str | None = None, light: bool = False):
        from .positions import ACTIVE_STATES
        ps = [p for p in om.positions.values() if (mode is None or p.mode == mode)
              and (not active_only or p.state in ACTIVE_STATES)
              and (day is None or _pday(p) == day)]
        ps.sort(key=lambda p: p.created, reverse=True)
        return [(_light(p) if light else p.snapshot()) for p in ps[:100]]

    @app.get("/positions/today", dependencies=auth)
    def positions_today(mode: str | None = None):
        """What the app's 'today' screens need: every still-open position (even one opened on an earlier day, e.g.
        Swing) plus everything that started or finished today. Light rows, newest first."""
        from .positions import ACTIVE_STATES
        today = datetime.now().date().isoformat()
        ps = [p for p in om.positions.values() if (mode is None or p.mode == mode)
              and (p.state in ACTIVE_STATES or _pday(p) == today or str(p.created or "")[:10] == today)]
        ps.sort(key=lambda p: p.created, reverse=True)
        return [_light(p) for p in ps]

    @app.get("/positions/past", dependencies=auth)
    def positions_past(mode: str | None = None, source: str | None = None, limit: int = 300):
        """Closed trades from EARLIER days only (today's live on the Home/Trade screens), newest first.
        source: auto | swing | manual."""
        today = datetime.now().date().isoformat()
        ps = [p for p in om.positions.values() if p.state == "POSITION_CLOSED" and (mode is None or p.mode == mode)
              and _pday(p) < today
              and (source is None or str(p.spec.source or "manual").split(":")[0] == source)]
        ps.sort(key=lambda p: str(p.exit_time or p.entry_time or ""), reverse=True)
        return [_light(p) for p in ps[:max(1, min(limit, 1000))]]

    @app.get("/positions/days", dependencies=auth)
    def positions_days(mode: str | None = None):
        """Closed trades' P&L per day (newest first): trades, wins, net, and the split by who placed them."""
        days: dict[str, dict] = {}
        for p in om.positions.values():
            if p.state != "POSITION_CLOSED" or (mode and p.mode != mode):
                continue
            k = str(p.exit_time or p.entry_time or "")[:10]
            d = days.setdefault(k, {"day": k, "trades": 0, "wins": 0, "net": 0.0, "by": {}})
            net = float(p.net or 0)
            src = str(p.spec.source or "manual").split(":")[0]
            d["trades"] += 1
            d["wins"] += net > 0
            d["net"] = round(d["net"] + net, 2)
            d["by"][src] = round(d["by"].get(src, 0.0) + net, 2)
        return sorted(days.values(), key=lambda d: d["day"], reverse=True)

    @app.delete("/positions/history", dependencies=auth)
    def positions_clear(mode: str | None = None, source: str | None = None):
        """Clear finished positions from the app (not from Zerodha). Open positions are kept."""
        return {"ok": True, "deleted": om.clear_history(mode, source)}

    @app.get("/positions/{pid}", dependencies=auth)
    def position_get(pid: str):
        return errors(lambda: om.get_position(pid).snapshot())

    @app.post("/positions/{pid}/close", dependencies=auth)
    def position_close(pid: str):
        return errors(lambda: om.close_position(pid).snapshot())

    @app.post("/positions/reconcile", dependencies=auth)
    def positions_reconcile():
        return errors(om.reconcile_all)

    # ---------------- Auto Trade
    @app.get("/auto", dependencies=auth)
    def auto_list(light: bool = False):
        return [a.light_snapshot() if light else a.snapshot() for a in om.autos.values()]

    @app.post("/auto", dependencies=auth)
    def auto_create(req: AutoRequest):
        return errors(lambda: om.create_auto(_auto_cfg(req)).snapshot())

    @app.put("/auto/{aid}", dependencies=auth)
    def auto_update(aid: str, req: AutoRequest):
        return errors(lambda: om.update_auto(aid, _auto_cfg(req)).snapshot())

    @app.get("/auto/{aid}", dependencies=auth)
    def auto_get(aid: str):
        return errors(lambda: om.get_auto(aid).snapshot())

    @app.post("/auto/{aid}/control", dependencies=auth)
    def auto_control(aid: str, req: ControlRequest):
        return errors(lambda: om.control_auto(aid, req.action, req.confirm_live).snapshot())

    @app.delete("/auto/{aid}", dependencies=auth)
    def auto_delete(aid: str):
        def go():
            om.delete_auto(aid)
            return {"ok": True}
        return errors(go)

    # ---------------- App settings: Paper/Live + Auto/Manual (the app runs its own strategies)
    @app.get("/app-settings", dependencies=auth)
    def app_settings_get():
        return om.app_settings()

    @app.put("/app-settings", dependencies=auth)
    def app_settings_put(req: AppSettingsReq):
        d = req.model_dump()
        confirm = d.pop("confirm_live", "")
        return errors(lambda: om.set_app_settings(d, confirm, background=True))

    # ---------------- Swing trend (weeks)
    from .swing import SwingConfig

    @app.get("/swing", dependencies=auth)
    def swing_list():
        return [w.snapshot() for w in om.swings.values()]

    @app.post("/swing", dependencies=auth)
    def swing_create(req: SwingRequest):
        return errors(lambda: om.create_swing(SwingConfig(**req.model_dump())).snapshot())

    @app.put("/swing/{sid}", dependencies=auth)
    def swing_update(sid: str, req: SwingRequest):
        return errors(lambda: om.update_swing(sid, SwingConfig(**req.model_dump())).snapshot())

    @app.get("/swing/{sid}", dependencies=auth)
    def swing_get(sid: str):
        return errors(lambda: {**om.get_swing(sid).snapshot(), "trades": om.swing_trades(sid)})

    @app.post("/swing/{sid}/control", dependencies=auth)
    def swing_control(sid: str, req: ControlRequest):
        return errors(lambda: om.control_swing(sid, req.action, req.confirm_live).snapshot())

    @app.delete("/swing/{sid}", dependencies=auth)
    def swing_delete(sid: str):
        def go():
            om.delete_swing(sid)
            return {"ok": True}
        return errors(go)

    @app.get("/swing-backtest", dependencies=auth)
    def swing_bt(index: str = "NIFTY", fast: int = 10, slow: int = 50, days: int = 3650, sid: str = ""):
        def go():
            cfg = om.get_swing(sid).cfg if sid else None
            return om.swing_backtest(cfg.index if cfg else index, fast, slow, min(max(days, 400), 4000), cfg)
        return errors(go)

    @app.post("/auto/{aid}/preview-signal", dependencies=auth)
    def auto_preview(aid: str, direction: int = 1):
        """Dry run: what would the strategy order right now for a bullish(1)/bearish(-1) signal?"""
        def go():
            a = om.get_auto(aid)
            if a.day != datetime.now().date():
                a.start_day(datetime.now())
            if a.last_spot is None:
                a.last_spot = om.spot_price(a.cfg.index)
            sig, _, _ = a.build_signal(1 if direction >= 0 else -1, None, datetime.now())
            return sig
        return errors(go)

    @app.get("/events", dependencies=auth)
    def events_get():
        from .config import settings
        from .events import upcoming, user_events
        return {"upcoming": upcoming(datetime.now().date(), settings.data_dir, 12),
                "mine": [{"day": k, "event": v} for k, v in sorted(user_events(settings.data_dir).items())]}

    @app.put("/events", dependencies=auth)
    def events_put(items: list[EventItem]):
        from .config import settings
        from .events import save_user_events
        errors(lambda: save_user_events(settings.data_dir, [i.model_dump() for i in items]))
        return events_get()

    @app.post("/auto/{aid}/replay", dependencies=auth)
    def auto_replay(aid: str, req: ReplayReq):
        errors(lambda: om.get_auto(aid))
        j = jobs.start("auto_replay", lambda job: om.replay_auto(aid, req.days, job))
        return j.snapshot()

    @app.get("/auto/{aid}/replay", dependencies=auth)
    def auto_replay_last(aid: str):
        return errors(lambda: om.last_replay(aid)) or {}

    _report_links: dict[str, tuple[float, dict]] = {}

    @app.post("/report/link", dependencies=auth)
    def report_link(req: ReportReq):
        """A short-lived link (10 min) the phone's browser can open without the app token."""
        import secrets
        import time as _t
        if not (1 <= req.days <= 400) or req.mode not in ("all", "paper", "live") or req.source not in ("all", "auto", "manual"):
            raise HTTPException(400, "days 1-400, mode all/paper/live, source all/auto/manual")
        now = _t.time()
        for k in [k for k, (t, _) in _report_links.items() if now - t > 600]:
            _report_links.pop(k, None)
        key = secrets.token_urlsafe(18)
        _report_links[key] = (now, req.model_dump())
        return {"html": f"/report/{key}.html", "csv": f"/report/{key}.csv", "expires_in_s": 600}

    @app.get("/report/{key}.{fmt}")
    def report_get(key: str, fmt: str):
        import time as _t
        from fastapi.responses import HTMLResponse, Response
        from .report import to_csv, to_html, trade_rows
        item = _report_links.get(key)
        if not item or _t.time() - item[0] > 600:
            raise HTTPException(404, "This report link has expired — make a new one in the app")
        q = item[1]
        rows = trade_rows(list(om.positions.values()), q["days"], q["mode"], q["source"])
        if fmt == "csv":
            return Response(to_csv(rows), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="kitealgo-trades-{datetime.now():%Y%m%d}.csv"'})
        if fmt == "html":
            return HTMLResponse(to_html(rows, q["days"], q["mode"], q["source"], om._skips_for(None)))
        raise HTTPException(404, "Unknown format")

    @app.get("/spreads", dependencies=auth)
    def spreads():
        from .config import settings
        from .quality import calibrated_slippage_pct, measured_spreads
        from .autotrade import OPTION_SLIPPAGE_PCT
        m = measured_spreads(settings.data_dir)
        m["slippage_pct_used"], m["slippage_source"] = calibrated_slippage_pct(settings.data_dir, OPTION_SLIPPAGE_PCT)
        return m

    @app.get("/auto-health", dependencies=auth)
    def auto_health():
        return errors(lambda: om.readiness(datetime.now()))

    @app.get("/analytics", dependencies=auth)
    def analytics(source: str | None = None, mode: str | None = None):
        return om.analytics(source, mode)

    return om
