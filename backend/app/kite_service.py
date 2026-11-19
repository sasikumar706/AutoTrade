"""Everything that talks to Zerodha: login session, instruments, historical data, quotes.

The api_secret and access_token live ONLY on this server. The mobile app never sees them.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .config import settings
from .store import Store

IST = ZoneInfo("Asia/Kolkata")

# Max calendar days per historical request. Zerodha's published table (minute 60 days,
# 3/5/10minute 100, 15/30minute 200, 60minute 400, day 2000). Minute is kept lower
# because some users report errors near the documented limit.
CHUNK_DAYS = {
    "minute": 30,
    "3minute": 90,
    "5minute": 90,
    "10minute": 90,
    "15minute": 180,
    "30minute": 180,
    "60minute": 365,
    "day": 1900,
}
HISTORICAL_GAP = 0.35  # seconds between calls -> stays under 3 req/sec


def now_ist() -> datetime:
    return datetime.now(IST)


def token_expiry(login_time: datetime) -> datetime:
    """Kite access tokens expire at 06:00 IST the next morning."""
    nxt = (login_time + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
    if login_time.hour < 6:
        nxt = login_time.replace(hour=6, minute=0, second=0, microsecond=0)
    return nxt


class NotLoggedIn(Exception):
    pass


class KiteService:
    def __init__(self, store: Store):
        self.store = store
        self._kite = None
        self._lock = threading.Lock()
        self._last_hist_call = 0.0
        self._instruments: pd.DataFrame | None = None
        self._instruments_day: date | None = None

    # ---------- session ----------
    def _new_client(self):
        from kiteconnect import KiteConnect  # imported lazily so tests run without it

        if not settings.kite_api_key:
            raise RuntimeError("KITE_API_KEY is not set on the server")
        return KiteConnect(api_key=settings.kite_api_key)

    def login_url(self) -> str:
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={settings.kite_api_key}"

    def complete_login(self, request_token: str) -> dict:
        """Exchange request_token -> access_token (checksum computed by the SDK:
        SHA-256(api_key + request_token + api_secret))."""
        kite = self._new_client()
        data = kite.generate_session(request_token, api_secret=settings.kite_api_secret)
        kite.set_access_token(data["access_token"])
        info = {
            "access_token": data["access_token"],
            "user_id": data.get("user_id"),
            "user_name": data.get("user_name"),
            "login_time": now_ist().isoformat(),
        }
        self.store.set("kite_session", info)
        with self._lock:
            self._kite = kite
        return {k: v for k, v in info.items() if k != "access_token"}

    def export_session(self) -> dict:
        """Today's session for another server of the SAME Kite app (one login serves phone + PC)."""
        info = self.store.get("kite_session")
        if not info or self.session_info() is None:
            raise NotLoggedIn("Kite session missing or expired. Log in from the app.")
        return {k: info[k] for k in ("access_token", "user_id", "user_name", "login_time")}

    def import_session(self, access_token: str, login_time: str | None = None) -> dict:
        """Adopt a session created on another server: verified with Kite before it is stored."""
        kite = self._new_client()
        kite.set_access_token(access_token)
        prof = kite.profile()  # raises if the token is invalid or belongs to another Kite app
        info = {"access_token": access_token, "user_id": prof.get("user_id"), "user_name": prof.get("user_name"),
                "login_time": login_time or now_ist().isoformat()}
        self.store.set("kite_session", info)
        with self._lock:
            self._kite = kite
        return {k: v for k, v in info.items() if k != "access_token"}

    def session_info(self) -> dict | None:
        info = self.store.get("kite_session")
        if not info:
            return None
        login = datetime.fromisoformat(info["login_time"])
        exp = token_expiry(login)
        if now_ist() >= exp:
            return None
        return {"user_id": info["user_id"], "user_name": info["user_name"], "login_time": info["login_time"], "expires": exp.isoformat()}

    def access_token(self) -> str:
        info = self.store.get("kite_session")
        if not info or self.session_info() is None:
            raise NotLoggedIn("Kite session missing or expired. Log in from the app.")
        return info["access_token"]

    @property
    def kite(self):
        token = self.access_token()
        with self._lock:
            if self._kite is None or getattr(self._kite, "access_token", None) != token:
                k = self._new_client()
                k.set_access_token(token)
                self._kite = k
            return self._kite

    def logout(self) -> None:
        try:
            self.kite.invalidate_access_token()
        except Exception:
            pass
        self.store.delete("kite_session")
        with self._lock:
            self._kite = None

    # ---------- instruments ----------
    def instruments(self) -> pd.DataFrame:
        """Instrument master (NSE, BSE, NFO, BFO), refreshed once a day. Tokens can be
        reused after F&O expiry, so always key on exchange + tradingsymbol."""
        today = now_ist().date()
        if self._instruments is not None and self._instruments_day == today:
            return self._instruments
        cache = settings.data_dir / f"instruments_{today.isoformat()}.csv"
        if cache.exists():
            df = pd.read_csv(cache)
        else:
            frames = []
            for ex in ("NSE", "BSE", "NFO", "BFO"):
                try:
                    frames.append(pd.DataFrame(self.kite.instruments(ex)))
                except Exception:
                    if ex in ("NSE",):
                        raise
            df = pd.concat(frames, ignore_index=True)
            df.to_csv(cache, index=False)
            for old in settings.data_dir.glob("instruments_*.csv"):
                if old != cache:
                    old.unlink(missing_ok=True)
        df["tradingsymbol"] = df["tradingsymbol"].astype(str)
        df["name"] = df["name"].fillna("").astype(str)
        self._instruments, self._instruments_day = df, today
        return df

    def search_instruments(self, q: str, exchange: str | None = None, limit: int = 30) -> list[dict]:
        df = self.instruments()
        q = q.strip().upper()
        if exchange:
            df = df[df["exchange"] == exchange.upper()]
        if not q:
            return []
        exact = df[df["tradingsymbol"] == q]
        starts = df[df["tradingsymbol"].str.startswith(q)]
        named = df[df["name"].str.upper().str.contains(q, regex=False)]
        res = pd.concat([exact, starts, named]).drop_duplicates(["exchange", "tradingsymbol"]).head(limit)
        cols = ["instrument_token", "exchange", "tradingsymbol", "name", "instrument_type", "segment", "expiry", "strike", "lot_size", "tick_size"]
        res = res[[c for c in cols if c in res.columns]].copy()
        return json.loads(res.to_json(orient="records", date_format="iso"))

    def find_instrument(self, exchange: str, tradingsymbol: str) -> dict:
        df = self.instruments()
        row = df[(df["exchange"] == exchange.upper()) & (df["tradingsymbol"] == tradingsymbol.upper())]
        if row.empty:
            raise ValueError(f"{exchange}:{tradingsymbol} not found in today's instrument list")
        return json.loads(row.iloc[[0]].to_json(orient="records", date_format="iso"))[0]

    # ---------- historical ----------
    def _throttle(self):
        wait = HISTORICAL_GAP - (time.monotonic() - self._last_hist_call)
        if wait > 0:
            time.sleep(wait)
        self._last_hist_call = time.monotonic()

    def historical(self, instrument_token: int, interval: str, start: datetime, end: datetime,
                   continuous: bool = False, oi: bool = False) -> list[dict]:
        if interval not in CHUNK_DAYS:
            raise ValueError(f"interval must be one of {list(CHUNK_DAYS)}")
        key = hashlib.sha1(f"{instrument_token}|{interval}|{start}|{end}|{continuous}|{oi}".encode()).hexdigest()
        cache = settings.data_dir / "cache" / f"{key}.json"
        # cache only fully-past ranges (today's candles are still changing)
        cacheable = end.date() < now_ist().date()
        if cacheable and cache.exists():
            return json.loads(cache.read_text())
        out: list[dict] = []
        cur = start
        step = timedelta(days=CHUNK_DAYS[interval])
        while cur <= end:
            chunk_end = min(cur + step, end)
            self._throttle()
            for attempt in range(3):
                try:
                    rows = self.kite.historical_data(instrument_token, cur, chunk_end, interval, continuous, oi)
                    break
                except Exception as e:  # rate-limit or network hiccup -> back off
                    if attempt == 2 or "Too many requests" not in str(e) and "timed out" not in str(e):
                        raise
                    time.sleep(1.5 * (attempt + 1))
            out.extend(rows)
            cur = chunk_end + timedelta(seconds=1)
        for r in out:
            d = r["date"]
            r["date"] = d.astimezone(IST).replace(tzinfo=None).isoformat() if isinstance(d, datetime) else str(d)
        seen, dedup = set(), []
        for r in out:
            if r["date"] not in seen:
                seen.add(r["date"])
                dedup.append(r)
        if cacheable:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(dedup))
        return dedup

    def ltp(self, symbols: list[str]) -> dict:
        return self.kite.ltp(symbols)
