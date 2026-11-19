"""HTTP API used by the mobile app.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000   (behind HTTPS, see deploy/)
Every endpoint except /health and /auth/callback needs:  Authorization: Bearer <APP_TOKEN>
"""
from __future__ import annotations

import re

import hmac
from dataclasses import fields
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import notify as _notify
from . import ai
from . import market as mkt
from .analysis import find_strategies, forecast, rule_based_summary, technical_snapshot
from .backtest import BacktestConfig, run_backtest
from .charges import round_trip_charges, segment_for
from .config import settings
from .engine import SessionConfig
from .kite_service import KiteService, NotLoggedIn
from .manager import SessionManager
from .store import Store
from .strategies import REGISTRY

app = FastAPI(title="Kite Algo Backend", version="1.0.0")
store = Store(settings.data_dir / "app.db")
kite = KiteService(store)
manager = SessionManager(kite, store)


def require_app_token(authorization: str = Header(default="")):
    if not settings.app_token or len(settings.app_token) < 24:
        raise HTTPException(500, "Server APP_TOKEN is not set (use 24+ random characters)")
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, settings.app_token):
        raise HTTPException(401, "Invalid app token")


auth = [Depends(require_app_token)]


def _kite_errors(fn):
    try:
        return fn()
    except NotLoggedIn as e:
        raise HTTPException(401, str(e))
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except HTTPException:
        raise
    except Exception as e:  # kiteconnect exceptions carry a readable message
        name = type(e).__name__
        if name == "TokenException":
            # Zerodha no longer accepts the saved token (daily expiry, logout elsewhere, or a different API key):
            # forget it so the app shows "Logged out" and offers "Login with Kite" instead of failing everywhere
            try:
                kite.store.delete("kite_session")
                with kite._lock:
                    kite._kite = None
            except Exception:
                pass
            raise HTTPException(401, f"Kite session expired — log in again from Home ({e})")
        raise HTTPException(502, f"{name}: {e}")


# ------------------------------------------------------------------ health & auth


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat(), "live_trading_enabled": settings.live_trading_enabled}


@app.get("/auth/login-url", dependencies=auth)
def login_url():
    if not settings.kite_api_key:
        raise HTTPException(500, "KITE_API_KEY not set on server")
    return {"url": kite.login_url()}


@app.get("/auth/callback")
def auth_callback(request_token: str = "", status: str = "", action: str = ""):
    """Kite redirects here after the user logs in (set this as the app's Redirect URL)."""
    if status != "success" or not request_token:
        return RedirectResponse(f"{settings.app_redirect}?status=error&message=" + quote("Login cancelled"))
    try:
        info = kite.complete_login(request_token)
        manager.hub.restart()  # running sessions resume with today's token
        try:
            options_manager.reconcile_all()  # live option positions: Kite is the source of truth
            options_manager.hub_refresh(force=True)
        except Exception:
            pass
    except Exception as e:
        return RedirectResponse(f"{settings.app_redirect}?status=error&message=" + quote(str(e)[:200]))
    return RedirectResponse(f"{settings.app_redirect}?status=ok&user=" + quote(info.get("user_id") or ""))


@app.get("/auth/status", dependencies=auth)
def auth_status():
    info = kite.session_info()
    return {"logged_in": info is not None, "session": info, "ticker": manager.ticker_status(),
            "live_trading_enabled": settings.live_trading_enabled}


@app.get("/auth/session-export", dependencies=auth)
def session_export():
    """Hand today's Kite session to your other server (phone <-> PC). Needs the app token."""
    return _kite_errors(kite.export_session)


class SessionImport(BaseModel):
    access_token: str
    login_time: str | None = None


@app.post("/auth/session-import", dependencies=auth)
def session_import(req: SessionImport):
    def go():
        info = kite.import_session(req.access_token, req.login_time)
        manager.hub.restart()
        try:
            options_manager.reconcile_all()
            options_manager.hub_refresh(force=True)
        except Exception:
            pass
        return {"ok": True, "session": info}
    return _kite_errors(go)


_public_ip: dict = {"ip": None, "at": 0.0}


@app.get("/server/info", dependencies=auth)
def server_info():
    """What this server is: live switch, and the public IP Zerodha sees (must be whitelisted for live orders)."""
    import platform
    import time as _t

    if _t.time() - _public_ip["at"] > 600:
        try:
            import httpx
            _public_ip["ip"] = httpx.get("https://api.ipify.org", timeout=5).text.strip()
        except Exception:
            _public_ip["ip"] = None
        _public_ip["at"] = _t.time()
    return {"live_trading_enabled": settings.live_trading_enabled, "public_ip": _public_ip["ip"],
            "platform": platform.system(), "host": platform.node(), "max_live_lots": settings.max_live_lots,
            "max_live_daily_loss": settings.max_live_daily_loss, "notifications": _notify.enabled(), "notify_channels": _notify.channels()}


@app.post("/client-log", dependencies=auth)
def client_log(entry: dict):
    """Crash / error reports from the mobile app (data/app_errors.log, newest last)."""
    import json as _json
    f = settings.data_dir / "app_errors.log"
    try:
        with f.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps({k: str(v)[:1500] for k, v in (entry or {}).items()}) + "\n")
        lines = f.read_text(encoding="utf-8").splitlines()
        if len(lines) > 200:
            f.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}


@app.get("/client-log", dependencies=auth)
def client_log_get():
    import json as _json
    f = settings.data_dir / "app_errors.log"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines()[-50:]:
        try:
            out.append(_json.loads(line))
        except Exception:
            pass
    return out


@app.post("/notify/test", dependencies=auth)
def notify_test():
    if not _notify.enabled():
        raise HTTPException(400, "Notifications are off. On the phone run in Termux:  pkg install termux-api  "
                                 "(and install the Termux:API app from the same store as Termux), then restart the server.")
    ok = _notify.send("Kite Algo", "Test notification — alerts for orders, exits and problems will arrive like this.", wait=True)
    if not ok:
        raise HTTPException(502, "Could not show the notification — open the Termux:API app once and allow notifications "
                                 "(Android Settings → Apps → Termux:API → Notifications)")
    return {"ok": True, "channels": _notify.channels()}


@app.post("/auth/logout", dependencies=auth)
def logout():
    for s in list(manager.sessions.values()):
        if s.status == "running" and s.cfg.mode in ("paper", "live"):
            raise HTTPException(409, "Stop running paper/live sessions before logging out")
    kite.logout()
    manager.hub.stop()
    return {"ok": True}


# ------------------------------------------------------------------ market data


@app.get("/instruments/search", dependencies=auth)
def instruments_search(q: str, exchange: str | None = None, cash_only: bool = False):
    """cash_only: stocks and indices on NSE/BSE only (no futures/options), NSE first."""
    def go():
        if not cash_only:
            return kite.search_instruments(q, exchange)
        rows = kite.search_instruments(q, exchange, limit=200)
        rows = [r for r in rows if r.get("exchange") in ("NSE", "BSE") and r.get("instrument_type") == "EQ"]
        rows.sort(key=lambda r: (r["tradingsymbol"] != q.strip().upper(), r.get("segment") != "INDICES", r["exchange"] != "NSE"))
        return rows[:30]
    return _kite_errors(go)


@app.get("/market/overview", dependencies=auth)
def market_overview():
    return _kite_errors(lambda: mkt.market_overview(kite))


@app.get("/market/detail", dependencies=auth)
def market_detail(exchange: str, tradingsymbol: str):
    return _kite_errors(lambda: mkt.market_detail(kite, exchange, tradingsymbol))


@app.get("/quote/ltp", dependencies=auth)
def quote_ltp(symbols: str = Query(..., description="Comma separated, e.g. NSE:INFY,NSE:TCS")):
    return _kite_errors(lambda: kite.ltp([s.strip() for s in symbols.split(",") if s.strip()][:50]))


_INDEX_QUOTE = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "SENSEX": "BSE:SENSEX"}


def index_moves(quotes: dict) -> dict:
    """Last price and today's move (points and %) vs previous close for each index."""
    out = {}
    for key, sym in _INDEX_QUOTE.items():
        q = quotes.get(sym) or {}
        last, ohlc = q.get("last_price"), q.get("ohlc") or {}
        prev = ohlc.get("close")
        if not last or not prev:
            out[key] = {"last": last, "prev_close": prev, "change": None, "change_pct": None, "direction": "FLAT"}
            continue
        chg = round(last - prev, 2)
        pct = round(chg / prev * 100, 2)
        out[key] = {
            "last": last, "prev_close": prev, "change": chg, "change_pct": pct,
            "open": ohlc.get("open"), "high": ohlc.get("high"), "low": ohlc.get("low"),
            # within +/-0.15% counts as flat: too small to call a direction
            "direction": "UP" if pct > 0.15 else "DOWN" if pct < -0.15 else "FLAT",
        }
    return out


@app.get("/market/indices", dependencies=auth)
def market_indices():
    return _kite_errors(lambda: index_moves(kite.kite.quote(list(_INDEX_QUOTE.values()))))


@app.get("/historical", dependencies=auth)
def historical(exchange: str, tradingsymbol: str, interval: str, from_date: str, to_date: str):
    def go():
        inst = kite.find_instrument(exchange, tradingsymbol)
        return kite.historical(int(inst["instrument_token"]), interval, _dt(from_date), _dt(to_date, end=True))
    return _kite_errors(go)


def _dt(s: str, end: bool = False) -> datetime:
    d = datetime.fromisoformat(s)
    if len(s) <= 10 and end:
        d = d.replace(hour=23, minute=59, second=59)
    return d


# ------------------------------------------------------------------ strategies & backtest


@app.get("/strategies", dependencies=auth)
def strategies():
    return [s.to_dict() for s in REGISTRY.values()]


class BacktestRequest(BaseModel):
    exchange: str = "NSE"
    tradingsymbol: str
    interval: str = "5minute"
    from_date: str
    to_date: str
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    initial_capital: float = 100_000
    sizing: str = "pct_capital"
    pct_capital: float = 100
    quantity: int = 1
    product: str = "MIS"
    allow_short: bool = True
    stop_loss_pct: float | None = None
    target_pct: float | None = None
    slippage_bps: float = 5
    squareoff_time: str = "15:10"
    no_entry_after: str = "14:45"
    save: bool = True


@app.post("/backtest", dependencies=auth)
def backtest(req: BacktestRequest):
    def go():
        inst = kite.find_instrument(req.exchange, req.tradingsymbol)
        start, end = _dt(req.from_date), _dt(req.to_date, end=True)
        if end <= start:
            raise ValueError("to_date must be after from_date")
        candles = kite.historical(int(inst["instrument_token"]), req.interval, start, end)
        if len(candles) < 30:
            raise ValueError(f"Only {len(candles)} candles found; widen the date range")
        known = {f.name for f in fields(BacktestConfig)}
        cfg = BacktestConfig(**{k: v for k, v in req.model_dump().items() if k in known})
        cfg.exchange = "BSE" if req.exchange.upper() in ("BSE", "BFO") else "NSE"
        cfg.lot_size = int(inst.get("lot_size") or 1)
        cfg.segment = segment_for(req.product, inst.get("instrument_type"))
        if cfg.segment in ("futures", "options") and req.product == "CNC":
            raise ValueError("Use NRML or MIS for F&O")
        if cfg.sizing == "fixed_qty" and cfg.quantity % cfg.lot_size:
            raise ValueError(f"Quantity must be a multiple of lot size {cfg.lot_size}")
        result = run_backtest(candles, cfg)
        result["instrument"] = {k: inst.get(k) for k in ("exchange", "tradingsymbol", "name", "instrument_type", "lot_size")}
        result["interval"] = req.interval
        if req.save:
            title = f"{REGISTRY[req.strategy].name} · {req.tradingsymbol} · {req.interval}"
            result["id"] = store.save_backtest(title, result)
        return result
    return _kite_errors(go)


class AnalyzeRequest(BaseModel):
    exchange: str = "NSE"
    tradingsymbol: str
    interval: str = "15minute"
    from_date: str
    to_date: str
    product: str = "MIS"
    initial_capital: float = 100_000
    allow_short: bool = True
    stop_loss_pct: float | None = None
    target_pct: float | None = None
    slippage_bps: float = 5
    find: bool = True
    use_ai: bool = True
    save: bool = True  # keep in the AI analysis history


_INDEX_SPOT = {("NSE", "NIFTY 50"): "NIFTY", ("NSE", "NIFTY BANK"): "BANKNIFTY", ("BSE", "SENSEX"): "SENSEX"}
_INDEX_FUT = re.compile(r"^(NIFTY|BANKNIFTY|SENSEX)\d{2}[A-Z]{3}FUT$")


def index_key_for(exchange: str, tradingsymbol: str) -> str | None:
    """NIFTY / BANKNIFTY / SENSEX when the analysed instrument is that index (spot or future), else None."""
    ex, ts = exchange.upper(), tradingsymbol.upper()
    if (ex, ts) in _INDEX_SPOT:
        return _INDEX_SPOT[(ex, ts)]
    m = _INDEX_FUT.match(ts) if ex in ("NFO", "BFO") else None
    return m.group(1) if m else None


def option_side(action: str) -> str | None:
    """Strategy signal -> option to BUY: bullish = CE, bearish = PE, otherwise no trade."""
    return {"BUY": "CE", "HOLD LONG": "CE", "SELL": "PE", "HOLD SHORT": "PE"}.get(action)


@app.post("/analyze", dependencies=auth)
def analyze(req: AnalyzeRequest):
    """Technical snapshot + walk-forward strategy finder + written summary."""
    def go():
        inst = kite.find_instrument(req.exchange, req.tradingsymbol)
        start, end = _dt(req.from_date), _dt(req.to_date, end=True)
        candles = kite.historical(int(inst["instrument_token"]), req.interval, start, end)
        if len(candles) < 60:
            raise ValueError(f"Only {len(candles)} candles found; widen the date range")
        snap = technical_snapshot(candles)
        finder = None
        if req.find:
            product = req.product if not (req.product == "MIS" and req.interval == "day") else "CNC"
            base = BacktestConfig(
                strategy="sma_cross", initial_capital=req.initial_capital, product=product,
                segment=segment_for(product, inst.get("instrument_type")),
                exchange="BSE" if req.exchange.upper() in ("BSE", "BFO") else "NSE",
                lot_size=int(inst.get("lot_size") or 1), allow_short=req.allow_short,
                stop_loss_pct=req.stop_loss_pct, target_pct=req.target_pct, slippage_bps=req.slippage_bps,
            )
            if base.segment in ("futures", "options") and product == "CNC":
                base.product, base.segment = "NRML", segment_for("NRML", inst.get("instrument_type"))
            finder = find_strategies(candles, base)
            finder["product"] = base.product
        summary, source = rule_based_summary(req.tradingsymbol, snap, finder), "rules"
        ai_error = None
        if req.use_ai and ai.enabled():
            try:
                summary, source = ai.ai_summary(req.tradingsymbol, req.interval, snap, finder), "claude"
            except Exception as e:  # fall back to the rule-based text
                ai_error = f"{type(e).__name__}: {e}"[:200]
        idx = index_key_for(req.exchange, req.tradingsymbol)
        if finder and idx:
            for row in finder.get("ranking", []):
                row["option_type"] = option_side(row.get("now", {}).get("action", ""))
        out = {
            "instrument": {k: inst.get(k) for k in ("exchange", "tradingsymbol", "name", "instrument_type", "lot_size")},
            "interval": req.interval,
            "product": req.product,
            "allow_short": req.allow_short,
            "snapshot": snap,
            "finder": finder,
            "summary": summary,
            "summary_source": source,
            "ai_available": ai.enabled(),
            "ai_error": ai_error,
            "index_key": idx,
            "forecast": forecast(finder["ranking"], snap, req.interval) if finder else None,
            "id": None,
            "created": None,
        }
        if req.save:
            top = (finder or {}).get("ranking") or [{}]
            meta = {"interval": req.interval, "verdict": snap.get("verdict"), "score": snap.get("score"),
                    "top": top[0].get("name"), "top_action": (top[0].get("now") or {}).get("action"), "index_key": idx}
            out["id"], out["created"] = store.save_analysis(f"{req.exchange}:{req.tradingsymbol}", meta, out)
        return out
    return _kite_errors(go)


@app.get("/analyses", dependencies=auth)
def analyses():
    return store.list_analyses()


@app.get("/analyses/{aid}", dependencies=auth)
def analysis_get(aid: str):
    r = store.get_analysis(aid)
    if r is None:
        raise HTTPException(404, "Not found")
    return {**r, "id": aid}


@app.delete("/analyses/{aid}", dependencies=auth)
def analysis_delete(aid: str):
    store.delete_analysis(aid)
    return {"ok": True}


@app.delete("/analyses", dependencies=auth)
def analyses_clear():
    return {"ok": True, "deleted": store.delete_analysis(None)}


@app.get("/backtests", dependencies=auth)
def backtests():
    return store.list_backtests()


@app.get("/backtests/{bid}", dependencies=auth)
def backtest_get(bid: str):
    r = store.get_backtest(bid)
    if r is None:
        raise HTTPException(404, "Not found")
    return r


@app.delete("/backtests/{bid}", dependencies=auth)
def backtest_delete(bid: str):
    store.delete_backtest(bid)
    return {"ok": True}


class ChargesRequest(BaseModel):
    segment: str
    buy_value: float
    sell_value: float
    exchange: str = "NSE"


@app.post("/charges", dependencies=auth)
def charges(req: ChargesRequest):
    return _kite_errors(lambda: round_trip_charges(req.segment, req.buy_value, req.sell_value, req.exchange))


# ------------------------------------------------------------------ sessions (paper / live / replay)


class SessionRequest(BaseModel):
    mode: str = "paper"
    exchange: str = "NSE"
    tradingsymbol: str
    interval: str = "5minute"
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    quantity: int = 1
    product: str = "MIS"
    allow_short: bool = True
    stop_loss_pct: float | None = None
    target_pct: float | None = None
    slippage_bps: float = 5
    squareoff_time: str = "15:10"
    no_entry_after: str = "14:45"
    max_trades_per_day: int = 10
    max_daily_loss: float = 1000
    replay_from: str | None = None
    replay_to: str | None = None
    replay_speed: float = 0.0
    confirm_live: str = ""


@app.post("/sessions", dependencies=auth)
def start_session(req: SessionRequest):
    def go():
        inst = kite.find_instrument(req.exchange, req.tradingsymbol)
        known = {f.name for f in fields(SessionConfig)}
        data = {k: v for k, v in req.model_dump().items() if k in known}
        data.update(
            instrument_token=int(inst["instrument_token"]),
            exchange=inst["exchange"],
            tradingsymbol=inst["tradingsymbol"],
            lot_size=int(inst.get("lot_size") or 1),
            segment=segment_for(req.product, inst.get("instrument_type")),
        )
        cfg = SessionConfig(**data)
        s = manager.start(
            cfg,
            _dt(req.replay_from) if req.replay_from else None,
            _dt(req.replay_to, end=True) if req.replay_to else None,
            req.confirm_live,
        )
        return s.snapshot()
    return _kite_errors(go)


@app.get("/sessions", dependencies=auth)
def list_sessions():
    return {"active": manager.list(), "history": store.list_sessions()}


@app.delete("/sessions/history", dependencies=auth)
def clear_sessions_history(mode: str = "paper"):
    """Clear finished strategy sessions (paper also clears replay). Running sessions are kept."""
    modes = ("paper", "replay") if mode == "paper" else (mode,)
    running = {s["id"] for s in manager.list() if s["status"] in ("running", "halted")}
    manager.forget_finished(modes)
    return {"ok": True, "deleted": store.delete_sessions(modes, running)}


@app.get("/sessions/{sid}", dependencies=auth)
def get_session(sid: str):
    return _kite_errors(lambda: manager.get(sid).snapshot())


@app.get("/sessions/{sid}/events", dependencies=auth)
def session_events(sid: str, limit: int = 200):
    return store.events(sid, limit)


@app.post("/sessions/{sid}/stop", dependencies=auth)
def stop_session(sid: str, square_off: bool = True):
    return _kite_errors(lambda: manager.stop(sid, square_off).snapshot())


@app.post("/kill", dependencies=auth)
def kill_switch():
    """Emergency stop: halts every session and squares off positions they opened."""
    return {"stopped": manager.kill_all(), "options": options_manager.kill()}


# ------------------------------------------------------------------ account (read only)


@app.get("/account/margins", dependencies=auth)
def margins():
    return _kite_errors(lambda: kite.kite.margins())


@app.get("/account/positions", dependencies=auth)
def positions():
    return _kite_errors(lambda: kite.kite.positions())


@app.get("/account/orders", dependencies=auth)
def orders():
    return _kite_errors(lambda: kite.kite.orders())


@app.get("/account/day", dependencies=auth)
def account_day(day: str | None = None):
    """Today's orders, fills and positions from Zerodha (Kite app + Kite Algo) with charges; a past day from the saved copy."""
    from . import account
    today = datetime.now().date().isoformat()
    if day and day != today:
        try:
            d = account.load_day(settings.data_dir, day)
        except ValueError:
            raise HTTPException(400, "day must be YYYY-MM-DD")
        if d is None:
            raise HTTPException(404, f"No saved copy for {day} — Zerodha's API only gives today; older days are in Console")
        return d
    d = _kite_errors(lambda: account.day_summary(kite.kite, settings.order_tag))
    try:
        if datetime.now().strftime("%H:%M") >= "15:30":
            account.save_day(settings.data_dir, d)
    except Exception:
        pass
    return d


@app.get("/account/days", dependencies=auth)
def account_days():
    from . import account
    return account.saved_days(settings.data_dir)


@app.get("/account/profile", dependencies=auth)
def profile():
    return _kite_errors(lambda: kite.kite.profile())


@app.post("/postback")
def postback(payload: dict):
    """Optional: set https://<server>/postback as the Kite app's Postback URL.
    Kite signs postbacks with checksum = SHA-256(order_id + order_timestamp + api_secret)."""
    import hashlib

    expect = hashlib.sha256(
        f"{payload.get('order_id')}{payload.get('order_timestamp')}{settings.kite_api_secret}".encode()
    ).hexdigest()
    if not hmac.compare_digest(str(payload.get("checksum", "")), expect):
        raise HTTPException(403, "bad checksum")
    for s in list(manager.sessions.values()):
        s.on_order_update(payload)
    options_manager.on_order_update(payload)
    return {"ok": True}


# ------------------------------------------------------------------ index options, Auto Trade
from .options_api import register as _register_options  # noqa: E402

options_manager = _register_options(app, auth, kite, store, manager.hub, _kite_errors, _dt)
try:
    if kite.session_info():
        options_manager.reconcile_all()
        options_manager.hub_refresh(force=True)  # resume price monitoring of restored positions
except Exception:
    pass
