import React, { useCallback, useRef, useState } from "react";
import { Pressable, RefreshControl, Text, View } from "react-native";
import { api, currentTarget, getSettings, todayKey, type AuthStatus, type AutoSnap, type PositionSnap } from "../api";
import { useMode } from "../mode";
import { isLive } from "./AutoTradeScreen";
import { PositionRow } from "./TradeTicket";
import MorningCard from "../MorningCard";
import { IndexTicker, useIndexMoves } from "../IndexTicker";
import { Badge, C, Card, Empty, Label, ListRow, PnlHero, QuickActions, Screen, pnlColor, rupee, s } from "../ui";
import { usePollOnFocus } from "../usePoll";

/**
 * Home = TODAY only (like a broker app's dashboard):
 *   market strip → today's P&L → open positions → Auto status → today's closed trades → shortcuts.
 * Earlier days are in the History tab.
 */
const isOpen = (p: PositionSnap) => !["POSITION_CLOSED", "ORDER_REJECTED", "ORDER_CANCELLED"].includes(p.state);
const hhmm = (t?: string | null) => (t ? t.slice(11, 16) : "");

export default function HomeScreen({ navigation }: any) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [margins, setMargins] = useState<any>(null);
  const { mode, auto } = useMode();
  const moves = useIndexMoves(true);
  const [positions, setPositions] = useState<PositionSnap[]>([]);
  const [autos, setAutos] = useState<AutoSnap[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [target, setTarget] = useState<"phone" | "pc">("phone");
  const [hasPc, setHasPc] = useState(false);
  const synced = useRef(false);

  const load = useCallback(async () => {
    const cfg = await getSettings();
    if (!cfg.url || !cfg.token) { setErr("Open Settings and enter your server URL and app token."); return; }
    const t = await currentTarget();
    setTarget(t); setHasPc(!!cfg.pcUrl);
    try {
      let st = await api.authStatus();
      if (t === "pc" && !st.logged_in && !synced.current) {
        // logged in on the phone this morning? copy that login to the PC (once per visit)
        synced.current = true;
        const ph = await api.authStatusOn("phone").catch(() => null);
        if (ph?.logged_in) { await api.syncSessionToPc().catch(() => null); st = await api.authStatus(); }
      }
      setStatus(st); setErr(null);
      // independent calls in parallel
      const [pos, au, mg] = await Promise.all([
        api.positionsToday(mode).catch(() => [] as PositionSnap[]),
        auto ? api.autos().catch(() => [] as AutoSnap[]) : Promise.resolve([] as AutoSnap[]),
        st.logged_in && mode === "live" ? api.margins().catch(() => null) : Promise.resolve(null),
      ]);
      setPositions(pos);
      setAutos(au.filter((a) => a.config.mode === mode && (a.enabled || a.position)));
      setMargins(mg);
    } catch (e: any) { setErr(e.message); }
  }, [mode, auto]);
  usePollOnFocus(load, 10_000);

  const eq = margins?.equity;
  const today = todayKey();
  const open = positions.filter(isOpen);
  const closed = positions.filter((p) => p.state === "POSITION_CLOSED" && String(p.exit_time ?? p.entry_time ?? "").startsWith(today));
  const realised = closed.reduce((a, p) => a + (p.net ?? 0), 0);
  const unrealised = open.reduce((a, p) => a + (p.unrealized ?? 0), 0);
  const wins = closed.filter((p) => (p.net ?? 0) > 0).length;
  const loggedOut = status && !status.logged_in;

  return (
    <Screen refresh={<RefreshControl refreshing={refreshing} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} tintColor={C.dim} />}>
      {(err || loggedOut) && (
        <Pressable onPress={() => navigation.navigate("Settings")}
          style={{ padding: 12, borderRadius: 12, borderWidth: 1, borderColor: C.amber, backgroundColor: C.amber + "18" }}>
          <Text style={{ color: C.amber, fontWeight: "800" }}>{err ? "⚠ Server not reachable" : "🔑 Kite logged out"} — tap to fix</Text>
          <Text style={s.muted}>{err ?? "Log in once each morning in Settings → Zerodha."}</Text>
        </Pressable>
      )}

      {status?.logged_in && (
        <IndexTicker moves={moves} onPress={(k) => {
          const m = { NIFTY: ["NSE", "NIFTY 50"], BANKNIFTY: ["NSE", "NIFTY BANK"], SENSEX: ["BSE", "SENSEX"] }[k];
          navigation.navigate("Quote", { exchange: m[0], tradingsymbol: m[1] });
        }} />
      )}

      {/* today's money */}
      <PnlHero title={`Today's P&L · ${mode === "live" ? "Live" : "Paper"}`} value={realised + unrealised}
        caption={`${open.length} open · ${closed.length} closed today${closed.length ? ` · ${wins} won, ${closed.length - wins} lost` : ""}`}
        right={<View style={{ flexDirection: "row", gap: 6 }}>
          {hasPc && <Badge text={target === "pc" ? "PC" : "PHONE"} color={target === "pc" ? C.amber : C.blue} />}
          {mode === "live" && status && <Badge text={status.live_trading_enabled ? "LIVE ON" : "LIVE OFF"} color={status.live_trading_enabled ? C.red : C.amber} />}
        </View>}
        items={[
          { label: "Realised", value: rupee(realised, true), color: pnlColor(realised) },
          { label: "Unrealised", value: rupee(unrealised, true), color: pnlColor(unrealised) },
          ...(mode === "live" && eq ? [{ label: "Available funds", value: rupee(eq.net) }] : [{ label: "Trades", value: String(open.length + closed.length) }]),
        ]} />

      {/* open positions */}
      <Card>
        <Label text={`Open positions · ${open.length}`} right={
          <Text onPress={() => navigation.navigate("Trade")} style={{ color: C.blue, fontWeight: "800" }}>Trade ›</Text>} />
        {open.length === 0 ? <Empty icon="◎" text="No open positions" sub={auto ? "Auto will open one when its signal fires." : "Place one from the Trade tab."} />
          : open.map((p) => <PositionRow key={p.id} p={p} onPress={() => navigation.navigate("Position", { id: p.id })} />)}
      </Card>

      {/* auto status */}
      {auto && (
        <Card>
          <Label text="Auto trading" right={<Text onPress={() => navigation.navigate("Auto")} style={{ color: C.blue, fontWeight: "800" }}>Open ›</Text>} />
          {autos.length === 0 ? <Empty icon="⚡" text="No Autopilot running" sub="Open the Auto tab to switch it on." />
            : [...autos].sort((x, y) => Number(isLive(y)) - Number(isLive(x))).map((a, i) => (
              <ListRow key={a.id} first={i === 0} onPress={() => navigation.navigate("AutoDetail", { id: a.id })}
                left={<View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: a.enabled ? (a.paused ? C.amber : C.green) : C.dim }} />}
                title={`${a.config.index} · ${a.config.strategy_source === "ai" ? a.ai_pick?.name ?? "AI pick" : a.config.strategy}`}
                sub={a.message || a.state.replace(/_/g, " ").toLowerCase()}
                value={rupee(a.realized_pnl + a.unrealized_pnl, true)} valueColor={pnlColor(a.realized_pnl + a.unrealized_pnl)}
                valueSub={`${a.trades_today} trade${a.trades_today === 1 ? "" : "s"}`} />
            ))}
        </Card>
      )}

      {/* today's closed trades */}
      <Card>
        <Label text={`Closed today · ${closed.length}`} right={closed.length ? <Text style={{ color: pnlColor(realised), fontWeight: "900" }}>{rupee(realised, true)}</Text> : undefined} />
        {closed.length === 0 ? <Empty icon="✓" text="Nothing closed yet today" /> : closed.slice(0, 8).map((p, i) => (
          <ListRow key={p.id} first={i === 0} onPress={() => navigation.navigate("Position", { id: p.id })}
            left={<Text style={{ color: C.dim, width: 40, fontSize: 12 }}>{hhmm(p.entry_time)}</Text>}
            title={p.spec.tradingsymbol} sub={`${String(p.spec.source ?? "manual").split(":")[0]} · ${p.entry_price ?? "—"} → ${p.exit_price ?? "—"}`}
            value={rupee(p.net, true)} valueColor={pnlColor(p.net)} />
        ))}
        {closed.length > 8 && <Text onPress={() => navigation.navigate("Trade")} style={{ color: C.blue, fontWeight: "700", paddingTop: 8 }}>All {closed.length} in Trade ›</Text>}
      </Card>

      <QuickActions items={[
        { icon: "✦", label: "AI Finder", onPress: () => navigation.navigate("Finder") },
        { icon: "📈", label: "Swing", onPress: () => navigation.navigate("Swing") },
        { icon: "📒", label: "Zerodha", onPress: () => navigation.navigate("Zerodha") },
        { icon: "🕘", label: "History", onPress: () => navigation.navigate("History") },
      ]} />

      <MorningCard enabled={!!status?.logged_in} />
      {mode === "live" && status && !status.live_trading_enabled && (
        <Text style={[s.muted, { color: C.amber }]}>Live orders need a static-IP server with LIVE_TRADING_ENABLED=true.</Text>
      )}
    </Screen>
  );
}
