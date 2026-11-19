import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useState } from "react";
import { Alert, Pressable, Text, View } from "react-native";
import { api, inr, type SessionSnapshot } from "../api";
import { INTRADAY, numericParams, StrategyForm, type StrategyFormValue } from "../StrategyForm";
import { Badge, Btn, C, Card, Chips, ErrorText, Field, Row, Toggle, pnlColor, s, Hint } from "../ui";

type Mode = "paper" | "replay" | "live";
const iso = (d: Date) => d.toISOString().slice(0, 10);
const daysAgo = (n: number) => iso(new Date(Date.now() - n * 86400_000));

export const statusColor = (st: string) =>
  st === "running" ? C.green : st === "halted" || st === "error" ? C.red : st === "finished" ? C.blue : C.dim;

/** Stock/futures strategy sessions (paper, replay, live). Embedded in the Paper and Live tabs. */
export function SessionsPanel({ navigation, modes }: { navigation: any; modes: readonly Mode[] }) {
  const [sessions, setSessions] = useState<SessionSnapshot[]>([]);
  const [form, setForm] = useState<StrategyFormValue>({ instrument: null, strategy: null, params: {}, interval: "5minute" });
  const [mode, setMode] = useState<Mode>(modes[0]);
  const [qty, setQty] = useState("1");
  const [product, setProduct] = useState<"MIS" | "NRML" | "CNC">("MIS");
  const [sl, setSl] = useState("1");
  const [tgt, setTgt] = useState("2");
  const [maxTrades, setMaxTrades] = useState("5");
  const [maxLoss, setMaxLoss] = useState("1000");
  const [allowShort, setAllowShort] = useState(true);
  const [rFrom, setRFrom] = useState(daysAgo(7));
  const [rTo, setRTo] = useState(daysAgo(1));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [liveEnabled, setLiveEnabled] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.sessions();
      setSessions(r.active);
      const st = await api.authStatus();
      setLiveEnabled(st.live_trading_enabled);
    } catch (e: any) {
      setErr(e.message);
    }
  }, []);

  useFocusEffect(useCallback(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]));

  const isFno = ["FUT", "CE", "PE"].includes(form.instrument?.instrument_type ?? "");

  async function start(confirmLive = "") {
    setErr(null);
    if (!form.instrument || !form.strategy) return setErr("Pick an instrument and a strategy");
    let params;
    try {
      params = numericParams(form);
    } catch (e: any) {
      return setErr(e.message);
    }
    const num = (v: string) => (v.trim() === "" ? null : Number(v));
    setBusy(true);
    try {
      const snap = await api.startSession({
        mode,
        exchange: form.instrument.exchange,
        tradingsymbol: form.instrument.tradingsymbol,
        interval: form.interval,
        strategy: form.strategy.key,
        params,
        quantity: Number(qty),
        product,
        allow_short: allowShort && product !== "CNC",
        stop_loss_pct: num(sl),
        target_pct: num(tgt),
        max_trades_per_day: Number(maxTrades),
        max_daily_loss: Number(maxLoss),
        replay_from: mode === "replay" ? rFrom : undefined,
        replay_to: mode === "replay" ? rTo : undefined,
        replay_speed: mode === "replay" ? 0.05 : 0,
        confirm_live: confirmLive,
      });
      navigation.navigate("Session", { id: snap.id });
      load();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  function onStart() {
    if (mode !== "live") return start();
    Alert.alert(
      "Start LIVE trading?",
      `Real orders will be placed in your Zerodha account:\n\n${form.instrument?.exchange}:${form.instrument?.tradingsymbol} · qty ${qty} · ${product}\nMax loss/day ₹${maxLoss} · max ${maxTrades} trades/day\n\nOnly continue if you have paper-tested this strategy.`,
      [
        { text: "Cancel", style: "cancel" },
        { text: "Place real orders", style: "destructive", onPress: () => start("I UNDERSTAND REAL MONEY") },
      ],
    );
  }

  function kill() {
    Alert.alert("Kill switch", "Stop ALL sessions and square off their open positions now?", [
      { text: "Cancel", style: "cancel" },
      {
        text: "STOP EVERYTHING", style: "destructive",
        onPress: async () => {
          try {
            const r = await api.kill();
            Alert.alert("Done", `Stopped ${r.stopped.length} session(s). Verify positions in Kite.`);
            load();
          } catch (e: any) {
            Alert.alert("Kill switch failed", e.message + "\n\nOpen Kite and exit positions manually.");
          }
        },
      },
    ]);
  }

  const visible = sessions.filter((x) => (modes as readonly string[]).includes(String(x.config.mode)));
  const running = visible.filter((x) => x.status === "running" || x.status === "halted");

  return (
    <>
      {running.length > 0 && <Btn label={`⛔ KILL SWITCH (${running.length} active)`} kind="danger" onPress={kill} />}

      <Card title="Strategy sessions (stocks / futures)">
        {visible.length === 0 ? <Text style={s.muted}>No sessions yet. Start one below.</Text> : visible.map((x) => (
          <Pressable key={x.id} onPress={() => navigation.navigate("Session", { id: x.id })}
            style={{ paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: C.border, gap: 4 }}>
            <View style={s.row}>
              <Badge text={String(x.config.mode).toUpperCase()} color={x.config.mode === "live" ? C.red : x.config.mode === "paper" ? C.blue : C.dim} />
              <Text style={[s.text, { flex: 1, fontWeight: "700" }]}>{x.config.tradingsymbol} · {x.config.strategy}</Text>
              <Badge text={x.status} color={statusColor(x.status)} />
            </View>
            <Text style={s.muted}>
              pos {x.position.qty} · LTP {x.ltp ?? "—"} · trades {x.trade_count} · <Text style={{ color: pnlColor(x.net_pnl) }}>{inr(x.net_pnl, 2)}</Text>
            </Text>
          </Pressable>
        ))}
      </Card>

      <Card title="Start a forward test">
        <Chips options={modes} value={mode} onChange={setMode}
          labels={{ paper: "📝 Paper (live market)", replay: "⏪ Replay (past days)", live: "💰 Live (real money)" }} />
        <Text style={s.muted}>
          {mode === "paper" && "Uses live Kite ticks during market hours; orders are simulated. No money at risk."}
          {mode === "replay" && "Feeds past candles through the same engine as live trading. Works anytime, even when the market is closed."}
          {mode === "live" && (liveEnabled
            ? "Places REAL market orders (with market protection) from your server's whitelisted static IP."
            : "Live trading is OFF on your server. Set LIVE_TRADING_ENABLED=true in backend/.env to allow it.")}
        </Text>
        {mode === "replay" && (
          <Row>
            <Field label="Replay from" value={rFrom} onChangeText={setRFrom} />
            <Field label="Replay to" value={rTo} onChangeText={setRTo} />
          </Row>
        )}
      </Card>

      <StrategyForm value={form} onChange={setForm} intervals={INTRADAY} />

      <Card title="Orders & risk">
        <Chips options={(isFno ? ["MIS", "NRML"] : ["MIS", "CNC"]) as ("MIS" | "NRML" | "CNC")[]} value={product} onChange={setProduct} />
        <Row>
          <Field label={`Quantity${(form.instrument?.lot_size ?? 1) > 1 ? ` (lot ${form.instrument?.lot_size})` : ""}`} numeric value={qty} onChangeText={setQty} />
          <Field label="Stop-loss %" numeric value={sl} onChangeText={setSl} />
          <Field label="Target %" numeric value={tgt} onChangeText={setTgt} />
        </Row>
        <Row>
          <Field label="Max trades / day" numeric value={maxTrades} onChangeText={setMaxTrades} />
          <Field label="Max loss / day ₹" numeric value={maxLoss} onChangeText={setMaxLoss} />
        </Row>
        {product !== "CNC" && <Toggle label="Allow short trades" value={allowShort} onChange={setAllowShort} />}
        <Hint>Intraday positions are squared off at 15:10 (before Zerodha's own auto square-off). No new entries after 14:45.</Hint>
      </Card>

      <ErrorText msg={err} />
      <Btn label={mode === "live" ? "Start LIVE session" : mode === "replay" ? "Start replay" : "Start paper trading"}
        kind={mode === "live" ? "warn" : "primary"} onPress={onStart} loading={busy} disabled={mode === "live" && !liveEnabled} />
    </>
  );
}
