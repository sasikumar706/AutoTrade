import React, { useCallback, useState } from "react";
import { Alert, Pressable, Switch, Text, View } from "react-native";
import { api, type AutoSnap, type PositionSnap } from "../api";
import { useMode } from "../mode";
import { C, ErrorText, Screen, pnlColor } from "../ui";
import { usePollOnFocus } from "../usePoll";

/**
 * Auto tab — one "cockpit" screen, read top to bottom:
 *  1. Status   — is it on, what is it doing right now, today's money in three numbers
 *  2. Now      — the open trade (live P&L on a stop→target track) OR the next trade it is waiting for
 *  3. Swing    — one line
 *  4. Today    — the trades it made today
 * Everything else (AI pick, scorecard, logs, tuning) lives behind "Details".
 */

const rs = (n?: number | null, sign = false) =>
  n == null ? "—" : `${n < 0 ? "−" : sign && n > 0 ? "+" : ""}₹${Math.abs(Math.round(n)).toLocaleString("en-IN")}`;
const hhmm = (t?: string | null) => (t ? t.slice(11, 16) : "");
const todayKey = () => new Date().toLocaleDateString("en-CA");

const EXIT: Record<string, string> = {
  target: "🎯 Target", stop_loss: "Stop-loss", trailing_stop: "Trailing stop", profit_lock: "🔒 Profit lock",
  quick_target: "⚡ Quick target", time_cutoff: "Day end", time_stop: "Time stop", signal_exit: "Signal flipped",
  manual_close: "You sold", kill_switch: "Kill switch", ai_exit: "AI exit", ai_giveback: "AI kept profit",
  ai_sharp_move: "AI early exit", ai_near_target: "AI near target", trend_down: "Trend down", roll: "Rolled",
  crash_guard: "Crash guard",
};
const exitLabel = (r?: string | null) => (r ? EXIT[r] ?? (r.startsWith("ai_") ? "AI exit" : r.replace(/_/g, " ")) : "");

type Tone = { label: string; color: string; sub?: string | null };

function statusOf(auto: boolean, starting: boolean, a: AutoSnap | null, pos: PositionSnap | null, err: string | null): Tone {
  if (!auto) return { label: "Auto is off", color: C.dim, sub: "Nothing trades by itself. Switch on to let the app trade." };
  if (err && !a) return { label: "Can't reach the server", color: C.red, sub: err };
  if (starting || !a) return { label: "Starting…", color: C.blue, sub: "Loading candles and testing strategies (up to a minute)" };
  if ((a as any).resume_pending) return { label: "Waiting for Kite login", color: C.amber, sub: "Log in to Kite — it switches on by itself" };
  if (!a.enabled) return { label: "Stopped", color: C.dim, sub: a.message };
  if (pos) return { label: "In a trade", color: C.blue, sub: null };
  if (a.day_blocked) return { label: "Done for today", color: C.amber, sub: a.day_blocked };
  return { label: "Watching the market", color: C.green, sub: a.watch?.status ?? a.message };
}

export default function AutoHomeScreen({ navigation }: any) {
  const { app, auto, mode, apply, refresh } = useMode();
  const [a, setA] = useState<AutoSnap | null>(null);
  const [w, setW] = useState<Record<string, any> | null>(null);
  const [today, setToday] = useState<PositionSnap[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      await refresh();
      const [aa, ww, tt] = await Promise.all([
        app?.auto_id ? api.auto(app.auto_id) : Promise.resolve(null),
        app?.swing_id ? api.swing(app.swing_id) : Promise.resolve(null),
        api.positionsDay(mode, todayKey()).catch(() => [] as PositionSnap[]),
      ]);
      setA(aa); setW(ww);
      setToday(tt.filter((p) => /^(auto|swing):/.test(p.spec.source ?? "") && p.state === "POSITION_CLOSED"));
      setErr(null);
    } catch (e: any) { setErr(e.message); }
  }, [app?.auto_id, app?.swing_id, mode]);
  usePollOnFocus(load, 8_000);

  const toggle = async (on: boolean) => {
    setBusy(true);
    try { await apply({ auto: on }); } catch (e: any) { Alert.alert("Auto", e.message); } finally { setBusy(false); }
  };

  const pos = a?.open_positions?.[0] ?? (a?.position && ["POSITION_OPEN", "ENTRY_PENDING", "EXIT_PENDING"].includes(a.position.state) ? a.position : null);
  const st = statusOf(auto, !!app?.starting, a, pos ?? null, err);
  const realized = a?.realized_pnl ?? 0;
  const open = a?.unrealized_pnl ?? 0;
  const dayNet = today.reduce((x, p) => x + (p.net ?? 0), 0);
  const wins = today.filter((p) => (p.net ?? 0) > 0).length;
  const riskLeft = a?.remaining_risk_budget ?? null;
  const dayStop = a?.money?.day_stop ?? a?.config?.max_daily_loss ?? null;

  return (
    <Screen>
      {/* 1 ── STATUS */}
      <View style={[box, { borderColor: st.color + "66" }]}>
        <View style={{ flexDirection: "row", alignItems: "center" }}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.dim, fontSize: 12, fontWeight: "800", letterSpacing: 0.6 }}>
              ⚡ AUTO · {mode === "live" ? "LIVE · REAL MONEY" : "PAPER"}
            </Text>
            <Text style={{ color: st.color, fontSize: 22, fontWeight: "900", marginTop: 2 }}>{st.label}</Text>
          </View>
          <Switch value={auto} disabled={busy} onValueChange={toggle}
            trackColor={{ true: C.green + "88", false: C.border }} thumbColor={auto ? C.green : "#fff"} />
        </View>
        {st.sub ? <Text style={{ color: C.text, fontSize: 14, lineHeight: 20, marginTop: 6 }} numberOfLines={3}>{st.sub}</Text> : null}
        {auto && a && (
          <View style={{ flexDirection: "row", marginTop: 14, gap: 8 }}>
            <Kpi label="Today" value={rs(realized + open, true)} color={pnlColor(realized + open)}
              sub={open ? `open ${rs(open, true)}` : undefined} />
            <Kpi label="Trades" value={String(a.trades_today)} sub={today.length ? `${wins} won · ${today.length - wins} lost` : undefined} />
            <Kpi label="Risk left" value={riskLeft == null ? "—" : rs(Math.max(riskLeft, 0))}
              sub={dayStop ? `of ${rs(dayStop)}` : undefined} color={riskLeft != null && dayStop && riskLeft < dayStop * 0.3 ? C.amber : C.text} />
          </View>
        )}
        {app?.needs_resume && (
          <Pressable onPress={() => toggle(true)} style={[pill, { backgroundColor: C.amber, marginTop: 12 }]}>
            <Text style={{ color: "#fff", fontWeight: "800" }}>Resume Auto</Text>
          </Pressable>
        )}
      </View>

      {/* 2 ── NOW: the open trade, or the next trade it's waiting for */}
      {auto && pos && <LiveTrade p={pos} onSell={() => sell(pos)} />}
      {auto && a?.enabled && !pos && !a.day_blocked && <NextTrade a={a} />}

      {/* 3 ── SWING */}
      {auto && app?.swing && w && (
        <Pressable onPress={() => navigation.navigate("Swing", { id: w.id })} style={box}>
          <Row label={`📈 Swing · ${w.config.index}`} right={<Text style={{ color: C.blue, fontWeight: "700" }}>›</Text>} />
          <Text style={{ color: C.text, marginTop: 4 }}>
            {w.position ? `Holding ${w.position.tradingsymbol ?? ""}` : `No position · decides daily at ${w.config.check_time}`}
            {w.position?.unrealized != null ? <Text style={{ color: pnlColor(w.position.unrealized), fontWeight: "800" }}>  {rs(w.position.unrealized, true)}</Text> : null}
          </Text>
          <Text style={{ color: C.dim, fontSize: 12, marginTop: 2 }}>Money {rs(w.equity)} · closed {rs(w.realized, true)}</Text>
        </Pressable>
      )}

      {/* 4 ── TODAY'S TRADES */}
      {auto && today.length > 0 && (
        <View style={box}>
          <Row label="Today's trades" right={<Text style={{ color: pnlColor(dayNet), fontWeight: "900" }}>{rs(dayNet, true)}</Text>} />
          {today.map((p) => (
            <Pressable key={p.id} onPress={() => navigation.navigate("Position", { id: p.id })}
              style={{ flexDirection: "row", alignItems: "center", paddingVertical: 9, borderTopWidth: 1, borderTopColor: C.border, gap: 8 }}>
              <Text style={{ color: C.dim, width: 44, fontVariant: ["tabular-nums"] }}>{hhmm(p.entry_time)}</Text>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontWeight: "700" }} numberOfLines={1}>{p.spec.tradingsymbol}</Text>
                <Text style={{ color: C.dim, fontSize: 12 }}>{p.spec.source.startsWith("swing") ? "Swing · " : ""}{exitLabel(p.exit_reason)}</Text>
              </View>
              <Text style={{ color: pnlColor(p.net), fontWeight: "800" }}>{rs(p.net, true)}</Text>
            </Pressable>
          ))}
        </View>
      )}

      {/* footer */}
      <View style={{ flexDirection: "row", gap: 8 }}>
        {a && <Link label="Strategy & log" onPress={() => navigation.navigate("AutoDetail", { id: a.id })} />}
        <Link label="Settings" onPress={() => navigation.navigate("Settings")} />
      </View>
      <Pressable onPress={() => navigation.navigate("AutoAdvanced")}>
        <Text style={{ color: C.dim, textAlign: "center", fontSize: 12, paddingVertical: 6 }}>Advanced: my own strategies ›</Text>
      </Pressable>
      <ErrorText msg={a ? err : null} />
    </Screen>
  );

  function sell(p: PositionSnap) {
    Alert.alert("Sell now?", `${p.spec.tradingsymbol} · ${rs(p.unrealized, true)}`, [
      { text: "Cancel", style: "cancel" },
      { text: "Sell", style: "destructive", onPress: async () => {
        try { await api.closePosition(p.id); load(); } catch (e: any) { Alert.alert("Sell", e.message); }
      } },
    ]);
  }
}

/* ---------- pieces ---------- */

function LiveTrade({ p, onSell }: { p: PositionSnap; onSell: () => void }) {
  const e = p.entry_price ?? 0, ltp = p.ltp ?? e, stop = p.stop ?? e, tgt = p.target ?? e + 2 * Math.abs(e - stop);
  const lo = Math.min(stop, tgt), hi = Math.max(stop, tgt), span = hi - lo || 1;
  const at = (x: number) => `${Math.min(100, Math.max(0, ((x - lo) / span) * 100))}%` as const;
  const locked = p.stats?.locked_rs as number | undefined;
  const lots = Math.round((p.filled_qty || p.spec.qty) / (p.spec.lot_size || 1));
  return (
    <View style={[box, { borderColor: C.blue + "55" }]}>
      <Row label="Open trade" right={<Text style={{ color: C.dim, fontSize: 12 }}>since {hhmm(p.entry_time)}</Text>} />
      <Text style={{ color: C.text, fontSize: 17, fontWeight: "900", marginTop: 4 }}>
        {p.spec.side > 0 ? "BUY" : "SELL"} {p.spec.tradingsymbol}
      </Text>
      <Text style={{ color: C.dim, marginTop: 2 }}>{lots} lot{lots === 1 ? "" : "s"} · qty {p.filled_qty || p.spec.qty} · {p.mode === "live" ? "real money" : "paper"}</Text>
      <View style={{ flexDirection: "row", alignItems: "flex-end", marginTop: 10 }}>
        <Text style={{ color: pnlColor(p.unrealized), fontSize: 30, fontWeight: "900", flex: 1 }}>{rs(p.unrealized, true)}</Text>
        <Text style={{ color: C.text, fontWeight: "700" }}>₹{e.toFixed(1)} → ₹{ltp.toFixed(1)}</Text>
      </View>
      {/* stop ─── entry ─── target track with the live price */}
      <View style={{ marginTop: 14, height: 34 }}>
        <View style={{ position: "absolute", left: 0, right: 0, top: 12, height: 8, borderRadius: 4, backgroundColor: C.border }} />
        <View style={{ position: "absolute", left: 0, width: at(e), top: 12, height: 8, borderTopLeftRadius: 4, borderBottomLeftRadius: 4, backgroundColor: C.red + "55" }} />
        <View style={{ position: "absolute", left: at(e), right: 0, top: 12, height: 8, borderTopRightRadius: 4, borderBottomRightRadius: 4, backgroundColor: C.green + "55" }} />
        <View style={{ position: "absolute", left: at(e), top: 6, width: 2, height: 20, backgroundColor: C.dim }} />
        <View style={{ position: "absolute", left: at(ltp), top: 4, marginLeft: -12, width: 24, height: 24, borderRadius: 12,
          backgroundColor: pnlColor(p.unrealized), borderWidth: 3, borderColor: "#fff" }} />
      </View>
      <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
        <Text style={{ color: C.red, fontSize: 12, fontWeight: "700" }}>Stop ₹{stop.toFixed(1)}</Text>
        <Text style={{ color: C.dim, fontSize: 12 }}>Entry ₹{e.toFixed(1)}</Text>
        <Text style={{ color: C.green, fontSize: 12, fontWeight: "700" }}>{p.target ? `Target ₹${tgt.toFixed(1)}` : "No fixed target"}</Text>
      </View>
      {locked ? <Text style={{ color: C.green, fontWeight: "800", marginTop: 8 }}>🔒 {rs(locked)} per lot locked in</Text> : null}
      {p.message ? <Text style={{ color: C.dim, fontSize: 13, marginTop: 6 }} numberOfLines={2}>{p.message}</Text> : null}
      <Pressable onPress={onSell} style={[pill, { backgroundColor: C.red + "14", borderWidth: 1, borderColor: C.red + "66", marginTop: 12 }]}>
        <Text style={{ color: C.red, fontWeight: "800" }}>Sell now</Text>
      </Pressable>
    </View>
  );
}

function NextTrade({ a }: { a: AutoSnap }) {
  const wt = a.watch;
  const o = wt?.expected_order;
  const up = wt?.bull_score ?? 0, down = wt?.bear_score ?? 0, total = wt?.checks_total ?? 0;
  const lean = wt?.lean === "BULLISH" ? { t: "Leaning UP", c: C.green } : wt?.lean === "BEARISH" ? { t: "Leaning DOWN", c: C.red } : { t: "No clear direction", c: C.dim };
  const blocked = o?.risk_problems?.length ? o.risk_problems[0] : null;
  return (
    <View style={box}>
      <Row label="Next trade" right={wt?.candle_closes_at ? <Text style={{ color: C.dim, fontSize: 12 }}>next check {wt.candle_closes_at}</Text> : null} />
      <View style={{ flexDirection: "row", alignItems: "center", marginTop: 6, gap: 10 }}>
        <Text style={{ color: lean.c, fontWeight: "900", fontSize: 16, flex: 1 }}>{lean.t}</Text>
        {total ? <Text style={{ color: C.dim, fontSize: 12 }}>indicators {up}↑ {down}↓ of {total}</Text> : null}
      </View>
      {total ? (
        <View style={{ flexDirection: "row", height: 6, borderRadius: 3, overflow: "hidden", backgroundColor: C.border, marginTop: 6 }}>
          <View style={{ flex: up, backgroundColor: C.green }} />
          <View style={{ flex: Math.max(total - up - down, 0), backgroundColor: C.border }} />
          <View style={{ flex: down, backgroundColor: C.red }} />
        </View>
      ) : null}
      {a.trend_note ? <Text style={{ color: C.text, fontSize: 13, marginTop: 8 }}>📈 {a.trend_note}</Text> : null}
      {o?.tradingsymbol ? (
        <View style={{ marginTop: 12, padding: 10, borderRadius: 10, backgroundColor: C.cardHi }}>
          <Text style={{ color: C.text, fontWeight: "800" }}>{o.action?.replace("_", " ")} {o.tradingsymbol}</Text>
          <Text style={{ color: C.dim, fontSize: 13, marginTop: 2 }}>
            ~₹{o.entry_price?.toFixed(1)} · stop ₹{o.stop_loss?.toFixed(1)} · target {o.target_price ? `₹${o.target_price.toFixed(1)}` : "—"} · risk {rs(o.est_max_loss)}
          </Text>
          {blocked ? <Text style={{ color: C.amber, fontSize: 13, fontWeight: "700", marginTop: 6 }}>⚠ Won't take it now: {blocked}</Text>
            : <Text style={{ color: C.green, fontSize: 13, fontWeight: "700", marginTop: 6 }}>✓ Passes the money checks</Text>}
        </View>
      ) : null}
    </View>
  );
}

function Kpi({ label, value, sub, color = C.text }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <View style={{ flex: 1, backgroundColor: C.cardHi, borderRadius: 12, padding: 10 }}>
      <Text style={{ color: C.dim, fontSize: 11, fontWeight: "800", letterSpacing: 0.5 }}>{label.toUpperCase()}</Text>
      <Text style={{ color, fontSize: 18, fontWeight: "900", marginTop: 2 }} numberOfLines={1} adjustsFontSizeToFit>{value}</Text>
      {sub ? <Text style={{ color: C.dim, fontSize: 11 }} numberOfLines={1}>{sub}</Text> : null}
    </View>
  );
}

function Row({ label, right }: { label: string; right?: React.ReactNode }) {
  return (
    <View style={{ flexDirection: "row", alignItems: "center" }}>
      <Text style={{ color: C.dim, fontSize: 12, fontWeight: "800", letterSpacing: 0.6, flex: 1 }}>{label.toUpperCase()}</Text>
      {right}
    </View>
  );
}

function Link({ label, onPress }: { label: string; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} style={[pill, { flex: 1, backgroundColor: C.card, borderWidth: 1, borderColor: C.border }]}>
      <Text style={{ color: C.blue, fontWeight: "800" }}>{label} ›</Text>
    </Pressable>
  );
}

const box = { backgroundColor: C.card, borderRadius: 16, padding: 16, borderWidth: 1, borderColor: C.border } as const;
const pill = { borderRadius: 12, paddingVertical: 12, alignItems: "center" as const };
