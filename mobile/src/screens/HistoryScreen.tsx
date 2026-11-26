import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Pressable, Text, View } from "react-native";
import { api, todayKey, type AnalysisItem, type PositionSnap } from "../api";
import { useMode } from "../mode";
import ReportCard from "../ReportCard";
import { Badge, C, Card, Chips, DayBars, Empty, ErrorText, Label, ListRow, PnlHero, Screen, TabBar, pnlColor, rupee, s, when } from "../ui";
import { AutoPerformance } from "./AutoTradeScreen";

/**
 * History — everything from EARLIER days lives here; Home / Auto / Trade show today only.
 *  P&L          day-by-day net with a bar chart and week / month / all totals
 *  Trades       every past closed trade, grouped by day, filter by who placed it
 *  Performance  Auto Trade's long-run results (scorecard, costs, execution quality)
 *  Saved        past AI analyses, backtests, reports, clean-up
 */
type Seg = "pnl" | "trades" | "performance" | "saved";
type Day = { day: string; trades: number; wins: number; net: number; by: Record<string, number> };

const SRC: Record<string, string> = { auto: "Auto", swing: "Swing", manual: "You" };
const EXIT: Record<string, string> = {
  target: "Target", stop_loss: "Stop-loss", trailing_stop: "Trailing stop", profit_lock: "Profit lock", quick_target: "Quick target",
  time_cutoff: "Day end", time_stop: "Time stop", signal_exit: "Signal flipped", manual_close: "You sold", kill_switch: "Kill switch",
  trend_down: "Trend down", roll: "Rolled", crash_guard: "Crash guard",
};
const exitLabel = (r?: string | null) => (r ? EXIT[r] ?? (r.startsWith("ai_") ? "AI exit" : r.replace(/_/g, " ")) : "");
const dayTitle = (k: string) => {
  const y = new Date(Date.now() - 86400000).toLocaleDateString("en-CA");
  if (k === y) return "Yesterday";
  const d = new Date(k + "T12:00:00");
  return isNaN(d.getTime()) ? k : d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
};
const startOf = (range: "week" | "month" | "all") => {
  const d = new Date();
  if (range === "week") { d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d.toLocaleDateString("en-CA"); }
  if (range === "month") return d.toLocaleDateString("en-CA").slice(0, 8) + "01";
  return "0000";
};

export default function HistoryScreen({ navigation, route }: any) {
  const { mode: appMode } = useMode();
  const [seg, setSeg] = useState<Seg>("pnl");
  const [mode, setMode] = useState<"paper" | "live">(appMode);
  useEffect(() => setMode(appMode), [appMode]);
  useEffect(() => { if (route?.params?.seg) setSeg(route.params.seg); }, [route?.params?.seg, route?.params?.nonce]);
  return (
    <Screen>
      <TabBar value={seg} onChange={setSeg} tabs={[
        { key: "pnl", label: "P&L" }, { key: "trades", label: "Trades" },
        { key: "performance", label: "Performance" }, { key: "saved", label: "Saved" }]} />
      {seg !== "saved" && (
        <Chips options={["paper", "live"] as const} value={mode} onChange={setMode} labels={{ paper: "📝 Paper", live: "💰 Live" }} />
      )}
      {seg === "pnl" && <PnlDays mode={mode} navigation={navigation} />}
      {seg === "trades" && <PastTrades mode={mode} navigation={navigation} />}
      {seg === "performance" && <Performance mode={mode} />}
      {seg === "saved" && <Saved navigation={navigation} />}
    </Screen>
  );
}

/* ---------- P&L by day ---------- */

function PnlDays({ mode, navigation }: { mode: "paper" | "live"; navigation: any }) {
  const [days, setDays] = useState<Day[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [range, setRange] = useState<"week" | "month" | "all">("month");
  const [open, setOpen] = useState<string | null>(null);
  const [trades, setTrades] = useState<PositionSnap[]>([]);
  const load = useCallback(async () => {
    try { setDays((await api.pnlDays(mode)).filter((d) => d.day && d.day < todayKey())); setErr(null); }
    catch (e: any) { setErr(e.message); }
  }, [mode]);
  useFocusEffect(useCallback(() => { load(); }, [load]));
  const shown = useMemo(() => (days ?? []).filter((d) => d.day >= startOf(range)), [days, range]);
  const net = shown.reduce((a, d) => a + d.net, 0);
  const trd = shown.reduce((a, d) => a + d.trades, 0);
  const won = shown.reduce((a, d) => a + d.wins, 0);
  const green = shown.filter((d) => d.net > 0).length;
  const best = shown.length ? Math.max(...shown.map((d) => d.net)) : null;
  const worst = shown.length ? Math.min(...shown.map((d) => d.net)) : null;
  const toggle = async (k: string) => {
    if (open === k) { setOpen(null); return; }
    setOpen(k); setTrades([]);
    try { setTrades((await api.positionsDay(mode, k)).filter((p) => p.state === "POSITION_CLOSED")); } catch {}
  };

  return (<>
    <Chips options={["week", "month", "all"] as const} value={range} onChange={setRange}
      labels={{ week: "This week", month: "This month", all: "All time" }} />
    <ErrorText msg={err} />
    <PnlHero title={`${range === "all" ? "All time" : range === "week" ? "This week" : "This month"} · ${mode}`} value={net}
      caption={shown.length ? `${shown.length} trading day${shown.length === 1 ? "" : "s"} · ${green} green · ${shown.length - green} red · today not included` : "Earlier days only — today is on Home"}
      items={[
        { label: "Trades", value: String(trd) },
        { label: "Win rate", value: trd ? `${Math.round((won / trd) * 100)}%` : "—" },
        { label: "Best / worst day", value: best == null ? "—" : `${rupee(best, true)} / ${rupee(worst, true)}` },
      ]} />
    {shown.length > 1 && (
      <Card>
        <Label text="Daily P&L" right={<Text style={{ color: C.dim, fontSize: 11 }}>oldest → newest</Text>} />
        <DayBars values={[...shown].reverse().slice(-40).map((d) => d.net)} />
      </Card>
    )}
    <Card>
      {days === null ? <Text style={s.muted}>Loading…</Text> : shown.length === 0 ? (
        <Empty icon="📅" text="No closed trades in this period" sub="Trades show up here from the day after they close." />
      ) : shown.map((d, i) => (
        <View key={d.day}>
          <ListRow first={i === 0} title={dayTitle(d.day)} onPress={() => toggle(d.day)}
            sub={`${d.trades} trade${d.trades === 1 ? "" : "s"} · ${d.wins} won · ${Object.entries(d.by).map(([k, v]) => `${SRC[k] ?? k} ${rupee(v, true)}`).join(" · ")}`}
            value={rupee(d.net, true)} valueColor={pnlColor(d.net)} valueSub={open === d.day ? "▾" : "▸"} />
          {open === d.day && (
            <View style={{ paddingLeft: 10, borderLeftWidth: 2, borderLeftColor: C.border, marginBottom: 8 }}>
              {trades.length === 0 ? <Text style={[s.muted, { paddingVertical: 8 }]}>Loading trades…</Text>
                : trades.map((p, j) => <TradeLine key={p.id} p={p} first={j === 0} onPress={() => navigation.navigate("Position", { id: p.id })} />)}
            </View>
          )}
        </View>
      ))}
    </Card>
  </>);
}

/* ---------- every past trade ---------- */

function PastTrades({ mode, navigation }: { mode: "paper" | "live"; navigation: any }) {
  const [src, setSrc] = useState<"all" | "auto" | "swing" | "manual">("all");
  const [list, setList] = useState<PositionSnap[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setList(await api.positionsPast(mode, src === "all" ? undefined : src)); setErr(null); }
    catch (e: any) {
      // older server without /positions/past: use the full list and drop today
      try {
        const all = await api.positionsList(mode);
        setList(all.filter((p) => p.state === "POSITION_CLOSED" && String(p.exit_time ?? p.entry_time ?? "").slice(0, 10) < todayKey()
          && (src === "all" || String(p.spec.source ?? "manual").split(":")[0] === src)));
        setErr(null);
      } catch { setErr(e.message); }
    }
  }, [mode, src]);
  useFocusEffect(useCallback(() => { load(); }, [load]));
  const groups = useMemo(() => {
    const g: { day: string; items: PositionSnap[]; net: number }[] = [];
    for (const p of list ?? []) {
      const k = String(p.exit_time ?? p.entry_time ?? "").slice(0, 10);
      let row = g[g.length - 1];
      if (!row || row.day !== k) { row = { day: k, items: [], net: 0 }; g.push(row); }
      row.items.push(p); row.net += p.net ?? 0;
    }
    return g;
  }, [list]);
  const net = (list ?? []).reduce((a, p) => a + (p.net ?? 0), 0);
  const wins = (list ?? []).filter((p) => (p.net ?? 0) > 0).length;

  return (<>
    <Chips options={["all", "auto", "swing", "manual"] as const} value={src} onChange={setSrc}
      labels={{ all: "All", auto: "⚡ Auto", swing: "📈 Swing", manual: "✋ Mine" }} />
    <ErrorText msg={err} />
    {list && list.length > 0 && (
      <View style={{ flexDirection: "row", alignItems: "center", paddingHorizontal: 4 }}>
        <Text style={[s.muted, { flex: 1 }]}>{list.length} trades · {wins} won · {list.length - wins} lost</Text>
        <Text style={{ color: pnlColor(net), fontWeight: "900" }}>{rupee(net, true)}</Text>
      </View>
    )}
    {list === null ? <Card><Text style={s.muted}>Loading…</Text></Card> : groups.length === 0 ? (
      <Card><Empty icon="🗂" text="No past trades" sub="Closed trades move here the day after." /></Card>
    ) : groups.map((g) => (
      <Card key={g.day}>
        <Label text={dayTitle(g.day)} right={<Text style={{ color: pnlColor(g.net), fontWeight: "800" }}>{rupee(g.net, true)}</Text>} />
        {g.items.map((p, j) => <TradeLine key={p.id} p={p} first={j === 0} onPress={() => navigation.navigate("Position", { id: p.id })} />)}
      </Card>
    ))}
  </>);
}

function TradeLine({ p, first, onPress }: { p: PositionSnap; first?: boolean; onPress: () => void }) {
  const src = String(p.spec.source ?? "manual").split(":")[0];
  const t = (x?: string | null) => (x ? x.slice(11, 16) : "");
  return (
    <ListRow first={first} onPress={onPress}
      left={<Text style={{ color: C.dim, width: 40, fontSize: 12, fontVariant: ["tabular-nums"] }}>{t(p.entry_time)}</Text>}
      title={`${p.spec.side > 0 ? "" : "SHORT "}${p.spec.tradingsymbol}`}
      sub={`${SRC[src] ?? src} · ${p.filled_qty || p.spec.qty} qty · ${p.entry_price ?? "—"} → ${p.exit_price ?? "—"} · ${exitLabel(p.exit_reason)}`}
      value={rupee(p.net, true)} valueColor={pnlColor(p.net)} valueSub={p.exit_time ? `out ${t(p.exit_time)}` : undefined} />
  );
}

/* ---------- long-run performance ---------- */

function Performance({ mode }: { mode: "paper" | "live" }) {
  const { app } = useMode();
  const [stats, setStats] = useState<Record<string, any> | null>(null);
  const [swing, setSwing] = useState<Record<string, any> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const [st, sw] = await Promise.all([
        app?.auto_id ? api.analytics(`auto:${app.auto_id}`) : api.analytics("auto", mode),
        app?.swing_id ? api.swing(app.swing_id).catch(() => null) : Promise.resolve(null),
      ]);
      setStats(st); setSwing(sw); setErr(null);
    } catch (e: any) { setErr(e.message); }
  }, [app?.auto_id, app?.swing_id, mode]);
  useFocusEffect(useCallback(() => { load(); }, [load]));
  const swingDone = (swing?.trades ?? []).filter((t: any) => t.exit_time);
  return (<>
    <ErrorText msg={err} />
    {!stats ? <Card><Text style={s.muted}>Loading…</Text></Card> : <AutoPerformance stats={stats} />}
    {swing && (
      <Card title={`📈 Swing · ${swing.config?.index ?? ""}`}>
        <View style={{ flexDirection: "row" }}>
          <Text style={[s.muted, { flex: 1 }]}>{swingDone.length} closed trades · money {rupee(swing.equity)}</Text>
          <Text style={{ color: pnlColor(swing.realized), fontWeight: "900" }}>{rupee(swing.realized, true)}</Text>
        </View>
        {swingDone.slice(0, 20).map((t: any, i: number) => (
          <ListRow key={t.id} first={i === 0} title={t.tradingsymbol ?? "Swing trade"}
            sub={`${t.entry_time ? when(t.entry_time) : "—"} → ${when(t.exit_time)} · ${exitLabel(t.exit_reason)}`}
            value={rupee(t.net, true)} valueColor={pnlColor(t.net)} />
        ))}
      </Card>
    )}
  </>);
}

/* ---------- saved work + reports + clean-up ---------- */

function Saved({ navigation }: { navigation: any }) {
  const [an, setAn] = useState<AnalysisItem[] | null>(null);
  const [bt, setBt] = useState<Awaited<ReturnType<typeof api.backtests>> | null>(null);
  const load = useCallback(async () => {
    const [a, b] = await Promise.all([api.analyses().catch(() => []), api.backtests().catch(() => [])]);
    setAn(a); setBt(b);
  }, []);
  useFocusEffect(useCallback(() => { load(); }, [load]));
  const clearTrades = (mode: "paper" | "live") => Alert.alert(`Clear ${mode} trade history?`,
    "Removes CLOSED trades from the app (not from Zerodha). Open positions are always kept.", [
      { text: "Cancel", style: "cancel" },
      { text: "Clear", style: "destructive", onPress: async () => {
        try { Alert.alert("Cleared", `${(await api.clearPositions(mode)).deleted} trade(s) removed.`); } catch (e: any) { Alert.alert("Clear failed", e.message); }
      } },
    ]);
  return (<>
    <Card title={`✦ AI analyses${an?.length ? ` · ${an.length}` : ""}`} right={an?.length ? (
      <Pressable hitSlop={10} onPress={() => Alert.alert("Clear all past analyses?", "", [
        { text: "Cancel", style: "cancel" },
        { text: "Clear all", style: "destructive", onPress: async () => { await api.clearAnalyses().catch(() => {}); load(); } }])}>
        <Text style={{ color: C.red, fontWeight: "700" }}>Clear</Text></Pressable>) : null}>
      {an === null ? <Text style={s.muted}>Loading…</Text> : an.length === 0 ? <Empty icon="✦" text="No saved analyses" /> : an.map((it, i) => (
        <ListRow key={it.id} first={i === 0} title={it.title} sub={`${when(it.created)}${it.top ? ` · #1 ${it.top}: ${it.top_action}` : ""}`}
          onPress={() => navigation.navigate("Finder", { openId: it.id })}
          left={<Badge text={it.verdict} color={it.verdict === "BULLISH" ? C.green : it.verdict === "BEARISH" ? C.red : C.dim} />} />
      ))}
    </Card>
    <Card title={`🧪 Backtests${bt?.length ? ` · ${bt.length}` : ""}`}>
      {bt === null ? <Text style={s.muted}>Loading…</Text> : bt.length === 0 ? <Empty icon="🧪" text="No saved backtests" /> : bt.map((b, i) => (
        <ListRow key={b.id} first={i === 0} title={b.title} sub={when(b.created)}
          value={`${b.metrics?.total_return_pct ?? "—"}%`} valueColor={pnlColor(b.metrics?.total_return_pct)}
          onPress={() => navigation.navigate("BacktestResult", { id: b.id })} />
      ))}
    </Card>
    <ReportCard source="all" mode="all" />
    <Card title="📒 Zerodha account">
      <ListRow first title="Trades & charges by day" sub="Everything in your Kite account, incl. manual trades"
        value="›" valueColor={C.blue} onPress={() => navigation.navigate("Zerodha")} />
    </Card>
    <Card title="Clean up">
      <ListRow first title="Clear paper trade history" sub="Closed paper trades only" value="›" valueColor={C.red} onPress={() => clearTrades("paper")} />
      <ListRow title="Clear live trade history" sub="Removes them from the app, not from Zerodha" value="›" valueColor={C.red} onPress={() => clearTrades("live")} />
    </Card>
  </>);
}
