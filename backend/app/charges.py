"""Zerodha brokerage + statutory charges.

Rates from zerodha.com/charges, checked 29 Oct 2026 (STT revised by Budget 2026-27,
effective 1 Apr 2026). Rates change — update RATES when Zerodha/NSE/Govt revise them.
All percentages below are fractions (0.0003 == 0.03%).
"""
from __future__ import annotations

SEGMENTS = ("equity_delivery", "equity_intraday", "futures", "options")

RATES = {
    "equity_delivery": {
        "brokerage_pct": 0.0,
        "brokerage_cap": 0.0,
        "brokerage_flat": None,
        "stt_buy": 0.001,
        "stt_sell": 0.001,
        "txn": {"NSE": 0.0000307, "BSE": 0.0000375},
        "stamp_buy": 0.00015,
        "dp_per_sell": 15.34,  # per scrip per day sold from demat (incl. GST)
    },
    "equity_intraday": {
        "brokerage_pct": 0.0003,
        "brokerage_cap": 20.0,
        "brokerage_flat": None,
        "stt_buy": 0.0,
        "stt_sell": 0.00025,
        "txn": {"NSE": 0.0000307, "BSE": 0.0000375},
        "stamp_buy": 0.00003,
        "dp_per_sell": 0.0,
    },
    "futures": {
        "brokerage_pct": 0.0003,
        "brokerage_cap": 20.0,
        "brokerage_flat": None,
        "stt_buy": 0.0,
        "stt_sell": 0.0005,
        "txn": {"NSE": 0.0000183, "BSE": 0.0},
        "stamp_buy": 0.00002,
        "dp_per_sell": 0.0,
    },
    "options": {  # turnover = premium x quantity
        "brokerage_pct": 0.0,
        "brokerage_cap": 0.0,
        "brokerage_flat": 20.0,
        "stt_buy": 0.0,
        "stt_sell": 0.0015,
        "txn": {"NSE": 0.0003553, "BSE": 0.000325},
        "stamp_buy": 0.00003,
        "dp_per_sell": 0.0,
    },
}

SEBI_PER_CRORE = 10.0
GST = 0.18


def _brokerage(r: dict, value: float) -> float:
    if value <= 0:
        return 0.0
    if r["brokerage_flat"] is not None:
        return r["brokerage_flat"]
    return min(value * r["brokerage_pct"], r["brokerage_cap"])


def round_trip_charges(segment: str, buy_value: float, sell_value: float, exchange: str = "NSE") -> dict:
    """Charges for one buy order + one sell order of the given turnover (in rupees)."""
    if segment not in RATES:
        raise ValueError(f"segment must be one of {SEGMENTS}")
    r = RATES[segment]
    exchange = exchange.upper() if exchange.upper() in ("NSE", "BSE") else "NSE"
    turnover = buy_value + sell_value
    brokerage = _brokerage(r, buy_value) + _brokerage(r, sell_value)
    stt = buy_value * r["stt_buy"] + sell_value * r["stt_sell"]
    txn = turnover * r["txn"][exchange]
    sebi = turnover * SEBI_PER_CRORE / 1e7
    stamp = buy_value * r["stamp_buy"]
    gst = (brokerage + txn + sebi) * GST
    dp = r["dp_per_sell"] if sell_value > 0 else 0.0
    total = brokerage + stt + txn + sebi + stamp + gst + dp
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(txn, 2),
        "sebi": round(sebi, 4),
        "stamp_duty": round(stamp, 2),
        "gst": round(gst, 2),
        "dp": round(dp, 2),
        "total": total,
    }


def segment_for(product: str, instrument_type: str | None) -> str:
    """Map Kite product + instrument type to a charges segment."""
    it = (instrument_type or "EQ").upper()
    if it in ("CE", "PE"):
        return "options"
    if it == "FUT":
        return "futures"
    return "equity_intraday" if product == "MIS" else "equity_delivery"
