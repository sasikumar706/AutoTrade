import React, { useEffect, useRef, useState } from "react";
import { Alert, Text, View } from "react-native";
import { api, DEFAULT_RISK, type RiskSettings, type StrategyInfo } from "../api";
import { defaultSelection, OptionPicker, RiskForm, strikeMethodParam, type OptionSelection } from "../OptionControls";
import { INTRADAY, numericParams, useStrategies } from "../StrategyForm";
import { Badge, Btn, C, Card, Chips, ErrorText, Field, Row, s, Toggle } from "../ui";

const iso = (d: Date) => d.toISOString().slice(0, 10);
const daysAgo = (n: number) => iso(new Date(Date.now() - n * 86400_000));

export default function OptionsBacktestScreen({ navigation }: any) {
  const { list } = useStrategies();
  const [sel, setSel] = useState<OptionSelection>({ ...defaultSelection(true) });
  const [strategy, setStrategy] = useState<StrategyInfo | null>(null);
  const [params, setParams] = useState<Record<string, string>>({});
  const [interval, setInterval_] = useState<(typeof INTRADAY)[number]>("5minute");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [allowSell, setAllowSell] = useState(false);
  const [source, setSource] = useState<"estimated" | "real">("estimated");
  const [iv, setIv] = useState("");
  const [from, setFrom] = useState(daysAgo(180));
  const [to, setTo] = useState(daysAgo(0));
  const [capital, setCapital] = useState("100000");
  const [risk, setRisk] = useState<RiskSettings>({ ...DEFAULT_RISK });
  const [slip, setSlip] = useState("30");
  const [win, setWin] = useState({ entry_start: "09:30", entry_end: "10:30", exit_time: "10:30" });
  const [maxTrades, setMaxTrades] = useState("2");
  const [maxLoss, setMaxLoss] = useState("");
  const [job, setJob] = useState<{ id: string; status: string; progress: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!strategy && list.length) pick(list.find((x) => x.key === "morning_momentum") ?? list[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list]);
  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);
  useEffect(() => { if (source === "real") setFrom(daysAgo(30)); }, [source]);

  function pick(st: StrategyInfo) {
    setStrategy(st);
    setParams(Object.fromEntries(st.params.map((p) => [p.key, String(p.default)])));
  }

  function reset() {
    setSel({ ...defaultSelection(true) });
    setRisk({ ...DEFAULT_RISK });
    setSide("BUY"); setAllowSell(false); setSource("estimated"); setIv(""); setSlip("30"); setCapital("100000");
    setWin({ entry_start: "09:30", entry_end: "10:30", exit_time: "10:30" }); setMaxTrades("2"); setMaxLoss("");
    setFrom(daysAgo(180)); setTo(daysAgo(0)); setErr(null); setJob(null);
    const mm = list.find((x) => x.key === "morning_momentum");
    if (mm) pick(mm);
  }

  async function run() {
    setErr(null);
    if (!strategy) return setErr("Pick a strategy");
    if (source === "real" && !sel.expiry) return setErr("Real-premium mode needs a listed expiry (pick one above)");
    if (side === "SELL" && !allowSell) return setErr("Turn on 'Allow option selling' to backtest SELL");
    let p;
    try {
      p = numericParams({ strategy, params, instrument: null, interval });
    } catch (e: any) {
      return setErr(e.message);
    }
    try {
      const j = await api.optionsBacktest({
        index: sel.index, interval, from_date: from, to_date: to, strategy: strategy.key, params: p, side,
        option_rule: sel.optionType, strike_method: strikeMethodParam(sel), strike_offset: sel.offset, lots: sel.lots,
        premium_source: source, iv_pct: iv ? Number(iv) : null, expiry: sel.expiry || null,
        initial_capital: Number(capital), risk, slippage_bps: Number(slip), ...win,
        max_trades_per_day: Number(maxTrades), max_daily_loss: maxLoss ? Number(maxLoss) : null, allow_option_selling: allowSell,
      });
      setJob(j);
      poll.current = setInterval(async () => {
        try {
          const s2 = await api.job(j.id);
          setJob(s2);
          if (s2.status !== "running") {
            if (poll.current) clearInterval(poll.current);
            if (s2.status === "done") navigation.navigate("OptionsResult", { result: s2.result });
            else if (s2.status === "error") setErr(s2.error);
          }
        } catch (e: any) {
          setErr(e.message);
        }
      }, 1500);
    } catch (e: any) {
      setErr(e.message);
    }
  }

  async function stop() {
    if (!job) return;
    try {
      await api.cancelJob(job.id);
    } catch (e: any) {
      Alert.alert("Stop", e.message);
    }
  }

  const running = job?.status === "running";
  return (
    <>
      <Card title="Trade direction">
        <Row>
          <View style={{ flex: 1 }}><Btn label="BUY options" kind={side === "BUY" ? "primary" : "ghost"} onPress={() => setSide("BUY")} /></View>
          <View style={{ flex: 1 }}><Btn label="SELL options" kind={side === "SELL" ? "danger" : "ghost"} onPress={() => setSide("SELL")} /></View>
        </Row>
        <Text style={s.muted}>
          {side === "BUY"
            ? "BUY = buy to open: bullish signal → buy CE, bearish → buy PE. Exits sell to close. Max loss = premium paid."
            : "SELL = sell to open: bullish → sell PE, bearish → sell CE. Exits buy to close. Losses can be large; needs high margin."}
        </Text>
        {side === "SELL" && <Toggle label="Allow option selling" value={allowSell} onChange={setAllowSell} />}
        <Text style={s.muted}>An opposite signal only closes the open position — it never flips it by accident.</Text>
      </Card>

      <OptionPicker value={sel} onChange={setSel} allowAuto allowNearest={source === "estimated"} title="Contracts to trade" />

      <Card title="Option prices">
        <Chips options={["estimated", "real"] as const} value={source} onChange={setSource}
          labels={{ estimated: "Estimated (long history)", real: "Real premiums (recent)" }} />
        <Text style={s.muted}>
          {source === "estimated"
            ? "Premiums estimated from the index with Black-Scholes and the rule-based expiry. Good for months of history; ignores IV changes."
            : "Uses real candles of the selected listed expiry. Accurate, but Kite has no data for expired contracts, so only recent weeks work."}
        </Text>
        {source === "estimated" && <Field label="Implied volatility % (blank = index default)" numeric value={iv} onChangeText={setIv} />}
      </Card>

      <Card title="Signal strategy (on the index)">
        <View style={s.chips}>
          {list.map((st) => (
            <Text key={st.key} onPress={() => pick(st)} style={[s.chip, s.chipText, strategy?.key === st.key && s.chipOn, strategy?.key === st.key && { color: "#fff" }]}>
              {st.name}
            </Text>
          ))}
        </View>
        {strategy && <Text style={s.muted}>{strategy.description}</Text>}
        <Row>
          {(strategy?.params ?? []).map((p) => (
            <Field key={p.key} label={p.label} numeric value={params[p.key] ?? ""} onChangeText={(t) => setParams({ ...params, [p.key]: t })} />
          ))}
        </Row>
        <Text style={s.label}>Candle interval</Text>
        <Chips options={INTRADAY} value={interval} onChange={setInterval_}
          labels={{ minute: "1m", "3minute": "3m", "5minute": "5m", "10minute": "10m", "15minute": "15m", "30minute": "30m", "60minute": "1h" }} />
      </Card>

      <RiskForm value={risk} onChange={setRisk} />

      <Card title="Period, windows & limits">
        <Row>
          <Field label="From" value={from} onChangeText={setFrom} />
          <Field label="To" value={to} onChangeText={setTo} />
        </Row>
        <Row>
          <Field label="Entry from" value={win.entry_start} onChangeText={(t) => setWin({ ...win, entry_start: t })} />
          <Field label="Entry until" value={win.entry_end} onChangeText={(t) => setWin({ ...win, entry_end: t })} />
          <Field label="Exit all at" value={win.exit_time} onChangeText={(t) => setWin({ ...win, exit_time: t })} />
        </Row>
        <Row>
          <Field label="Capital ₹" numeric value={capital} onChangeText={setCapital} />
          <Field label="Max trades/day" numeric value={maxTrades} onChangeText={setMaxTrades} />
          <Field label="Max loss/day ₹" numeric value={maxLoss} onChangeText={setMaxLoss} placeholder="none" />
          <Field label="Slippage (bps)" numeric value={slip} onChangeText={setSlip} hint="30 = 0.3% per fill" />
        </Row>
      </Card>

      <ErrorText msg={err} />
      {job && running && (
        <Card><View style={s.row}><Badge text="RUNNING" color={C.blue} /><Text style={[s.muted, { flex: 1 }]}>{job.progress}</Text></View></Card>
      )}
      {job?.status === "cancelled" && <Text style={{ color: C.amber }}>Backtest stopped.</Text>}
      <Row>
        <View style={{ flex: 2 }}><Btn label="RUN BACKTEST" onPress={run} disabled={running} loading={running} /></View>
        <View style={{ flex: 1 }}><Btn label="STOP" kind="danger" onPress={stop} disabled={!running} /></View>
        <View style={{ flex: 1 }}><Btn label="RESET" kind="ghost" onPress={reset} disabled={running} /></View>
      </Row>
    </>
  );
}
