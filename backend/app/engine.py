"""Forward-testing engine: paper trading, real (live) trading and offline replay.

One TradingSession = one strategy on one instrument. Ticks come from the Kite WebSocket
(TickerHub) or from a ReplayFeed (historical candles turned into ticks, for testing when
the market is closed). Candles are built from ticks; when a candle closes the strategy
is evaluated exactly like in the backtest, and the resulting order is executed on the
next tick (= the next candle's open), so backtest and forward test behave the same way.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Any, Callable

import pandas as pd

from .candles import CandleBuilder
from .charges import round_trip_charges
from .config import settings
from .strategies import get_strategy

# --------------------------------------------------------------------------- config


@dataclass
class SessionConfig:
    mode: str  # "paper" | "live" | "replay"
    strategy: str
    exchange: str
    tradingsymbol: str
    instrument_token: int
    interval: str = "5minute"
    params: dict[str, Any] = field(default_factory=dict)
    quantity: int = 1
    lot_size: int = 1
    product: str = "MIS"
    segment: str = "equity_intraday"
    allow_short: bool = True
    stop_loss_pct: float | None = None
    target_pct: float | None = None
    slippage_bps: float = 5.0  # paper/replay fills only
    squareoff_time: str = "15:10"
    no_entry_after: str = "14:45"
    max_trades_per_day: int = 10
    max_daily_loss: float = 1000.0  # rupees, net of charges; session halts when hit
    replay_speed: float = 0.0  # seconds to sleep per replayed bar (0 = as fast as possible)

    def validate(self):
        if self.mode not in ("paper", "live", "replay"):
            raise ValueError("mode must be paper, live or replay")
        if self.quantity <= 0 or self.quantity % self.lot_size:
            raise ValueError("quantity must be a positive multiple of lot size")
        if self.product == "CNC":
            self.allow_short = False
        if self.mode == "live":
            if not settings.live_trading_enabled:
                raise PermissionError("Live trading is disabled on the server (set LIVE_TRADING_ENABLED=true)")
            if self.quantity > settings.max_live_qty:
                raise PermissionError(f"Quantity above server limit MAX_LIVE_QTY={settings.max_live_qty}")
            self.max_daily_loss = min(self.max_daily_loss, settings.max_live_daily_loss)
            self.max_trades_per_day = min(self.max_trades_per_day, settings.max_live_orders_per_day // 2)
        get_strategy(self.strategy).resolve(self.params)


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


# --------------------------------------------------------------------------- executors


class PaperExecutor:
    """Fills instantly at the tick price, with slippage against you."""

    def __init__(self, slippage_bps: float):
        self.slip = slippage_bps / 10_000

    def submit(self, session: "TradingSession", side: int, qty: int, price: float, purpose: str):
        fill = price * (1 + self.slip * side)
        session.on_fill(purpose, side, qty, fill, order_id=f"paper-{uuid.uuid4().hex[:8]}")


class LiveExecutor:
    """Places real MARKET orders through Kite. Fills are confirmed by order updates.

    Since 1 Apr 2026 MARKET orders via API need market_protection (non-zero); -1 lets
    Zerodha apply its automatic protection. Orders are only accepted from the static IP
    registered in the Kite developer console, so this must run on that server.
    """

    def __init__(self, kite_getter: Callable[[], Any]):
        self.kite_getter = kite_getter

    def submit(self, session: "TradingSession", side: int, qty: int, price: float, purpose: str):
        kite = self.kite_getter()
        cfg = session.cfg
        params = dict(
            variety=kite.VARIETY_REGULAR,
            exchange=cfg.exchange,
            tradingsymbol=cfg.tradingsymbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY if side > 0 else kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=cfg.product,
            order_type=kite.ORDER_TYPE_MARKET,
            validity=kite.VALIDITY_DAY,
            tag=settings.order_tag,
            market_protection=-1,
        )
        order_id = kite.place_order(**params)
        session.pending_orders[str(order_id)] = {"purpose": purpose, "side": side, "qty": qty, "sent": time.time()}
        session.event("order_sent", {"order_id": order_id, "purpose": purpose, "side": side, "qty": qty, "ref_price": price})


# --------------------------------------------------------------------------- session


class TradingSession:
    def __init__(self, cfg: SessionConfig, seed_candles: list[dict], executor, on_event=None):
        cfg.validate()
        self.id = uuid.uuid4().hex[:10]
        self.cfg = cfg
        self.strategy = get_strategy(cfg.strategy)
        self.params = self.strategy.resolve(cfg.params)
        self.executor = executor
        self._on_event = on_event
        self.lock = threading.RLock()
        self.builder = CandleBuilder(cfg.interval)
        self.history = pd.DataFrame(seed_candles or [], columns=["date", "open", "high", "low", "close", "volume"])
        if len(self.history):
            self.history["date"] = pd.to_datetime(self.history["date"])
        self.sq_t, self.ne_t = _hm(cfg.squareoff_time), _hm(cfg.no_entry_after)

        self.status = "running"  # running | stopped | halted | error | finished
        self.message = ""
        self.position = 0  # signed qty
        self.entry_price = 0.0
        self.entry_time: str | None = None
        self.stop: float | None = None
        self.target: float | None = None
        self.pending_action: str | None = None  # long | short | flat (from a candle signal)
        self.pending_orders: dict[str, dict] = {}
        self.pending_after_exit: str | None = None
        self.trades: list[dict] = []
        self.realized = 0.0
        self.charges = 0.0
        self.ltp: float | None = None
        self.last_tick: datetime | None = None
        self.trading_day = None
        self.day_trades = 0
        self.day_pnl = 0.0
        self.day_done = False
        self.candles_seen = 0
        self.started = datetime.now().isoformat()

    # ----- helpers
    def event(self, kind: str, data: dict):
        if self._on_event:
            try:
                self._on_event(self.id, kind, data)
            except Exception:
                pass

    @property
    def busy(self) -> bool:
        return bool(self.pending_orders)

    def unrealized(self) -> float:
        if not self.position or self.ltp is None:
            return 0.0
        return (self.ltp - self.entry_price) * self.position

    # ----- tick path
    def on_tick(self, ts: datetime, price: float, cum_volume: float | None = None):
        with self.lock:
            if self.status not in ("running",):
                return
            self.ltp, self.last_tick = price, ts
            if self.trading_day != ts.date():
                self.trading_day, self.day_trades, self.day_pnl, self.day_done = ts.date(), 0, 0.0, False

            finished = self.builder.update(ts, price, cum_volume)
            if finished is not None:
                self._on_candle(finished.as_dict(), ts)

            if self.busy:
                return
            # 1) queued signal -> execute at this tick (= next candle's open)
            if self.pending_action is not None:
                self._execute(self.pending_action, price, "signal")
                self.pending_action = None
            # 2) stop-loss / target on live price
            if self.position and not self.busy:
                long = self.position > 0
                if self.stop is not None and ((long and price <= self.stop) or (not long and price >= self.stop)):
                    self._exit(price, "stop_loss")
                elif self.target is not None and ((long and price >= self.target) or (not long and price <= self.target)):
                    self._exit(price, "target")
            # 3) daily loss limit (realized today + open P&L)
            if not self.day_done and self.day_pnl + self.unrealized() <= -abs(self.cfg.max_daily_loss):
                if self.position and not self.busy:
                    self._exit(price, "daily_loss_limit")
                self.day_done = True
                self.event("risk", {"msg": f"Daily loss limit {self.cfg.max_daily_loss} hit; no more trades today"})
            # 4) intraday square-off
            if self.cfg.product == "MIS" and ts.time() >= self.sq_t:
                if self.position and not self.busy:
                    self._exit(price, "squareoff")
                self.day_done = True

    def on_clock(self, now: datetime):
        """Called every few seconds so a candle closes even if no tick arrives."""
        with self.lock:
            if self.status != "running":
                return
            done = self.builder.flush_if_due(now)
            if done is not None:
                self._on_candle(done.as_dict(), now)

    def _on_candle(self, candle: dict, now: datetime):
        self.candles_seen += 1
        row = pd.DataFrame([candle])
        row["date"] = pd.to_datetime(row["date"])
        self.history = pd.concat([self.history, row], ignore_index=True).tail(3000).reset_index(drop=True)
        if len(self.history) < 3:
            return
        sig = self.strategy.signals(self.history, self.params).iloc[-1]
        want = None
        if self.position > 0 and sig["exit_long"]:
            want = "flat"
        if self.position < 0 and sig["exit_short"]:
            want = "flat"
        if sig["entry_long"] and self.position <= 0:
            want = "long"
        elif sig["entry_short"] and self.cfg.allow_short and self.position >= 0:
            want = "short"
        if want in ("long", "short"):
            blocked = (
                self.day_done
                or self.day_trades >= self.cfg.max_trades_per_day
                or (self.cfg.product == "MIS" and now.time() >= self.ne_t)
            )
            if blocked:
                want = "flat" if self.position else None
        self.event("candle", {"candle": {k: (str(v) if k == "date" else v) for k, v in candle.items()}, "signal": want})
        self.pending_action = want

    # ----- order path
    def _execute(self, action: str, price: float, reason: str):
        cur = "long" if self.position > 0 else "short" if self.position < 0 else "flat"
        if action == cur:
            return
        if self.position:
            self._exit(price, reason)
            if self.busy:  # live: wait for exit fill, then re-evaluate entry on next signal
                if action != "flat":
                    self.pending_after_exit = action
                return
        if action in ("long", "short"):
            self._enter(1 if action == "long" else -1, price, reason)

    def _enter(self, side: int, price: float, reason: str):
        self.day_trades += 1
        self._submit(side, self.cfg.quantity, price, f"entry:{reason}")

    def _exit(self, price: float, reason: str):
        side = -1 if self.position > 0 else 1
        self._submit(side, abs(self.position), price, f"exit:{reason}")

    def _submit(self, side: int, qty: int, price: float, purpose: str):
        """If the broker refuses the order request itself (IP not whitelisted, margin,
        RMS, network...), halt the session instead of retrying on every tick."""
        try:
            self.executor.submit(self, side, qty, price, purpose)
        except Exception as e:
            self.pending_action = None
            self.pending_after_exit = None
            self.status = "halted"
            what = "EXIT" if purpose.startswith("exit") else "Entry"
            self.message = f"{what} order refused: {type(e).__name__}: {e}"
            if purpose.startswith("exit"):
                self.message += " — CHECK YOUR POSITION IN KITE NOW!"
            self.event("order_failed", {"status": "REFUSED", "message": str(e), "purpose": purpose})

    def on_fill(self, purpose: str, side: int, qty: int, price: float, order_id: str):
        with self.lock:
            kind, reason = purpose.split(":", 1)
            if kind == "entry":
                self.position = side * qty
                self.entry_price = price
                self.entry_time = (self.last_tick or datetime.now()).isoformat()
                sl, tg = self.cfg.stop_loss_pct, self.cfg.target_pct
                self.stop = price * (1 - side * sl / 100) if sl else None
                self.target = price * (1 + side * tg / 100) if tg else None
                self.event("fill", {"order_id": order_id, "type": "entry", "side": side, "qty": qty, "price": round(price, 2), "reason": reason})
            else:
                pos_side = -side
                gross = (price - self.entry_price) * qty * pos_side
                buy_val = (self.entry_price if pos_side > 0 else price) * qty
                sell_val = (price if pos_side > 0 else self.entry_price) * qty
                ch = round_trip_charges(self.cfg.segment, buy_val, sell_val, self.cfg.exchange)["total"]
                net = gross - ch
                self.realized += net
                self.charges += ch
                self.day_pnl += net
                trade = {
                    "side": "LONG" if pos_side > 0 else "SHORT",
                    "qty": qty,
                    "entry_time": self.entry_time,
                    "entry_price": round(self.entry_price, 2),
                    "exit_time": (self.last_tick or datetime.now()).isoformat(),
                    "exit_price": round(price, 2),
                    "gross_pnl": round(gross, 2),
                    "charges": round(ch, 2),
                    "net_pnl": round(net, 2),
                    "exit_reason": reason,
                }
                self.trades.append(trade)
                self.position, self.stop, self.target = 0, None, None
                self.event("trade", trade)
                nxt = self.pending_after_exit
                if nxt:
                    self.pending_after_exit = None
                    if not self.day_done and self.ltp is not None:
                        self._enter(1 if nxt == "long" else -1, self.ltp, "signal")

    def on_order_update(self, data: dict):
        """Kite order update (WebSocket text message or postback)."""
        oid = str(data.get("order_id"))
        with self.lock:
            meta = self.pending_orders.get(oid)
            if not meta:
                return
            st = data.get("status")
            if st == "COMPLETE":
                self.pending_orders.pop(oid, None)
                price = float(data.get("average_price") or self.ltp or 0)
                qty = int(data.get("filled_quantity") or meta["qty"])
                self.on_fill(meta["purpose"], meta["side"], qty, price, oid)
            elif st in ("REJECTED", "CANCELLED"):
                self.pending_orders.pop(oid, None)
                msg = data.get("status_message") or st
                self.event("order_failed", {"order_id": oid, "status": st, "message": msg})
                if meta["purpose"].startswith("exit"):
                    # failing to exit is dangerous: stop the strategy and tell the user
                    self.status, self.message = "halted", f"EXIT ORDER {st}: {msg}. Check your position in Kite now!"
                else:
                    self.message = f"Entry order {st}: {msg}"

    # ----- control
    def stop_session(self, square_off: bool = True):
        with self.lock:
            if square_off and self.position and not self.busy and self.ltp is not None:
                self._exit(self.ltp, "manual_stop")
            self.status = "stopped" if self.status == "running" else self.status

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "status": self.status,
                "message": self.message,
                "config": asdict(self.cfg),
                "started": self.started,
                "ltp": self.ltp,
                "last_tick": self.last_tick.isoformat() if self.last_tick else None,
                "position": {
                    "qty": self.position,
                    "entry_price": round(self.entry_price, 2) if self.position else None,
                    "entry_time": self.entry_time if self.position else None,
                    "stop": round(self.stop, 2) if self.stop else None,
                    "target": round(self.target, 2) if self.target else None,
                    "unrealized": round(self.unrealized(), 2),
                },
                "pending_orders": list(self.pending_orders),
                "realized_pnl": round(self.realized, 2),
                "charges": round(self.charges, 2),
                "net_pnl": round(self.realized + self.unrealized(), 2),
                "trades": self.trades[-100:],
                "trade_count": len(self.trades),
                "candles_seen": self.candles_seen,
                "running_candle": self.builder.current.as_dict() if self.builder.current else None,
            }


# --------------------------------------------------------------------------- feeds


def candles_to_ticks(candles: list[dict]):
    """Turn each candle into 4 ticks inside the candle's time window: O, then the extreme
    nearer the open first (H for bullish-looking bars ... ), then the other extreme, then C."""
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    if len(df) > 1:
        step = (df["date"].diff().dropna().min()).to_pytimedelta()
    else:
        step = timedelta(minutes=1)
    step = min(step, timedelta(minutes=60))
    for r in df.itertuples(index=False):
        t0 = r.date.to_pydatetime()
        first, second = (r.low, r.high) if r.close >= r.open else (r.high, r.low)
        vol = getattr(r, "volume", 0) or 0
        yield t0 + step * 0.02, r.open, None
        yield t0 + step * 0.30, first, None
        yield t0 + step * 0.65, second, None
        yield t0 + step * 0.98, r.close, vol


class ReplayFeed(threading.Thread):
    def __init__(self, session: TradingSession, candles: list[dict], on_done=None):
        super().__init__(daemon=True)
        self.session, self.candles, self.on_done = session, candles, on_done

    def run(self):
        last_day, cum = None, 0.0
        for ts, px, vol in candles_to_ticks(self.candles):
            if self.session.status != "running":
                break
            if ts.date() != last_day:
                last_day, cum = ts.date(), 0.0
            if vol is not None:
                cum += vol
            self.session.on_tick(ts, float(px), cum)
            if vol is not None and self.session.cfg.replay_speed > 0:
                time.sleep(self.session.cfg.replay_speed)
        s = self.session
        with s.lock:
            if s.status == "running":
                if s.position and not s.busy and s.ltp is not None:
                    s._exit(s.ltp, "end_of_replay")
                s.status = "finished"
        if self.on_done:
            self.on_done(s)


class TickerHub:
    """One shared Kite WebSocket connection for all paper/live sessions."""

    def __init__(self, api_key_getter, token_getter):
        self.api_key_getter, self.token_getter = api_key_getter, token_getter
        self.ticker = None
        self.sessions: dict[str, TradingSession] = {}
        self.lock = threading.Lock()
        self.connected = False
        self.last_error = ""
        self._clock_started = False
        self.listeners: list = []  # objects with wanted_tokens/on_raw_tick/on_order_update/on_clock

    def _tokens(self) -> list[int]:
        toks = {s.cfg.instrument_token for s in self.sessions.values() if s.status == "running"}
        for l in self.listeners:
            toks.update(l.wanted_tokens())
        return sorted(toks)

    def ensure_started(self):
        if self.ticker is not None:
            return
        from kiteconnect import KiteTicker

        t = KiteTicker(self.api_key_getter(), self.token_getter(), reconnect=True, reconnect_max_tries=300)

        def on_connect(ws, response):
            self.connected = True
            toks = self._tokens()
            if toks:
                ws.subscribe(toks)
                ws.set_mode(ws.MODE_FULL, toks)

        def on_ticks(ws, ticks):
            for tk in ticks:
                tok = tk.get("instrument_token")
                ts = tk.get("exchange_timestamp") or tk.get("last_trade_time") or datetime.now()
                if getattr(ts, "tzinfo", None) is not None:
                    ts = ts.replace(tzinfo=None)
                price = tk.get("last_price")
                vol = tk.get("volume_traded")
                for s in list(self.sessions.values()):
                    if s.cfg.instrument_token == tok and price is not None:
                        s.on_tick(ts, float(price), vol)
                for l in list(self.listeners):
                    try:
                        l.on_raw_tick(tk)
                    except Exception as e:
                        self.last_error = f"listener: {e}"

        def on_order_update(ws, data):
            for s in list(self.sessions.values()):
                s.on_order_update(data)
            for l in list(self.listeners):
                l.on_order_update(data)

        def on_close(ws, code, reason):
            self.connected = False

        def on_error(ws, code, reason):
            self.last_error = f"{code}: {reason}"

        def on_reconnect(ws, attempts):
            self.connected = False

        t.on_connect, t.on_ticks, t.on_order_update = on_connect, on_ticks, on_order_update
        t.on_close, t.on_error, t.on_reconnect = on_close, on_error, on_reconnect
        t.connect(threaded=True)
        self.ticker = t
        if not self._clock_started:
            self._clock_started = True
            threading.Thread(target=self._clock, daemon=True).start()

    def _clock(self):
        while True:
            time.sleep(3)
            now = datetime.now()
            for s in list(self.sessions.values()):
                s.on_clock(now)
            # listeners (options manager) run their own clock; see main.py

    def add(self, s: TradingSession):
        with self.lock:
            self.sessions[s.id] = s
        self.ensure_started()
        if self.ticker is not None and self.connected:
            self.ticker.subscribe([s.cfg.instrument_token])
            self.ticker.set_mode(self.ticker.MODE_FULL, [s.cfg.instrument_token])

    def refresh_listener(self, listener):
        """Register a listener and (re)subscribe the tokens it currently needs."""
        if listener not in self.listeners:
            self.listeners.append(listener)
        toks = listener.wanted_tokens()
        if not toks:
            return
        self.ensure_started()
        if self.ticker is not None and self.connected:
            self.ticker.subscribe(toks)
            self.ticker.set_mode(self.ticker.MODE_FULL, toks)

    def remove_unused(self):
        if self.ticker is None or not self.connected:
            return
        live = set(self._tokens())
        stale = {s.cfg.instrument_token for s in self.sessions.values()} - live
        if stale:
            self.ticker.unsubscribe(list(stale))

    def restart(self):
        """Reconnect with the new access token after the daily login."""
        had = self.ticker is not None
        self.stop()
        if had and self._tokens():
            self.ensure_started()

    def stop(self):
        if self.ticker is not None:
            try:
                self.ticker.close()
            except Exception:
                pass
        self.ticker, self.connected = None, False
