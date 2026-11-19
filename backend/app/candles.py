"""Build OHLCV candles from live ticks (used by paper and live sessions)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

INTERVAL_MINUTES = {
    "minute": 1,
    "3minute": 3,
    "5minute": 5,
    "10minute": 10,
    "15minute": 15,
    "30minute": 30,
    "60minute": 60,
}

MARKET_OPEN = (9, 15)


def bucket_start(ts: datetime, minutes: int) -> datetime:
    """Kite aligns intraday candles to 09:15, so a 60minute candle runs 09:15-10:15, etc."""
    open_dt = ts.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    if ts < open_dt:
        return ts.replace(second=0, microsecond=0)
    elapsed = int((ts - open_dt).total_seconds() // 60)
    return open_dt + timedelta(minutes=(elapsed // minutes) * minutes)


@dataclass
class Candle:
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def as_dict(self):
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class CandleBuilder:
    """Feed ticks with `update`; it returns the finished candle when a new bucket starts.

    Volume is taken from the cumulative day volume in the tick (volume_traded) when
    available, so the candle volume = difference between first and last tick of the bucket.
    """

    def __init__(self, interval: str):
        if interval not in INTERVAL_MINUTES:
            raise ValueError(f"Live sessions support intraday intervals only: {list(INTERVAL_MINUTES)}")
        self.minutes = INTERVAL_MINUTES[interval]
        self.current: Candle | None = None
        self._vol_at_start: float | None = None
        self._last_cum_vol: float | None = None

    def update(self, ts: datetime, price: float, cum_volume: float | None = None) -> Candle | None:
        start = bucket_start(ts, self.minutes)
        finished = None
        if self.current is not None and start > self.current.date:
            finished = self.current
            self.current = None
        if self.current is None:
            self.current = Candle(start, price, price, price, price, 0.0)
            self._vol_at_start = self._last_cum_vol if self._last_cum_vol is not None else cum_volume
        else:
            c = self.current
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
        if cum_volume is not None:
            self._last_cum_vol = cum_volume
            if self._vol_at_start is not None:
                self.current.volume = max(0.0, cum_volume - self._vol_at_start)
        return finished

    def flush_if_due(self, now: datetime) -> Candle | None:
        """Close the running candle if its time window has passed (no tick arrived)."""
        if self.current is None:
            return None
        if now >= self.current.date + timedelta(minutes=self.minutes):
            done, self.current = self.current, None
            return done
        return None
