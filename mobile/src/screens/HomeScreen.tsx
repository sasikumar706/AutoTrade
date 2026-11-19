import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useState } from "react";
import { Pressable, RefreshControl, Text, View } from "react-native";
import { api, currentTarget, getSettings, inr, pct, type AuthStatus, type AutoSnap, type PositionSnap } from "../api";
import { useMode } from "../mode";
import { autoStateColor, isLive } from "./AutoTradeScreen";
import { PositionRow } from "./TradeTicket";
import MorningCard from "../MorningCard";
import { IndexTicker, useIndexMoves } from "../IndexTicker";
import { Badge, Btn, C, Card, ErrorText, Row, Screen, Stat, pnlColor, s, Hint } from "../ui";

export default function HomeScreen({ navigation }: any) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [margins, setMargins] = useState<any>(null);
  const { mode, auto } = useMode();
  const moves = useIndexMoves(true);
  const [positions, setPositions] = useState<PositionSnap[]>([]);
  const [autos, setAutos] = useState<AutoSnap[]>([]);
  const [autoErr, setAutoErr] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [target, setTarget] = useState<"phone" | "pc">("phone");
  const [hasPc, setHasPc] = useState(false);

  const load = useCallback(async () => {
    const cfg = await getSettings();
    if (!cfg.url || !cfg.token) {
      setErr("Open Settings and enter your server URL and app token.");
      return;
    }
    const t = await currentTarget();
    setTarget(t);
    setHasPc(!!cfg.pcUrl);
    try {
      let st = await api.authStatus();
      if (t === "pc" && !st.logged_in) {
        // logged in on the phone this morning? copy that login to the PC
        const ph = await api.authStatusOn("phone").catch(() => null);
        if (ph?.logged_in) { await api.syncSessionToPc().catch(() => null); st = await api.authStatus(); }
      }
      setStatus(st);
      setErr(null);
      if (auto) {
        try {
          setAutos((await api.autos()).filter((a) => a.config.mode === mode && (a.enabled || a.position)));
          setAutoErr(null);
        } catch (e: any) { setAutoErr(e.message); }
      }
      setPositions(await api.positionsList(mode).catch(() => []));
      setMargins(st.logged_in && mode === "live" ? await api.margins().catch(() => null) : null);
    } catch (e: any) {
      setErr(e.message);
    }
  }, [mode, auto]);

  useFocusEffect(useCallback(() => { load(); }, [load]));



  const eq = margins?.equity;
  const open = positions.filter((p) => p.state !== "POSITION_CLOSED" && p.state !== "ORDER_REJECTED");
  const today = new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, phone's local (IST) date
  const closedToday = positions.filter((p) => p.state === "POSITION_CLOSED" && (p.exit_time ?? p.created ?? "").startsWith(today));
  const realised = closedToday.reduce((a, p) => a + (p.net ?? 0), 0);
  const unrealised = open.reduce((a, p) => a + (p.unrealized ?? 0), 0);
  const goTrade = () => navigation.navigate("Trade");

  return (
    <Screen refresh={<RefreshControl refreshing={refreshing} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} tintColor={C.dim} />}>
      {hasPc && (
        <View style={{ flexDirection: "row" }}>
          <Badge text={target === "pc" ? "🖥 PC SERVER" : "📱 PHONE SERVER"} color={target === "pc" ? C.amber : C.blue} />
        </View>
      )}
      {status?.logged_in && (
        <IndexTicker moves={moves} onPress={(k) => {
          const m = { NIFTY: ["NSE", "NIFTY 50"], BANKNIFTY: ["NSE", "NIFTY BANK"], SENSEX: ["BSE", "SENSEX"] }[k];
          navigation.navigate("Quote", { exchange: m[0], tradingsymbol: m[1] });
        }} />
      )}

      {(err || (status && !status.logged_in)) && (
        <Pressable onPress={() => navigation.navigate("Settings")}
          style={{ padding: 12, borderRadius: 12, borderWidth: 1, borderColor: C.amber, backgroundColor: C.amber + "18" }}>
          <Text style={{ color: C.amber, fontWeight: "800" }}>
            {err ? "⚠ Server not reachable" : "🔑 Kite logged out"} — tap to open Settings
          </Text>
          {err ? <Text style={s.muted}>{err}</Text> : <Text style={s.muted}>Log in once each morning in Settings → Zerodha.</Text>}
        </Pressable>
      )}


      {auto && (
        <Card title="⚡ Auto trading today">
          {autos.length === 0 ? <Text style={s.muted}>{autoErr ? `Couldn't load: ${autoErr}` : "No Autopilot running — open Auto to check"}</Text> : [...autos].sort((x, y) => Number(isLive(y)) - Number(isLive(x))).map((a) => (
            <Pressable key={a.id} onPress={() => navigation.navigate("AutoDetail", { id: a.id })}
              style={{ paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border, gap: 4 }}>
              <View style={s.row}>
                <Badge text={a.config.mode.toUpperCase()} color={a.config.mode === "live" ? C.red : C.blue} />
                <Text style={[s.text, { flex: 1, fontWeight: "700" }]} numberOfLines={1}>
                  {a.config.index} · {a.config.strategy_source === "ai" ? `✦ ${a.ai_pick?.name ?? "AI"}` : a.config.strategy}
                </Text>
                <Badge text={a.enabled ? (a.paused ? "PAUSED" : "ON") : "OFF"} color={a.enabled ? (a.paused ? C.amber : C.green) : C.dim} />
              </View>
              <View style={s.row}>
                <Badge text={a.state.replace(/_/g, " ")} color={autoStateColor(a.state)} />
                <Text style={[s.muted, { flex: 1 }]} numberOfLines={1}>{a.message}</Text>
                <Text style={{ color: pnlColor(a.realized_pnl + a.unrealized_pnl), fontWeight: "700" }}>{inr(a.realized_pnl + a.unrealized_pnl, 0)}</Text>
              </View>
            </Pressable>
          ))}
          <Btn label="Open Auto" kind="warn" onPress={() => navigation.navigate("Auto")} />
        </Card>
      )}

      {(
        <Card title={mode === "paper" ? "Paper trading today" : "Live trading today"}
          right={mode === "live" && status ? <Badge text={status.live_trading_enabled ? "ENABLED" : "OFF ON SERVER"} color={status.live_trading_enabled ? C.red : C.amber} /> : null}>
          <Row gap={8}>
            <Stat label="Open" value={`${open.length}`} />
            <Stat label="Unrealised" value={inr(unrealised, 0)} color={pnlColor(unrealised)} />
            <Stat label="Realised" value={inr(realised, 0)} color={pnlColor(realised)} />
          </Row>
          {open.slice(0, 5).map((p) => <PositionRow key={p.id} p={p} onPress={() => navigation.navigate("Position", { id: p.id })} />)}
          {mode === "live" && status && !status.live_trading_enabled && (
            <Text style={{ color: C.amber }}>Live orders need a static-IP server with LIVE_TRADING_ENABLED=true.</Text>
          )}
          <Btn label={mode === "paper" ? "Open paper trading" : "Open live trading"} kind={mode === "live" ? "warn" : "primary"} onPress={goTrade} />
        </Card>
      )}

      {mode === "live" && eq && (
        <Card title="Funds (equity)">
          <Row gap={8}>
            <Stat label="Available" value={inr(eq.net)} />
            <Stat label="Cash" value={inr(eq.available?.live_balance ?? eq.available?.cash)} />
            <Stat label="Used margin" value={inr(eq.utilised?.debits)} />
          </Row>
        </Card>
      )}

      {status?.logged_in && (
        <Pressable onPress={() => navigation.navigate("Zerodha")}>
          <Card title="📒 Zerodha account" right={<Text style={{ color: C.blue, fontWeight: "800" }}>Open ›</Text>}>
            <Hint>Every trade today — by Kite Algo or by hand in the Kite app — with buy/sell amounts, P&L and charges. Past days are kept from now on.</Hint>
          </Card>
        </Pressable>
      )}

      <Btn label="✦ AI Strategy Finder" kind="ghost" onPress={() => navigation.navigate("Finder")} />

      <MorningCard enabled={!!status?.logged_in} />

    </Screen>
  );
}
