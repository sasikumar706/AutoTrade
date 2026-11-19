"""Creates and tracks paper / live / replay sessions."""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import settings
from .engine import LiveExecutor, PaperExecutor, ReplayFeed, SessionConfig, TickerHub, TradingSession
from .kite_service import KiteService
from .store import Store
from .strategies import get_strategy


from contextlib import nullcontext as _nullctx


class SessionManager:
    def __init__(self, kite: KiteService, store: Store):
        self.kite, self.store = kite, store
        self.sessions: dict[str, TradingSession] = {}
        self.hub = TickerHub(lambda: settings.kite_api_key, kite.access_token)

    def _log(self, sid: str, kind: str, data: dict):
        self.store.log_event(sid, kind, data)
        s = self.sessions.get(sid)
        if s is not None and kind in ("trade", "order_failed", "risk"):
            self.store.upsert_session(sid, s.snapshot()["config"], s.status, self._summary(s))

    @staticmethod
    def _summary(s: TradingSession) -> dict:
        return {"realized_pnl": round(s.realized, 2), "charges": round(s.charges, 2), "trades": len(s.trades), "message": s.message}

    def _seed(self, cfg: SessionConfig, end: datetime) -> list[dict]:
        strat = get_strategy(cfg.strategy)
        bars = strat.warmup(strat.resolve(cfg.params))
        minutes = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}[cfg.interval]
        trading_days = max(3, int(bars * minutes / 375) + 2)
        start = end - timedelta(days=int(trading_days * 1.6) + 4)
        rows = self.kite.historical(cfg.instrument_token, cfg.interval, start, end)
        return [r for r in rows if datetime.fromisoformat(r["date"]) < end]

    def start(self, cfg: SessionConfig, replay_from: datetime | None = None, replay_to: datetime | None = None,
              confirm_live: str = "") -> TradingSession:
        cfg.validate()
        if cfg.mode == "live":
            if confirm_live != "I UNDERSTAND REAL MONEY":
                raise PermissionError("Live mode needs confirm_live = 'I UNDERSTAND REAL MONEY'")
            if any(s.cfg.mode == "live" and s.status == "running" and s.cfg.tradingsymbol == cfg.tradingsymbol
                   for s in self.sessions.values()):
                raise PermissionError("A live session is already running on this instrument")

        if cfg.mode == "replay":
            if not (replay_from and replay_to):
                raise ValueError("Replay needs replay_from and replay_to")
            seed = self._seed(cfg, replay_from)
            candles = self.kite.historical(cfg.instrument_token, cfg.interval, replay_from, replay_to)
            if not candles:
                raise ValueError("No candles in the replay range (holiday/weekend?)")
            s = TradingSession(cfg, seed, PaperExecutor(cfg.slippage_bps), on_event=self._log)
            self.sessions[s.id] = s
            self.store.upsert_session(s.id, s.snapshot()["config"], s.status, self._summary(s))
            ReplayFeed(s, candles, on_done=lambda x: self.store.upsert_session(x.id, x.snapshot()["config"], x.status, self._summary(x))).start()
            return s

        seed = self._seed(cfg, datetime.now())
        executor = PaperExecutor(cfg.slippage_bps) if cfg.mode == "paper" else LiveExecutor(lambda: self.kite.kite)
        s = TradingSession(cfg, seed, executor, on_event=self._log)
        # drop the still-forming candle from the seed; the builder will rebuild it from ticks
        if len(s.history):
            last = s.history["date"].iloc[-1].to_pydatetime()
            if s.builder.minutes and last + timedelta(minutes=s.builder.minutes) > datetime.now():
                s.history = s.history.iloc[:-1]
        self.sessions[s.id] = s
        self.store.upsert_session(s.id, s.snapshot()["config"], s.status, self._summary(s))
        self.hub.add(s)
        return s

    def get(self, sid: str) -> TradingSession:
        if sid not in self.sessions:
            raise KeyError("Session not found (sessions live in memory; a server restart clears them)")
        return self.sessions[sid]

    def stop(self, sid: str, square_off: bool = True) -> TradingSession:
        s = self.get(sid)
        s.stop_session(square_off)
        self.store.upsert_session(s.id, s.snapshot()["config"], s.status, self._summary(s))
        self.hub.remove_unused()
        return s

    def kill_all(self) -> list[str]:
        """Emergency: stop every session and square off their open positions."""
        stopped = []
        for s in list(self.sessions.values()):
            if s.status in ("running", "halted"):
                s.status = "running" if s.status == "halted" else s.status
                s.stop_session(True)
                stopped.append(s.id)
        self.hub.remove_unused()
        return stopped

    def forget_finished(self, modes: tuple[str, ...]) -> None:
        """Drop stopped/finished sessions of these modes from memory (running/halted ones stay)."""
        with self.lock if hasattr(self, "lock") else _nullctx():
            for sid, sess in list(self.sessions.items()):
                if sess.cfg.mode in modes and sess.status not in ("running", "halted"):
                    del self.sessions[sid]

    def list(self) -> list[dict]:
        out = []
        for s in self.sessions.values():
            snap = s.snapshot()
            out.append({k: snap[k] for k in ("id", "status", "message", "config", "started", "ltp", "position", "net_pnl", "realized_pnl", "trade_count")})
        return sorted(out, key=lambda x: x["started"], reverse=True)

    def ticker_status(self) -> dict:
        return {"connected": self.hub.connected, "last_error": self.hub.last_error, "started": self.hub.ticker is not None}
