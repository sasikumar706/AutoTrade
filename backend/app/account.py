"""Your Zerodha account's day: every order, fill and position — whether it was placed by Kite Algo or by hand in the
Kite app — with amounts, P&L and estimated charges.

Zerodha's API only gives TODAY's orders, trades and positions. So each day's summary is also saved to a file
(data/zerodha_days/<day>.json) after the market closes; the history here grows from the day you start using it.
For anything older, Zerodha Console (console.zerodha.com → Reports → Tradebook / P&L) has the official record.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .charges import RATES, GST, _brokerage, round_trip_charges, segment_for


def _by(tag: str | None, order_tag: str) -> str:
    t = (tag or "").strip()
    return "Kite Algo" if t and (t.startswith("ka") or t == order_tag) else "Kite app / other"


def _itype(symbol: str) -> str | None:
    return symbol[-2:] if symbol[-2:] in ("CE", "PE") else ("FUT" if symbol.endswith("FUT") else None)


def _ex(exchange: str) -> str:
    return "BSE" if (exchange or "").upper() in ("BSE", "BFO") else "NSE"


def _t(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return str(v or "")


def day_summary(kite, order_tag: str = "kitealgo", now: datetime | None = None) -> dict:
    now = now or datetime.now()
    orders = kite.orders() or []
    trades = kite.trades() or []
    pos = (kite.positions() or {}).get("day") or []
    tag_of = {str(o.get("order_id")): o.get("tag") for o in orders}

    fills, per_order = [], {}
    for t in trades:
        sym, oid = t.get("tradingsymbol"), str(t.get("order_id"))
        qty, px = float(t.get("quantity") or 0), float(t.get("average_price") or 0)
        side = t.get("transaction_type")
        fills.append({"time": _t(t.get("fill_timestamp") or t.get("exchange_timestamp")), "symbol": sym, "side": side,
                      "qty": qty, "price": px, "amount": round(qty * px, 2), "order_id": oid,
                      "product": t.get("product"), "by": _by(tag_of.get(oid), order_tag)})
        k = (sym, oid)
        per_order[k] = per_order.get(k, 0.0) + qty * px
    fills.sort(key=lambda r: r["time"], reverse=True)

    rows, tot = [], {"buy_value": 0.0, "sell_value": 0.0, "pnl": 0.0, "realised": 0.0, "unrealised": 0.0,
                     "charges": 0.0, "open_positions": 0}
    for p in pos:
        sym = p.get("tradingsymbol")
        bv, sv = float(p.get("buy_value") or 0), float(p.get("sell_value") or 0)
        if not bv and not sv:
            continue
        seg = segment_for(p.get("product") or "MIS", _itype(sym))
        ch = round_trip_charges(seg, bv, sv, _ex(p.get("exchange")))
        # brokerage is per executed order (₹20 for F&O), not per round trip
        orders_here = [v for (s, _), v in per_order.items() if s == sym]
        if orders_here:
            real_b = sum(_brokerage(RATES[seg], v) for v in orders_here)
            ch["total"] = ch["total"] - ch["brokerage"] * (1 + GST) + real_b * (1 + GST)
            ch["gst"] = round(ch["gst"] - ch["brokerage"] * GST + real_b * GST, 2)
            ch["brokerage"] = round(real_b, 2)
        charges = round(float(ch["total"]), 2)
        pnl = float(p.get("pnl") or 0)
        oq = int(p.get("quantity") or 0)
        srcs = sorted({f["by"] for f in fills if f["symbol"] == sym}) or ["Kite app / other"]
        rows.append({"symbol": sym, "exchange": p.get("exchange"), "product": p.get("product"), "by": " + ".join(srcs),
                     "buy_qty": int(p.get("buy_quantity") or 0), "avg_buy": round(float(p.get("buy_price") or 0), 2),
                     "buy_value": round(bv, 2), "sell_qty": int(p.get("sell_quantity") or 0),
                     "avg_sell": round(float(p.get("sell_price") or 0), 2), "sell_value": round(sv, 2),
                     "open_qty": oq, "ltp": p.get("last_price"), "pnl": round(pnl, 2),
                     "realised": round(float(p.get("realised") or 0), 2), "unrealised": round(float(p.get("unrealised") or 0), 2),
                     "charges": charges, "charges_detail": {k: round(float(v), 2) for k, v in ch.items()},
                     "net": round(pnl - charges, 2), "orders": len(orders_here)})
        tot["buy_value"] += bv
        tot["sell_value"] += sv
        tot["pnl"] += pnl
        tot["realised"] += float(p.get("realised") or 0)
        tot["unrealised"] += float(p.get("unrealised") or 0)
        tot["charges"] += charges
        tot["open_positions"] += 1 if oq else 0
    rows.sort(key=lambda r: abs(r["pnl"]), reverse=True)
    tot = {k: round(v, 2) if isinstance(v, float) else v for k, v in tot.items()}
    tot["net_after_charges"] = round(tot["pnl"] - tot["charges"], 2)
    tot["orders"] = len(orders)
    tot["filled_orders"] = sum(1 for o in orders if o.get("status") == "COMPLETE")
    tot["rejected_orders"] = sum(1 for o in orders if o.get("status") == "REJECTED")
    tot["trades"] = len(fills)
    tot["by_kite_algo"] = round(sum(r["pnl"] for r in rows if r["by"] == "Kite Algo"), 2)
    tot["by_hand"] = round(sum(r["pnl"] for r in rows if r["by"] != "Kite Algo"), 2)

    olist = []
    for o in orders:
        olist.append({"time": _t(o.get("order_timestamp")), "symbol": o.get("tradingsymbol"), "side": o.get("transaction_type"),
                      "qty": o.get("quantity"), "filled": o.get("filled_quantity"), "type": o.get("order_type"),
                      "price": o.get("price"), "avg_price": o.get("average_price"), "status": o.get("status"),
                      "reason": o.get("status_message") or "", "by": _by(o.get("tag"), order_tag), "order_id": o.get("order_id")})
    olist.sort(key=lambda r: r["time"], reverse=True)
    return {"day": now.date().isoformat(), "fetched_at": now.isoformat(timespec="seconds"), "live": True,
            "totals": tot, "symbols": rows, "trades": fills, "orders": olist,
            "note": "Charges are estimated from Zerodha's published rates (brokerage per order, STT, exchange, GST, SEBI, "
                    "stamp). The contract note in Console is the exact figure."}


def _dir(data_dir) -> Path:
    d = Path(data_dir) / "zerodha_days"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_day(data_dir, summary: dict) -> None:
    if not summary.get("symbols") and not summary.get("orders"):
        return
    (_dir(data_dir) / f"{summary['day']}.json").write_text(json.dumps({**summary, "live": False}))


def load_day(data_dir, day: str) -> dict | None:
    date.fromisoformat(day)  # validates
    f = _dir(data_dir) / f"{day}.json"
    return json.loads(f.read_text()) if f.exists() else None


def saved_days(data_dir, limit: int = 120) -> list[dict]:
    out = []
    for f in sorted(_dir(data_dir).glob("*.json"), reverse=True)[:limit]:
        try:
            d = json.loads(f.read_text())
            out.append({"day": d["day"], **{k: d["totals"].get(k) for k in
                                            ("pnl", "charges", "net_after_charges", "trades", "by_kite_algo", "by_hand")}})
        except Exception:
            continue
    return out
