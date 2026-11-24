import { usePollOnFocus } from "../usePoll";
import React, { useCallback, useState } from "react";
import { Alert, Text, View } from "react-native";
import { api, inr, type PositionSnap, type WhyItem } from "../api";
import { useView } from "../view";
import { WhyList } from "../WhyList";
import { Badge, Btn, C, Card, ErrorText, LineChart, Row, Screen, Stat, pnlColor, s, when, contractText, Hint } from "../ui";
import { stateColor } from "./TradeTicket";

export default function PositionScreen({ route }: any) {
  const id: string = route.params.id;
  const [p, setP] = useState<PositionSnap | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [why, setWhy] = useState<WhyItem[] | null>(null);
  const { detailed } = useView();
  const load = useCallback(async () => {
    try {
      const x = await api.position(id);
      setP(x);
      setErr(null);
      if (x.spec.source?.startsWith("auto:") && why === null) {
        const a = await api.auto(x.spec.source.slice(5)).catch(() => null);
        const sig = a?.signals.find((g) => g.position_id === x.id);
        setWhy((sig?.why as WhyItem[]) ?? []);
      }
    } catch (e: any) { setErr(e.message); }
  }, [id, why]);
  usePollOnFocus(load, 2000);

  if (!p) return <Screen><ErrorText msg={err} /><Text style={s.muted}>Loading…</Text></Screen>;
  const open = ["POSITION_OPEN", "PARTIALLY_FILLED", "EXIT_FAILED"].includes(p.state);
  const long = p.spec.side > 0;
  const pnl = open ? p.unrealized : p.net;
  function close() {
    Alert.alert(long ? "SELL to close?" : "BUY to close?", `${p!.spec.tradingsymbol} at market`, [
      { text: "Cancel", style: "cancel" },
      { text: "Close", style: "destructive", onPress: async () => { try { setP(await api.closePosition(p!.id)); } catch (e: any) { Alert.alert("Error", e.message); } } },
    ]);
  }
  return (
    <Screen>
      <Card>
        <View style={s.row}>
          <Badge text={p.mode.toUpperCase()} color={p.mode === "live" ? C.red : C.blue} />
          <Text style={[s.text, { flex: 1, fontWeight: "800", fontSize: 16 }]} numberOfLines={2}>{contractText({ tradingsymbol: p.spec.tradingsymbol, strike: p.spec.strike, expiry: p.spec.expiry, option_type: p.spec.instrument_type })}</Text>
          <Badge text={p.state.replace(/_/g, " ")} color={stateColor(p.state)} />
        </View>
        <Text style={[s.text, { fontWeight: "700" }]}>
          📅 Entry {p.entry_time ? when(p.entry_time) : when(p.created)}{p.exit_time ? `  →  exit ${when(p.exit_time)}` : ""}
        </Text>
        <Text style={s.muted}>{long ? "Long (bought to open)" : "Short (sold to open)"} · {p.spec.lots} lot × {p.spec.lot_size} · {p.spec.product} · {p.spec.source}</Text>
        {p.message ? <Text style={{ color: stateColor(p.state), fontWeight: "700" }}>{p.message}</Text> : null}
        {open && p.ltp != null && p.entry_price ? <LivePrice p={p} /> : null}
        <Text style={{ color: pnlColor(pnl), fontSize: 28, fontWeight: "800" }}>{inr(pnl, 2)}</Text>
        {!open && p.state === "POSITION_CLOSED" && <Text style={s.muted}>gross {inr(p.gross, 2)} · charges {inr(p.charges, 2)} · exit: {p.exit_reason}</Text>}
      </Card>
      <ErrorText msg={err} />
      {(open || (p.booked?.length ?? 0) > 0) && (p.stats?.risk_live || (p.booked?.length ?? 0) > 0) ? <RiskMonitor p={p} open={open} /> : null}
      {why && why.length > 0 && <Card detail title="Why Auto Trade placed this order"><WhyList why={why} /></Card>}
      {(p.path?.length ?? 0) > 1 && (
        <Card detail title="Premium since entry">
          <LineChart values={p.path!.map((x) => x[1])} color={pnlColor(pnl) === C.text ? C.blue : pnlColor(pnl)} height={170}
            lines={[
              ...(p.entry_price ? [{ value: p.entry_price, color: C.dim, label: "Entry" }] : []),
              ...(p.stop ? [{ value: p.stop, color: C.red, label: p.stop0 && p.stop !== p.stop0 ? "Trail stop" : "Stop" }] : []),
              ...(p.target ? [{ value: p.target, color: C.green, label: "Target" }] : []),
            ]} />
          <Text style={s.muted}>{(p.entry_time ?? "").slice(0, 10)} · {p.path![0][0].slice(0, 5)} → {p.path![p.path!.length - 1][0].slice(0, 5)} · sampled every 20 s</Text>
        </Card>
      )}
      <Card title="Levels">
        <Row gap={8}>
          <Stat label="LTP" value={p.ltp?.toFixed(2) ?? "—"} />
          <Stat label="Entry" value={p.entry_price?.toFixed(2) ?? "—"} />
          <Stat label="Stop" value={p.stop?.toFixed(2) ?? "—"} color={C.red} />
          {detailed && <Stat label="Initial stop" value={p.stop0?.toFixed(2) ?? "—"} />}
          <Stat label="Target" value={p.target?.toFixed(2) ?? "—"} color={C.green} />
          <Stat label="Exit" value={p.exit_price?.toFixed(2) ?? "—"} />
          <Stat label="Filled qty" value={String(p.filled_qty)} />
          {detailed && <Stat label="Entry slippage" value={p.stats.entry_slippage_pct !== null && p.stats.entry_slippage_pct !== undefined ? `${p.stats.entry_slippage_pct}%` : "—"} />}
        </Row>
        {p.mode === "live" && detailed && (
          <Text style={s.muted}>Orders: entry {p.entry_order_id ?? "—"} · protective SL {p.sl_order_id ?? "—"} · exit {p.exit_order_id ?? "—"}</Text>
        )}
        {open && <Btn label={long ? "SELL to close" : "BUY to close"} kind="danger" onPress={close} />}
      </Card>
      <Card detail title="Execution log">
        {[...p.events].reverse().map((e, i) => (
          <Text key={i} style={[s.muted, { color: e.kind === "alert" ? C.red : C.dim }]}>{when(e.t, true)}  {e.msg}</Text>
        ))}
      </Card>
    </Screen>
  );
}


/** Live premium with where it sits between the stop-loss and the target. */
function LivePrice({ p }: { p: PositionSnap }) {
  const ltp = p.ltp as number, entry = p.entry_price as number, side = p.spec.side;
  const diff = (ltp - entry) * side;
  const pct = (diff / entry) * 100;
  const col = diff > 0 ? C.green : diff < 0 ? C.red : C.text;
  const stop = p.stop ?? null, target = p.target ?? null;
  const lo = stop !== null ? Math.min(stop, ltp) : Math.min(entry, ltp);
  const hi = target !== null ? Math.max(target, ltp) : Math.max(entry, ltp) * 1.001;
  const pos = (v: number) => `${Math.max(0, Math.min(100, ((v - lo) / (hi - lo || 1)) * 100))}%` as const;
  return (
    <View style={{ gap: 6, padding: 10, borderRadius: 12, backgroundColor: col + "12" }}>
      <View style={{ flexDirection: "row", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <Text style={{ color: C.dim, fontSize: 13, fontWeight: "700" }}>NOW</Text>
        <Text style={{ color: C.text, fontSize: 30, fontWeight: "900" }}>₹{ltp.toFixed(2)}</Text>
        <Text style={{ color: col, fontSize: 16, fontWeight: "800" }}>
          {diff >= 0 ? "▲" : "▼"} {diff >= 0 ? "+" : ""}{diff.toFixed(2)} ({pct >= 0 ? "+" : ""}{pct.toFixed(1)}%) from entry {entry.toFixed(2)}
        </Text>
      </View>
      {stop !== null && target !== null && (<>
        <View style={{ height: 10, borderRadius: 5, backgroundColor: C.cardHi, marginTop: 6 }}>
          <View style={{ position: "absolute", left: 0, width: pos(entry), top: 0, bottom: 0, borderRadius: 5, backgroundColor: C.red + "33" }} />
          <View style={{ position: "absolute", left: pos(entry), right: 0, top: 0, bottom: 0, borderRadius: 5, backgroundColor: C.green + "33" }} />
          <View style={{ position: "absolute", left: pos(entry), top: -3, width: 2, height: 16, backgroundColor: C.dim }} />
          <View style={{ position: "absolute", left: pos(ltp), top: -5, width: 8, height: 20, marginLeft: -4, borderRadius: 4, backgroundColor: col }} />
        </View>
        <View style={{ flexDirection: "row" }}>
          <Text style={{ color: C.red, fontSize: 12, fontWeight: "700", flex: 1 }}>Stop {stop.toFixed(2)} · {Math.abs(ltp - stop).toFixed(1)} away</Text>
          <Text style={{ color: C.green, fontSize: 12, fontWeight: "700" }}>Target {target.toFixed(2)} · {Math.abs(target - ltp).toFixed(1)} away</Text>
        </View>
      </>)}
      <Text style={s.muted}>Updates every 2 seconds{p.stop0 && p.stop !== p.stop0 ? ` · stop has moved up from ${p.stop0} (trailing / break-even)` : ""}</Text>
    </View>
  );
}

const pct = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}R`;

/** What the AI sees right now for this open trade (refreshed every ~10 s on the server) and what it has done. */
function RiskMonitor({ p, open }: { p: PositionSnap; open: boolean }) {
  const r = p.stats?.risk_live as Record<string, any> | undefined;
  const dangerColor = !r ? C.dim : r.danger >= 60 ? C.red : r.danger >= 30 ? C.amber : C.green;
  const feel = !r ? "" : r.danger >= 60 ? "High risk — ready to exit early" : r.danger >= 30 ? "Watching closely" : "Healthy";
  return (
    <Card detail title="🧠 AI risk monitor" right={r && open ? <Badge text={`${r.danger}/100`} color={dangerColor} /> : undefined}>
      {r && open && (
        <>
          <Text style={{ color: dangerColor, fontWeight: "800" }}>{feel}</Text>
          <View style={{ height: 8, backgroundColor: C.cardHi, borderRadius: 999, overflow: "hidden" }}>
            <View style={{ width: `${Math.max(4, r.danger)}%`, height: 8, backgroundColor: dangerColor, borderRadius: 999 }} />
          </View>
          <Row gap={8}>
            <Stat label="Open P&L" value={inr(r.open_pnl)} color={pnlColor(r.open_pnl)} />
            <Stat label="₹ at risk to stop" value={r.at_risk != null ? inr(r.at_risk) : "—"} color={C.red} />
            <Stat label="₹ locked in" value={inr(r.locked)} color={r.locked > 0 ? C.green : undefined} />
          </Row>
          <Row gap={8}>
            <Stat label="Now / best" value={`${pct(r.r)} / ${pct(r.peak_r)}`} />
            <Stat label="Index (2 min)" value={`${r.velocity > 0 ? "↑" : r.velocity < 0 ? "↓" : "→"} ${Math.abs(r.velocity).toFixed(1)}× candle`}
              color={r.velocity < -0.4 ? C.red : r.velocity > 0.4 ? C.green : undefined} />
            <Stat label="Lots running" value={String(r.lots_running)} />
          </Row>
          <Text style={s.muted}>
            {r.wrong_vwap ? "⚠ Index on the wrong side of VWAP" : "✓ Index on the right side of VWAP"}
            {r.agree_pct != null ? ` · ${r.agree_pct}% of indicators still agree` : ""} · checked {when(r.at, true).slice(-8)}
          </Text>
        </>
      )}
      {r?.action ? <Text style={[s.text, { fontWeight: "700" }]}>Last action: {r.action}</Text> : null}
      {(p.booked ?? []).map((b, i) => (
        <View key={i} style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
          <Badge text="BOOKED" color={C.green} />
          <Text style={[s.muted, { flex: 1 }]}>{when(b.time, true)} · {b.qty} @ ₹{b.price}</Text>
          <Text style={{ color: pnlColor(b.gross - b.charges), fontWeight: "800" }}>{inr(b.gross - b.charges)}</Text>
        </View>
      ))}
      <Hint>
        Checked every ~10 s, between candles too: books half the lots at +1R (rest rides free with the stop at entry), closes if the
        trade hands back most of its profit, exits early on a sharp move against it, and books when it stalls just before the target.
      </Hint>
    </Card>
  );
}
