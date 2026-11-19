"""Index options: NIFTY, BANKNIFTY, SENSEX.

Contracts, expiries, strikes and lot sizes always come from Kite's daily instrument list
(never hard-coded). Black-Scholes is used only for the "estimated premium" backtest mode.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

INDICES = {
    # key: underlying index (spot), options exchange, typical strike step, fallback spot token
    "NIFTY": {"spot_exchange": "NSE", "spot_symbol": "NIFTY 50", "spot_token": 256265,
              "opt_exchange": "NFO", "name": "NIFTY", "step": 50, "iv": 12.0,
              "expiry_rule": "weekly", "expiry_weekday": 1},  # Tuesday
    "BANKNIFTY": {"spot_exchange": "NSE", "spot_symbol": "NIFTY BANK", "spot_token": 260105,
                  "opt_exchange": "NFO", "name": "BANKNIFTY", "step": 100, "iv": 14.0,
                  "expiry_rule": "monthly", "expiry_weekday": 1},  # last Tuesday (weeklies discontinued 2025)
    "SENSEX": {"spot_exchange": "BSE", "spot_symbol": "SENSEX", "spot_token": 265,
               "opt_exchange": "BFO", "name": "SENSEX", "step": 100, "iv": 13.0,
               "expiry_rule": "weekly", "expiry_weekday": 3},  # Thursday
}


def index_cfg(index: str) -> dict:
    key = index.upper()
    if key not in INDICES:
        raise ValueError(f"Index must be one of {', '.join(INDICES)}")
    return INDICES[key]


# ------------------------------------------------------------------ instrument-list helpers


def _opts(instruments: pd.DataFrame, index: str) -> pd.DataFrame:
    c = index_cfg(index)
    df = instruments[(instruments["exchange"] == c["opt_exchange"]) & (instruments["name"] == c["name"])
                     & (instruments["instrument_type"].isin(["CE", "PE"]))].copy()
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["strike"] = df["strike"].astype(float)
    return df


def spot_instrument(instruments: pd.DataFrame, index: str) -> dict:
    c = index_cfg(index)
    row = instruments[(instruments["exchange"] == c["spot_exchange"]) & (instruments["tradingsymbol"] == c["spot_symbol"])]
    if not row.empty:
        r = row.iloc[0]
        return {"instrument_token": int(r["instrument_token"]), "exchange": c["spot_exchange"], "tradingsymbol": c["spot_symbol"]}
    return {"instrument_token": c["spot_token"], "exchange": c["spot_exchange"], "tradingsymbol": c["spot_symbol"]}


def nearest_future(instruments: pd.DataFrame, index: str, today: date) -> dict | None:
    c = index_cfg(index)
    df = instruments[(instruments["exchange"] == c["opt_exchange"]) & (instruments["name"] == c["name"])
                     & (instruments["instrument_type"] == "FUT")].copy()
    if df.empty:
        return None
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df = df[df["expiry"] >= today].sort_values("expiry")
    if df.empty:
        return None
    r = df.iloc[0]
    return {"instrument_token": int(r["instrument_token"]), "exchange": r["exchange"], "tradingsymbol": r["tradingsymbol"],
            "expiry": r["expiry"].isoformat(), "lot_size": int(r["lot_size"])}


def expiries(instruments: pd.DataFrame, index: str, today: date) -> list[str]:
    df = _opts(instruments, index)
    return sorted({d.isoformat() for d in df["expiry"] if d >= today})


def chain(instruments: pd.DataFrame, index: str, expiry: str) -> pd.DataFrame:
    df = _opts(instruments, index)
    df = df[df["expiry"] == date.fromisoformat(expiry)]
    if df.empty:
        raise ValueError(f"No {index} options listed for expiry {expiry}")
    return df.sort_values(["strike", "instrument_type"])


def classify(strike: float, spot: float, option_type: str, step: float) -> str:
    """ATM / ITM / OTM relative to spot (ATM = nearest strike)."""
    if abs(strike - spot) <= step / 2:
        return "ATM"
    if option_type == "CE":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def pick_strike(strikes: list[float], spot: float, option_type: str, method: str, offset: int) -> float:
    """method: ATM | ITM | OTM (offset = how many strikes away from ATM) | a strike number."""
    strikes = sorted(set(strikes))
    if not strikes:
        raise ValueError("No strikes available")
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    m = str(method).upper()
    if m == "ATM":
        i = atm_i
    elif m in ("ITM", "OTM"):
        n = max(int(offset), 1)
        # for CE, ITM = lower strikes; for PE, ITM = higher strikes
        towards_lower = (m == "ITM") == (option_type == "CE")
        i = atm_i - n if towards_lower else atm_i + n
    else:
        target = float(method)
        if target not in strikes:
            raise ValueError(f"Strike {target:g} is not listed for this expiry")
        return target
    if i < 0 or i >= len(strikes):
        raise ValueError("Requested strike is outside the listed range")
    return strikes[i]


@dataclass
class Contract:
    index: str
    exchange: str
    tradingsymbol: str
    instrument_token: int
    option_type: str
    strike: float
    expiry: str
    lot_size: int
    tick_size: float
    moneyness: str

    def to_dict(self):
        return self.__dict__.copy()


def select_contract(instruments: pd.DataFrame, index: str, expiry: str, option_type: str, spot: float,
                    method: str = "ATM", offset: int = 0) -> Contract:
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise ValueError("Option type must be CE or PE")
    ch = chain(instruments, index, expiry)
    side = ch[ch["instrument_type"] == option_type]
    strike = pick_strike(side["strike"].tolist(), spot, option_type, method, offset)
    r = side[side["strike"] == strike].iloc[0]
    lot = int(r["lot_size"])
    if lot <= 0:
        raise ValueError("Lot size unavailable for this contract")
    step = index_cfg(index)["step"]
    return Contract(index=index.upper(), exchange=r["exchange"], tradingsymbol=r["tradingsymbol"],
                    instrument_token=int(r["instrument_token"]), option_type=option_type, strike=strike,
                    expiry=expiry, lot_size=lot, tick_size=float(r.get("tick_size") or 0.05) or 0.05,
                    moneyness=classify(strike, spot, option_type, step))


def quantity(lots: int, lot_size: int) -> int:
    if lots < 1:
        raise ValueError("Lots must be at least 1")
    if lot_size < 1:
        raise ValueError("Lot size unavailable")
    return int(lots) * int(lot_size)


# ------------------------------------------------------------------ previous-day levels


def pivots(high: float, low: float, close: float) -> dict:
    p = (high + low + close) / 3
    return {"P": round(p, 2), "R1": round(2 * p - low, 2), "S1": round(2 * p - high, 2),
            "R2": round(p + (high - low), 2), "S2": round(p - (high - low), 2)}


def daily_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Per-day OHLC from intraday candles + previous-day columns aligned to each day."""
    d = df.copy()
    d["day"] = pd.to_datetime(d["date"]).dt.date
    g = d.groupby("day").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    prev = g.shift(1).add_prefix("prev_")
    return pd.concat([g, prev], axis=1)


# ------------------------------------------------------------------ estimated premiums


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, years: float, iv_pct: float, option_type: str, r: float = 0.065) -> float:
    """Black-Scholes premium. years -> time to expiry; iv_pct e.g. 12 for 12%."""
    intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
    if years <= 1e-6:
        return intrinsic
    s = iv_pct / 100
    d1 = (math.log(spot / strike) + (r + 0.5 * s * s) * years) / (s * math.sqrt(years))
    d2 = d1 - s * math.sqrt(years)
    if option_type == "CE":
        v = spot * _ncdf(d1) - strike * math.exp(-r * years) * _ncdf(d2)
    else:
        v = strike * math.exp(-r * years) * _ncdf(-d2) - spot * _ncdf(-d1)
    return max(v, intrinsic, 0.05)


def implied_vol(premium: float, spot: float, strike: float, years: float, option_type: str, r: float = 0.065) -> float | None:
    """IV % that makes Black-Scholes equal the market premium (bisection). None when it can't be solved
    (no time value left, bad inputs)."""
    if not premium or premium <= 0 or not spot or years <= 1e-6:
        return None
    intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
    if premium <= intrinsic + 0.05:
        return None
    lo, hi = 1.0, 300.0
    if bs_price(spot, strike, years, hi, option_type, r) < premium:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, option_type, r) > premium:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 2)


def bs_delta(spot: float, strike: float, years: float, iv_pct: float, option_type: str, r: float = 0.065) -> float:
    """Option delta (absolute, 0..1)."""
    if years <= 1e-6 or iv_pct <= 0:
        itm = spot > strike if option_type == "CE" else spot < strike
        return 1.0 if itm else 0.0
    s = iv_pct / 100
    d1 = (math.log(spot / strike) + (r + 0.5 * s * s) * years) / (s * math.sqrt(years))
    return _ncdf(d1) if option_type == "CE" else 1 - _ncdf(d1)


def estimated_expiry(index: str, on: date, min_days: int = 0) -> date:
    """Next expiry by rule (weekly weekday or last-weekday-of-month), ignoring holidays."""
    c = index_cfg(index)
    wd = c["expiry_weekday"]
    d = on + timedelta(days=min_days)
    if c["expiry_rule"] == "weekly":
        return d + timedelta(days=(wd - d.weekday()) % 7)
    # monthly: last <weekday> of the month, rolling to next month if passed
    for add in (0, 1, 2):
        y, m = d.year + (d.month - 1 + add) // 12, (d.month - 1 + add) % 12 + 1
        last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        exp = last - timedelta(days=(last.weekday() - wd) % 7)
        if exp >= d:
            return exp
    raise RuntimeError("unreachable")


def years_to_expiry(ts: datetime, expiry: date) -> float:
    exp_dt = datetime.combine(expiry, time(15, 30))
    secs = (exp_dt - ts).total_seconds()
    return max(secs, 0.0) / (365.0 * 86400)
