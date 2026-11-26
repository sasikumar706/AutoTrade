import { useFocusEffect } from "@react-navigation/native";
import { usePollOnFocus } from "../usePoll";
import React, { useCallback, useEffect, useState } from "react";
import { Alert, RefreshControl, Text, View, Pressable } from "react-native";
import { api, inr } from "../api";
import { useMode, type TicketPrefill } from "../mode";
import { Badge, C, Card, Chips, ErrorText, Screen, pnlColor, s } from "../ui";
import { SessionsPanel } from "./LiveScreen";
import TradeTicket from "./TradeTicket";
import { confirmClear } from "../KiteLogout";
import { Btn } from "../ui";

/** The Trade tab shows the tools of the current mode only. */
export function TradeScreen({ navigation, route }: any) {
  const { mode } = useMode();
  const prefill: TicketPrefill | undefined = route?.params?.prefill;
  if (mode === "paper") return <PaperScreen navigation={navigation} prefill={prefill} />;
  return <LiveTradeScreen navigation={navigation} prefill={prefill} />;
}

export function PaperScreen({ navigation, prefill }: { navigation: any; prefill?: TicketPrefill }) {
  const [seg, setSeg] = useState<"options" | "sessions">("options");
  const [reload, setReload] = useState(0);
  useEffect(() => { if (prefill) setSeg("options"); }, [prefill?.nonce]);
  return (
    <Screen>
      <Chips options={["options", "sessions"] as const} value={seg} onChange={setSeg}
        labels={{ options: "Option paper trades", sessions: "Strategy sessions" }} />
      <Text style={s.muted}>Paper trading uses live Zerodha prices but simulated orders. No money at risk.</Text>
      {seg === "options" ? <TradeTicket key={`t${reload}`} mode="paper" navigation={navigation} prefill={prefill} />
        : <SessionsPanel key={`s${reload}`} navigation={navigation} modes={["paper", "replay"] as const} />}
      {seg === "sessions" && <Btn label="Clear paper session history" kind="ghost" onPress={() =>
        confirmClear("Clear paper session history?", "Removes finished paper and replay strategy sessions. Running sessions are kept.",
          async () => (await api.clearSessions("paper")).deleted, () => setReload((n) => n + 1))} />}
      {seg === "options" && (
        <Pressable onPress={() => navigation.navigate("Swing")}>
          <Card title="📈 Swing trend (holds for weeks)" right={<Text style={{ color: C.blue, fontWeight: "800" }}>Open ›</Text>}>
            <Text style={s.muted}>Monthly ITM CE on uptrends · daily check 15:15</Text>
          </Card>
        </Pressable>
      )}
    </Screen>
  );
}

function KitePositions() {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setData(await api.positions());
      setErr(null);
    } catch (e: any) {
      setErr(e.message);
    }
  }, []);
  usePollOnFocus(load, 5000);
  const net: any[] = data?.net ?? [];
  return (
    <Card title="Zerodha positions (all, incl. manual Kite trades)">
      <ErrorText msg={err} />
      {net.length === 0 ? <Text style={s.muted}>No positions today.</Text> : net.map((p, i) => (
        <View key={i} style={{ paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border }}>
          <View style={s.row}>
            <Text style={[s.text, { flex: 1, fontWeight: "700" }]}>{p.tradingsymbol}</Text>
            <Badge text={`${p.quantity} qty`} color={p.quantity ? C.amber : C.dim} />
          </View>
          <Text style={s.muted}>
            {p.product} · avg {p.average_price} · LTP {p.last_price} · <Text style={{ color: pnlColor(p.pnl) }}>{inr(p.pnl, 2)}</Text>
          </Text>
        </View>
      ))}
    </Card>
  );
}

export function LiveTradeScreen({ navigation, prefill }: { navigation: any; prefill?: TicketPrefill }) {
  const [seg, setSeg] = useState<"options" | "kite" | "sessions">("options");
  useEffect(() => { if (prefill) setSeg("options"); }, [prefill?.nonce]);
  const [liveEnabled, setLiveEnabled] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  useFocusEffect(useCallback(() => {
    api.authStatus().then((s2) => setLiveEnabled(s2.live_trading_enabled)).catch(() => {});
  }, []));
  return (
    <Screen refresh={<RefreshControl refreshing={refreshing} onRefresh={async () => {
      setRefreshing(true);
      try { setLiveEnabled((await api.authStatus()).live_trading_enabled); } catch {}
      setRefreshing(false);
    }} tintColor={C.dim} />}>
      <Chips options={["options", "kite", "sessions"] as const} value={seg} onChange={setSeg}
        labels={{ options: "Option orders", kite: "Kite positions", sessions: "Strategy sessions" }} />
      <Text style={{ color: liveEnabled ? C.red : C.amber, fontWeight: "700" }}>
        {liveEnabled ? "LIVE: orders here use real money." : "Live trading is disabled on the server."}
      </Text>
      {seg === "options" && <TradeTicket mode="live" navigation={navigation} liveEnabled={liveEnabled} prefill={prefill} />}
      {seg === "options" && (
        <Pressable onPress={() => navigation.navigate("Swing")}>
          <Card title="📈 Swing trend (holds for weeks)" right={<Text style={{ color: C.blue, fontWeight: "800" }}>Open ›</Text>}>
            <Text style={s.muted}>Monthly ITM CE on uptrends · daily check 15:15</Text>
          </Card>
        </Pressable>
      )}
      {seg === "kite" && <KitePositions />}
      {seg === "sessions" && <SessionsPanel navigation={navigation} modes={["live"] as const} />}
    </Screen>
  );
}
