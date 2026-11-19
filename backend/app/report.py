"""Trading history report: every trade with where and how it was executed, plus summaries.

Two formats from the same data:
* HTML — readable on the phone (summary, by day / entry type / exit reason / strategy, then each trade with
  its reasons and order events);
* CSV — one row per trade for Excel / Google Sheets.
"""
from __future__ import annotations

import csv
import html
import io
from datetime import date, datetime, timedelta

ENTRY_LABELS = {"strategy": "Main strategy", "any": "Any-strategy", "agreement": "Agreement", "override": "Strong trend",
                "late": "Late join", "pullback": "Pullback entry", "trendday": "Trend Day", "daybuilt": "Day-built", "manual": "Manual"}
EXIT_LABELS = {"target": "Target hit", "profit_lock": "Profit lock", "stop_loss": "Stop-loss", "trailing_stop": "Trailing stop", "time_cutoff": "Time cut-off",
               "time_stop": "Time-stop", "quick_target": "Quick target (slow trade)", "signal_exit": "Strategy exit", "manual_close": "Closed by you",
               "kill_switch": "Kill switch", "ai_exit": "AI exit (market turned / momentum faded)",
               "ai_giveback": "AI: protected profit (give-back)", "ai_sharp_move": "AI: early exit on sharp move against",
               "ai_near_target": "AI: booked near target (turning back)",
               "trend_down": "Swing: trend turned down", "roll": "Swing: rolled to next monthly"}


def _f(x, d=2):
    try:
        return round(float(x), d)
    except (TypeError, ValueError):
        return None


def trade_rows(positions, days: int = 30, mode: str = "all", source: str = "all", today: date | None = None) -> list[dict]:
    today = today or datetime.now().date()
    start = (today - timedelta(days=days - 1)).isoformat()
    out = []
    for p in positions:
        t_in = p.entry_time or p.created or ""
        if t_in[:10] < start or p.state in ("NEW",):
            continue
        if mode != "all" and p.mode != mode:
            continue
        src = p.spec.source or ""
        if source == "auto" and not src.startswith("auto:"):
            continue
        if source == "manual" and src.startswith("auto:"):
            continue
        st = p.stats or {}
        qty = p.filled_qty or p.spec.qty
        quote = st.get("quote_at_entry")
        slip = (p.entry_price - quote) * p.spec.side if p.entry_price and quote else None
        out.append({
            "date": t_in[:10], "entry_time": t_in[11:16], "exit_time": (p.exit_time or "")[11:16],
            "mode": p.mode, "source": "Auto" if src.startswith("auto:") else "Manual",
            "index": st.get("index") or p.spec.tradingsymbol.rstrip("0123456789CEP")[:9],
            "symbol": p.spec.tradingsymbol, "type": p.spec.instrument_type, "strike": p.spec.strike,
            "expiry": p.spec.expiry, "side": "BUY" if p.spec.side > 0 else "SELL", "lots": p.spec.lots, "qty": qty,
            "quote_at_entry": _f(quote), "entry_limit": _f(p.entry_limit), "entry_price": _f(p.entry_price),
            "entry_slippage": _f(slip), "exit_price": _f(p.exit_price),
            "stop_at_entry": _f(p.stop0), "final_stop": _f(p.stop), "target": _f(p.target),
            "spot_at_entry": _f(st.get("spot_at_entry")),
            "exit_reason": p.exit_reason or ("open" if p.is_active else p.state.lower()),
            "state": p.state, "gross": _f(p.gross), "charges": _f(p.charges), "net": _f(p.net),
            "entry_type": st.get("entry_type") or ("manual" if not src.startswith("auto:") else "strategy"),
            "proof": st.get("proof") or "", "strategy": st.get("strategy_name") or "",
            "candle": st.get("interval") or "",
            "settings_v": st.get("settings_v") or 1, "iv_at_entry": _f(st.get("iv")), "iv_level": st.get("iv_level") or "",
            "best_move_pts": _f(st.get("mfe_pts")), "worst_move_pts": _f(st.get("mae_pts")),
            "oi_wall": (st.get("oi_wall") or {}).get("strike") or "",
            "why": " | ".join(st.get("why") or []),
            "events": " | ".join(f"{e.get('t', '')[11:19]} {e.get('msg', '')}" for e in (p.events or [])[-12:]),
            "id": p.id,
        })
    out.sort(key=lambda r: (r["date"], r["entry_time"]))
    return out


def summarise(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["state"] == "POSITION_CLOSED" and r["net"] is not None]
    nets = [r["net"] for r in closed]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        dd = min(dd, eq - peak)

    def group(key):
        g: dict[str, dict] = {}
        for r in closed:
            k = r[key] or "—"
            x = g.setdefault(k, {"trades": 0, "wins": 0, "net": 0.0})
            x["trades"] += 1
            x["wins"] += r["net"] > 0
            x["net"] = round(x["net"] + r["net"], 2)
        for x in g.values():
            x["win_rate_pct"] = round(x["wins"] / x["trades"] * 100, 1)
        return dict(sorted(g.items(), key=lambda kv: -kv[1]["trades"]))
    days = group("date")
    return {
        "trades": len(closed), "open": sum(1 for r in rows if r["state"] != "POSITION_CLOSED" and r["exit_reason"] == "open"),
        "wins": len(wins), "losses": len(losses), "win_rate_pct": round(len(wins) / len(nets) * 100, 1) if nets else 0.0,
        "net": round(sum(nets), 2), "gross": round(sum(r["gross"] or 0 for r in closed), 2),
        "charges": round(sum(r["charges"] or 0 for r in closed), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0, "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_drawdown": round(dd, 2),
        "avg_entry_slippage": round(sum(r["entry_slippage"] for r in closed if r["entry_slippage"] is not None)
                                    / max(1, sum(1 for r in closed if r["entry_slippage"] is not None)), 2),
        "best_day": max(days.items(), key=lambda kv: kv[1]["net"])[0] if days else None,
        "worst_day": min(days.items(), key=lambda kv: kv[1]["net"])[0] if days else None,
        "by_day": dict(sorted(days.items())), "by_entry_type": group("entry_type"), "by_exit": group("exit_reason"),
        "by_strategy": group("strategy"), "by_index": group("index"),
    }


def to_csv(rows: list[dict]) -> str:
    cols = ["date", "entry_time", "exit_time", "mode", "source", "index", "symbol", "type", "strike", "expiry", "side", "lots",
            "qty", "spot_at_entry", "quote_at_entry", "entry_limit", "entry_price", "entry_slippage", "exit_price",
            "stop_at_entry", "final_stop", "target", "exit_reason", "gross", "charges", "net", "entry_type", "proof",
            "strategy", "candle", "settings_v", "iv_at_entry", "iv_level", "best_move_pts", "worst_move_pts", "oi_wall",
            "why", "events", "id"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def _money(x):
    if x is None:
        return "—"
    c = "#15803d" if x > 0 else "#b91c1c" if x < 0 else "#334155"
    return f'<span style="color:{c};font-weight:700">₹{x:,.2f}</span>'


def _table(title: str, g: dict, labels: dict | None = None) -> str:
    if not g:
        return ""
    rows = "".join(f"<tr><td>{html.escape((labels or {}).get(k, k))}</td><td>{v['trades']}</td><td>{v['win_rate_pct']}%</td>"
                   f"<td>{_money(v['net'])}</td></tr>" for k, v in g.items())
    return f"<h2>{title}</h2><table><tr><th></th><th>Trades</th><th>Win</th><th>Net</th></tr>{rows}</table>"


def to_html(rows: list[dict], days: int, mode: str, source: str, skipped: dict | None = None) -> str:
    s = summarise(rows)
    e = html.escape
    cards = [("Trades", s["trades"]), ("Win rate", f"{s['win_rate_pct']}%"), ("Net after costs", _money(s["net"])),
             ("Charges paid", _money(-s["charges"])), ("Avg win", _money(s["avg_win"])), ("Avg loss", _money(s["avg_loss"])),
             ("Profit factor", s["profit_factor"] or "—"), ("Worst drop", _money(s["max_drawdown"])),
             ("Avg entry slippage", f"₹{s['avg_entry_slippage']}/unit")]
    card_html = "".join(f'<div class="c"><div class="l">{k}</div><div class="v">{v}</div></div>' for k, v in cards)
    trades = []
    for r in reversed(rows):
        why = "".join(f"<li>{e(x)}</li>" for x in r["why"].split(" | ") if x) if r["why"] else ""
        ev = "".join(f"<li>{e(x)}</li>" for x in r["events"].split(" | ") if x) if r["events"] else ""
        trades.append(f"""
<details class="t"><summary>
  <b>{e(r['date'])} {e(r['entry_time'])}→{e(r['exit_time'] or '…')}</b> · {e(r['symbol'])} · {r['lots']} lot · {_money(r['net'])}
  <span class="tag">{e(ENTRY_LABELS.get(r['entry_type'], r['entry_type']))}</span>
  <span class="tag">{e(EXIT_LABELS.get(r['exit_reason'], r['exit_reason']))}</span>
</summary>
<div class="g">
  <div>Mode</div><div>{e(r['mode'].upper())} · {e(r['source'])}</div>
  <div>Strategy</div><div>{e(r['strategy'] or '—')} {('(' + e(r['proof']) + ')') if r['proof'] else ''} · {e(r['candle'])}</div>
  <div>Index at entry</div><div>{r['spot_at_entry'] or '—'}</div>
  <div>Option</div><div>{e(r['side'])} {e(r['type'])} {r['strike']} exp {e(r['expiry'] or '')} · qty {r['qty']}</div>
  <div>Quote → limit → fill</div><div>{r['quote_at_entry'] or '—'} → {r['entry_limit'] or '—'} → <b>{r['entry_price'] or '—'}</b>
       (slippage {r['entry_slippage'] if r['entry_slippage'] is not None else '—'}/unit)</div>
  <div>Stop / target</div><div>{r['stop_at_entry'] or '—'} (final {r['final_stop'] or '—'}) / {r['target'] or '—'}</div>
  <div>Exit</div><div><b>{r['exit_price'] or '—'}</b> · {e(EXIT_LABELS.get(r['exit_reason'], r['exit_reason']))}</div>
  <div>P&amp;L</div><div>gross {_money(r['gross'])} · charges ₹{r['charges'] or 0} · net {_money(r['net'])}</div>
</div>
{('<p class="h">Why it was placed</p><ul>' + why + '</ul>') if why else ''}
{('<p class="h">Order events</p><ul class="ev">' + ev + '</ul>') if ev else ''}
</details>""")
    skip = ""
    if skipped:
        skip = "<h2>Skipped signals (all time)</h2><table>" + "".join(
            f"<tr><td>{e(k)}</td><td>{n}</td></tr>" for k, n in skipped.items()) + "</table>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kite Algo report</title><style>
body{{font-family:system-ui,-apple-system,Roboto,sans-serif;margin:0;padding:16px;background:#f4f6fa;color:#0f172a}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:22px 0 8px}} .m{{color:#64748b;font-size:13px}}
.cs{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-top:12px}}
.c{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:10px}} .l{{color:#64748b;font-size:12px}} .v{{font-size:18px;font-weight:800}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;font-size:14px}}
td,th{{padding:7px 9px;border-bottom:1px solid #eef2f7;text-align:left}} th{{color:#64748b;font-weight:600;font-size:12px}}
.t{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;margin:8px 0}} summary{{cursor:pointer;line-height:1.6}}
.tag{{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:8px;padding:1px 7px;font-size:12px;margin-left:4px}}
.g{{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:14px;margin-top:8px}} .g>div:nth-child(odd){{color:#64748b}}
.h{{font-weight:700;margin:10px 0 2px}} ul{{margin:4px 0;padding-left:18px;font-size:14px}} .ev{{color:#475569;font-size:13px}}
</style></head><body>
<h1>Kite Algo — trading report</h1>
<div class="m">Last {days} days · {e(mode)} trades · {e(source)} · made {datetime.now():%d %b %Y %H:%M} ·
{s['open']} still open</div>
<div class="cs">{card_html}</div>
{_table("By day", {k: v for k, v in sorted(s['by_day'].items(), reverse=True)})}
{_table("By entry type", s['by_entry_type'], ENTRY_LABELS)}
{_table("By exit reason", s['by_exit'], EXIT_LABELS)}
{_table("By strategy", s['by_strategy'])}
{_table("By index", s['by_index'])}
{skip}
<h2>Every trade (newest first — tap to open)</h2>
{''.join(trades) or '<p class="m">No trades in this period.</p>'}
<p class="m">Paper trades are simulated fills on live prices. Net = after Zerodha charges. Slippage = fill minus the quote at entry.</p>
</body></html>"""
