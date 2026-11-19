import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useState } from "react";
import { Alert, Text, View } from "react-native";
import { api, inr, type SessionEvent, type SessionSnapshot } from "../api";
import { Badge, Btn, C, Card, ErrorText, Row, Screen, Stat, pnlColor, s } from "../ui";
import { TradeRow } from "./BacktestResultScreen";
import { statusColor } from "./LiveScreen";

function describe(e: SessionEvent): string {
  const d = e.data ?? {};
  switch (e.kind) {
    case "candle":
      return `Candle ${String(d.candle?.date ?? "").slice(11, 16)} close ${d.candle?.close?.toFixed?.(2)}${d.signal ? ` → signal ${d.signal.toUpperCase()}` : ""}`;
    case "fill":
      return `Filled ${d.type} ${d.side > 0 ? "BUY" : "SELL"} ${d.qty} @ ${d.price} (${d.reason})`;
    case "order_sent":
      return `Order sent ${d.side > 0 ? "BUY" : "SELL"} ${d.qty} · id ${d.order_id}`;
    case "order_failed":
      return `❌ Order ${d.status}: ${d.message}`;
    case "trade":
      return `Closed ${d.side} ${d.qty}: ${inr(d.net_pnl, 2)} (${d.exit_reason})`;
    case "risk":
      return `⚠️ ${d.msg}`;
    default:
      return `${e.kind}: ${JSON.stringify(d)}`;
  }
}

export default function SessionScreen({ route }: any) {
  const id: string = route.params.id;
  const [snap, setSnap] = useState<SessionSnapshot | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [a, b] = await Promise.all([api.session(id), api.events(id)]);
      setSnap(a);
      setEvents(b);
      setErr(null);
    } catch (e: any) {
      setErr(e.message);
    }
  }, [id]);

  useFocusEffect(useCallback(() => {
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [load]));

  function stop() {
    Alert.alert("Stop session", "Square off the open position (if any) and stop?", [
      { text: "Cancel", style: "cancel" },
      {
        text: "Stop & square off", style: "destructive",
        onPress: async () => {
          setBusy(true);
          try {
            setSnap(await api.stopSession(id, true));
          } catch (e: any) {
            Alert.alert("Error", e.message);
          } finally {
            setBusy(false);
          }
        },
      },
    ]);
  }

  if (!snap) return <Screen><ErrorText msg={err} /><Text style={s.muted}>Loading…</Text></Screen>;
  const c = snap.config;
  const p = snap.position;

  return (
    <Screen>
      <Card>
        <View style={s.row}>
          <Badge text={String(c.mode).toUpperCase()} color={c.mode === "live" ? C.red : C.blue} />
          <Text style={[s.text, { flex: 1, fontWeight: "800", fontSize: 17 }]}>{c.exchange}:{c.tradingsymbol}</Text>
          <Badge text={snap.status} color={statusColor(snap.status)} />
        </View>
        <Text style={s.muted}>{c.strategy} {JSON.stringify(c.params)} · {c.interval} · {c.product} · qty {c.quantity}</Text>
        {snap.message ? <Text style={{ color: C.red, fontWeight: "700" }}>{snap.message}</Text> : null}
        <Text style={{ color: pnlColor(snap.net_pnl), fontSize: 28, fontWeight: "800" }}>{inr(snap.net_pnl, 2)}</Text>
        <Text style={s.muted}>realized {inr(snap.realized_pnl, 2)} · charges {inr(snap.charges, 2)} · last tick {snap.last_tick?.slice(11, 19) ?? "—"}</Text>
      </Card>
      <ErrorText msg={err} />

      <Card title="Position">
        <Row gap={8}>
          <Stat label="LTP" value={snap.ltp?.toFixed(2) ?? "—"} />
          <Stat label="Qty" value={String(p.qty)} color={p.qty > 0 ? C.green : p.qty < 0 ? C.red : undefined} />
          <Stat label="Entry" value={p.entry_price?.toFixed(2) ?? "—"} />
          <Stat label="Stop" value={p.stop?.toFixed(2) ?? "—"} color={C.red} />
          <Stat label="Target" value={p.target?.toFixed(2) ?? "—"} color={C.green} />
          <Stat label="Open P&L" value={inr(p.unrealized, 2)} color={pnlColor(p.unrealized)} />
        </Row>
        {snap.pending_orders.length > 0 && <Text style={{ color: C.amber }}>Waiting for Kite to confirm order {snap.pending_orders.join(", ")}</Text>}
        {snap.running_candle && (
          <Text style={s.muted}>
            Forming candle {String(snap.running_candle.date).slice(11, 16)}: O {snap.running_candle.open} H {snap.running_candle.high} L {snap.running_candle.low} C {snap.running_candle.close}
          </Text>
        )}
        {snap.status === "running" && <Btn label="Stop & square off" kind="danger" onPress={stop} loading={busy} />}
      </Card>

      <Card title={`Trades (${snap.trade_count})`}>
        {snap.trades.length === 0 ? <Text style={s.muted}>No closed trades yet.</Text> : [...snap.trades].reverse().map((t, i) => <TradeRow key={i} t={t} />)}
      </Card>

      <Card title="Activity log">
        {events.length === 0 ? <Text style={s.muted}>Waiting for the first candle to close…</Text> : events.slice(0, 60).map((e, i) => (
          <Text key={i} style={[s.muted, { color: e.kind === "order_failed" || e.kind === "risk" ? C.amber : C.dim }]}>
            {new Date(e.ts * 1000).toLocaleTimeString("en-IN", { hour12: false })}  {describe(e)}
          </Text>
        ))}
      </Card>
    </Screen>
  );
}
