import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useEffect, useState } from "react";
import { Alert, Pressable, Text, View } from "react-native";
import { api, DEFAULT_RISK, inr, type PositionSnap, type RiskSettings } from "../api";
import type { TicketPrefill } from "../mode";
import { ContractPreview, defaultSelection, OptionPicker, RiskForm, strikeMethodParam, type OptionSelection } from "../OptionControls";
import { Badge, Btn, C, Card, Chips, ErrorText, Row, s, Toggle, pnlColor, when, contractText, Hint } from "../ui";

export const stateColor = (st: string) =>
  st === "POSITION_OPEN" || st === "PARTIALLY_FILLED" ? C.green
    : st === "POSITION_CLOSED" ? C.dim
    : st.includes("FAIL") || st.includes("REJECT") || st === "RECONCILIATION_REQUIRED" ? C.red : C.amber;

export function PositionRow({ p, onPress, onClose }: { p: PositionSnap; onPress: () => void; onClose?: () => void }) {
  const open = p.state === "POSITION_OPEN" || p.state === "PARTIALLY_FILLED" || p.state === "EXIT_FAILED";
  const long = p.spec.side > 0;
  const pnl = open ? p.unrealized : p.net;
  return (
    <Pressable onPress={onPress} style={{ paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: C.border, gap: 4 }}>
      <View style={s.row}>
        <Badge text={long ? "LONG" : "SHORT"} color={long ? C.blue : C.amber} />
        <Text style={[s.text, { flex: 1, fontWeight: "700" }]} numberOfLines={2}>{contractText({ tradingsymbol: p.spec.tradingsymbol, strike: p.spec.strike, expiry: p.spec.expiry, option_type: p.spec.instrument_type })}</Text>
        <Badge text={p.state.replace(/_/g, " ")} color={stateColor(p.state)} />
      </View>
      <Text style={[s.muted, { fontWeight: "700" }]}>
        📅 {when(p.entry_time ?? p.created)}{p.exit_time ? ` → ${when(p.exit_time).split(" ").slice(-1)[0]}` : ""}
      </Text>
      <Text style={s.muted}>
        {p.filled_qty || p.spec.qty} qty · entry {p.entry_price ?? "—"} · SL {p.stop ?? "—"} · T {p.target ?? "—"} ·{" "}
        <Text style={{ color: pnlColor(pnl) }}>{inr(pnl, 2)}</Text>
      </Text>
      {["POSITION_OPEN", "PARTIALLY_FILLED", "EXIT_FAILED"].includes(p.state) && p.ltp != null && p.entry_price ? (
        <Text style={{ color: pnlColor((p.ltp - p.entry_price) * p.spec.side), fontWeight: "800", fontSize: 15 }}>
          Now ₹{p.ltp.toFixed(2)}  {(p.ltp - p.entry_price) * p.spec.side >= 0 ? "▲ +" : "▼ "}{((p.ltp - p.entry_price) * p.spec.side).toFixed(2)} from entry
        </Text>
      ) : null}
      {p.message && p.state !== "POSITION_CLOSED" ? <Text style={{ color: stateColor(p.state), fontSize: 12 }}>{p.message}</Text> : null}
      {open && onClose && (
        <Btn label={long ? "SELL to close" : "BUY to close"} kind="danger" onPress={onClose} />
      )}
    </Pressable>
  );
}

export default function TradeTicket({ mode, navigation, liveEnabled = false, prefill }: {
  mode: "paper" | "live"; navigation: any; liveEnabled?: boolean; prefill?: TicketPrefill;
}) {
  const [sel, setSel] = useState<OptionSelection>(defaultSelection(false));
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [allowSell, setAllowSell] = useState(false);
  const [product, setProduct] = useState<"MIS" | "NRML">("MIS");
  const [risk, setRisk] = useState<RiskSettings>({ ...DEFAULT_RISK });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [positions, setPositions] = useState<PositionSnap[]>([]);
  const [note, setNote] = useState<string | null>(null);

  // AI finder suggestion -> BUY the suggested CE/PE (ATM) on that index; user still reviews and places it.
  useEffect(() => {
    if (!prefill || Date.now() - prefill.nonce > 60_000) return; // only fresh suggestions, not on later revisits
    setSel((cur) => cur.index === prefill.index
      ? { ...cur, optionType: prefill.optionType, method: "ATM", strike: null }
      : { ...cur, index: prefill.index, optionType: prefill.optionType, method: "ATM", strike: null, expiry: "" });
    setSide("BUY");
    setNote(prefill.note ?? `From AI Finder: BUY ${prefill.index} ${prefill.optionType} (ATM)`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill?.nonce]);

  const load = useCallback(async () => {
    try {
      setPositions(await api.positionsList(mode));
    } catch (e: any) {
      setErr(e.message);
    }
  }, [mode]);
  useFocusEffect(useCallback(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]));

  const blocked = mode === "live" && !liveEnabled;
  // duplicate orders in the same contract are rejected by the server (prevents accidental reversal)

  async function place(confirm = "") {
    setErr(null);
    setBusy(true);
    try {
      const p = await api.optionOrder({
        mode, index: sel.index, expiry: sel.expiry, option_type: sel.optionType, strike_method: strikeMethodParam(sel),
        strike_offset: sel.offset, lots: sel.lots, side, product, risk, confirm_live: confirm, allow_option_selling: allowSell,
      });
      navigation.navigate("Position", { id: p.id });
      load();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  function onPlace() {
    if (!sel.expiry) return setErr("Pick an expiry");
    if (side === "SELL" && !allowSell) return setErr("Turn on 'Allow selling to open' first");
    if (mode === "paper") return place();
    Alert.alert("Place REAL order?",
      `${side === "BUY" ? "BUY to open" : "SELL to open"} ${sel.index} ${sel.optionType} · ${sel.lots} lot(s) · ${product}\n` +
      "A protective stop-loss order is placed at Zerodha right after the fill.", [
        { text: "Cancel", style: "cancel" },
        { text: "Place real order", style: "destructive", onPress: () => place("I UNDERSTAND REAL MONEY") },
      ]);
  }

  function close(p: PositionSnap) {
    Alert.alert(p.spec.side > 0 ? "SELL to close?" : "BUY to close?", `${p.spec.tradingsymbol} · ${p.filled_qty} qty at market`, [
      { text: "Cancel", style: "cancel" },
      { text: "Close position", style: "destructive", onPress: async () => {
        try { await api.closePosition(p.id); load(); } catch (e: any) { Alert.alert("Close failed", e.message); }
      } },
    ]);
  }

  const active = positions.filter((p) => p.state !== "POSITION_CLOSED" && p.state !== "ORDER_REJECTED");
  return (
    <>
      {blocked && (
        <Card><Text style={{ color: C.amber }}>
          Live orders are OFF on your server. They need a static IP registered with Zerodha (your phone doesn't have one) and
          LIVE_TRADING_ENABLED=true in backend/.env. Use the Paper tab meanwhile.
        </Text></Card>
      )}
      <Card title={`Open ${mode === "live" ? "live" : "paper"} positions (${active.length})`}>
        {active.length === 0 ? <Text style={s.muted}>None.</Text> : active.map((p) => (
          <PositionRow key={p.id} p={p} onPress={() => navigation.navigate("Position", { id: p.id })} onClose={() => close(p)} />
        ))}
      </Card>

      {note && (
        <Card style={{ borderColor: C.blue }}>
          <View style={s.row}>
            <Text style={{ color: C.blue, fontWeight: "700", flex: 1 }}>✦ {note}</Text>
            <Pressable onPress={() => setNote(null)} hitSlop={10}><Text style={{ color: C.dim, fontSize: 16 }}>✕</Text></Pressable>
          </View>
          <Text style={s.muted}>Check expiry, strike, lots and stop-loss below, then place the order.</Text>
        </Card>
      )}
      <OptionPicker value={sel} onChange={setSel} title={mode === "live" ? "New LIVE option order" : "New paper option order"} />
      <Card title="Order">
        <Row>
          <View style={{ flex: 1 }}><Btn label="BUY to open" kind={side === "BUY" ? "primary" : "ghost"} onPress={() => setSide("BUY")} /></View>
          <View style={{ flex: 1 }}><Btn label="SELL to open" kind={side === "SELL" ? "danger" : "ghost"} onPress={() => setSide("SELL")} /></View>
        </Row>
        {side === "SELL" && <Toggle label="Allow selling to open (short options, high risk)" value={allowSell} onChange={setAllowSell} />}
        <Chips options={["MIS", "NRML"] as const} value={product} onChange={setProduct} labels={{ MIS: "MIS · intraday", NRML: "NRML · overnight" }} />
        <Hint>To exit an open position use its "SELL/BUY to close" button above — a new order in the same contract is blocked.</Hint>
      </Card>
      <RiskForm value={risk} onChange={setRisk} />
      <ContractPreview sel={sel} side={side} product={product} risk={risk} />
      <ErrorText msg={err} />
      <Btn label={mode === "live" ? `Place LIVE ${side === "BUY" ? "BUY" : "SELL"} order` : `Place paper ${side === "BUY" ? "BUY" : "SELL"}`}
        kind={mode === "live" ? "warn" : "primary"} onPress={onPlace} loading={busy} disabled={blocked} />

      <DayHistory mode={mode} navigation={navigation} />
    </>
  );
}

const dayLabel = (k: string) => {
  const today = new Date().toLocaleDateString("en-CA");
  const y = new Date(Date.now() - 86400000).toLocaleDateString("en-CA");
  if (k === today) return "Today";
  if (k === y) return "Yesterday";
  const d = new Date(k + "T12:00:00");
  return isNaN(d.getTime()) ? k : d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
};
const SRC: Record<string, string> = { auto: "Auto", swing: "Swing", manual: "You" };

/** Closed trades' P&L by day: one line per day (trades · wins · net), tap a day to see who made it and its trades. */
export function DayHistory({ mode, navigation, reload }: { mode: "paper" | "live"; navigation: any; reload?: number }) {
  const [days, setDays] = useState<Awaited<ReturnType<typeof api.pnlDays>>>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [trades, setTrades] = useState<PositionSnap[]>([]);
  const [more, setMore] = useState(false);
  useFocusEffect(useCallback(() => { api.pnlDays(mode).then(setDays).catch(() => {}); }, [mode, reload]));
  const toggle = async (k: string) => {
    if (open === k) { setOpen(null); return; }
    setOpen(k); setTrades([]);
    try { setTrades((await api.positionsDay(mode, k)).filter((p) => p.state === "POSITION_CLOSED")); } catch {}
  };
  if (!days.length) return null;
  const shown = more ? days : days.slice(0, 7);
  return (
    <Card title="P&L by day">
      {shown.map((d) => (
        <View key={d.day}>
          <Pressable onPress={() => toggle(d.day)}
            style={{ flexDirection: "row", alignItems: "center", paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: C.border, gap: 8 }}>
            <Text style={{ color: C.text, fontWeight: "800", width: 96 }}>{dayLabel(d.day)}</Text>
            <Text style={[s.muted, { flex: 1 }]}>{d.trades} trade{d.trades === 1 ? "" : "s"} · {d.wins} won</Text>
            <Text style={{ color: pnlColor(d.net), fontWeight: "800" }}>{inr(d.net, 0)}</Text>
            <Text style={{ color: C.dim }}>{open === d.day ? "▾" : "▸"}</Text>
          </Pressable>
          {open === d.day && (
            <View style={{ paddingTop: 6 }}>
              <Text style={s.muted}>{Object.entries(d.by).map(([k, v]) => `${SRC[k] ?? k} ${inr(v, 0)}`).join(" · ")}</Text>
              {trades.map((p) => <PositionRow key={p.id} p={p} onPress={() => navigation.navigate("Position", { id: p.id })} />)}
            </View>
          )}
        </View>
      ))}
      {days.length > 7 && (
        <Text onPress={() => setMore(!more)} style={{ color: C.blue, fontWeight: "700", paddingTop: 8 }}>
          {more ? "Show last 7 days" : `Show all ${days.length} days`}</Text>
      )}
    </Card>
  );
}
