import React, { useState } from "react";
import { Text } from "react-native";
import { api } from "../api";
import { numericParams, StrategyForm, type StrategyFormValue } from "../StrategyForm";
import { Screen } from "../ui";
import KiteLogout from "../KiteLogout";
import OptionsBacktestScreen from "./OptionsBacktestScreen";
import { Btn, Card, Chips, ErrorText, Field, Row, Toggle, s } from "../ui";

const iso = (d: Date) => d.toISOString().slice(0, 10);
const daysAgo = (n: number) => iso(new Date(Date.now() - n * 86400_000));

// Ranges that keep request counts sane for each candle size (server chunks them at 3 req/sec).
const PRESETS: Record<string, number[]> = {
  minute: [7, 30, 90], "3minute": [30, 90, 180], "5minute": [30, 90, 180, 365], "10minute": [90, 180, 365],
  "15minute": [90, 180, 365], "30minute": [180, 365, 730], "60minute": [365, 730], day: [365, 1095, 1825, 3650],
};

export function StocksBacktest({ navigation }: any) {
  const [form, setForm] = useState<StrategyFormValue>({ instrument: null, strategy: null, params: {}, interval: "5minute" });
  const [from, setFrom] = useState(daysAgo(90));
  const [to, setTo] = useState(daysAgo(1));
  const [capital, setCapital] = useState("100000");
  const [product, setProduct] = useState<"MIS" | "CNC" | "NRML">("MIS");
  const [sizing, setSizing] = useState<"pct_capital" | "fixed_qty">("pct_capital");
  const [pctCap, setPctCap] = useState("100");
  const [qty, setQty] = useState("1");
  const [sl, setSl] = useState("1");
  const [tgt, setTgt] = useState("2");
  const [slip, setSlip] = useState("5");
  const [allowShort, setAllowShort] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const isFno = ["FUT", "CE", "PE"].includes(form.instrument?.instrument_type ?? "");
  const products = (isFno ? ["MIS", "NRML"] : ["MIS", "CNC"]) as ("MIS" | "CNC" | "NRML")[];

  async function run() {
    setErr(null);
    if (!form.instrument || !form.strategy) return setErr("Pick an instrument and a strategy");
    if (product === "MIS" && form.interval === "day") return setErr("Intraday (MIS) needs an intraday interval");
    let params;
    try {
      params = numericParams(form);
    } catch (e: any) {
      return setErr(e.message);
    }
    const num = (v: string) => (v.trim() === "" ? null : Number(v));
    setBusy(true);
    try {
      const res = await api.backtest({
        exchange: form.instrument.exchange,
        tradingsymbol: form.instrument.tradingsymbol,
        interval: form.interval,
        from_date: from,
        to_date: to,
        strategy: form.strategy.key,
        params,
        initial_capital: Number(capital),
        product: products.includes(product) ? product : "MIS",
        sizing,
        pct_capital: Number(pctCap),
        quantity: Number(qty),
        stop_loss_pct: num(sl),
        target_pct: num(tgt),
        slippage_bps: Number(slip),
        allow_short: allowShort && product !== "CNC",
      });
      navigation.navigate("BacktestResult", { result: res });
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <StrategyForm value={form} onChange={setForm} />

      <Card title="Period">
        <Row>
          <Field label="From (YYYY-MM-DD)" value={from} onChangeText={setFrom} />
          <Field label="To" value={to} onChangeText={setTo} />
        </Row>
        <Chips options={(PRESETS[form.interval] ?? [90]).map(String)} value={""}
          onChange={(d) => { setFrom(daysAgo(Number(d))); setTo(daysAgo(1)); }}
          labels={Object.fromEntries((PRESETS[form.interval] ?? []).map((d) => [String(d), d >= 365 ? `${Math.round(d / 365)}Y` : `${d}D`]))} />
        <Text style={s.muted}>Long minute-data ranges take longer: Kite allows 3 history requests per second.</Text>
      </Card>

      <Card title="Trading rules">
        <Text style={s.label}>Product</Text>
        <Chips options={products} value={product} onChange={setProduct}
          labels={{ MIS: "MIS · intraday", CNC: "CNC · delivery", NRML: "NRML · F&O carry" }} />
        <Row>
          <Field label="Capital ₹" numeric value={capital} onChangeText={setCapital} />
          <Field label="Slippage (bps)" numeric value={slip} onChangeText={setSlip} hint="5 bps = 0.05% per fill" />
        </Row>
        <Text style={s.label}>Position size</Text>
        <Chips options={["pct_capital", "fixed_qty"] as const} value={sizing} onChange={setSizing}
          labels={{ pct_capital: "% of capital", fixed_qty: "Fixed quantity" }} />
        {sizing === "pct_capital" ? (
          <Field label="% of capital per trade" numeric value={pctCap} onChangeText={setPctCap} />
        ) : (
          <Field label={`Quantity${(form.instrument?.lot_size ?? 1) > 1 ? ` (multiple of ${form.instrument?.lot_size})` : ""}`} numeric value={qty} onChangeText={setQty} />
        )}
        <Row>
          <Field label="Stop-loss %" numeric value={sl} onChangeText={setSl} hint="blank = none" />
          <Field label="Target %" numeric value={tgt} onChangeText={setTgt} hint="blank = none" />
        </Row>
        {product !== "CNC" && <Toggle label="Allow short trades" value={allowShort} onChange={setAllowShort} />}
      </Card>

      <ErrorText msg={err} />
      <Btn label={busy ? "Downloading candles & testing…" : "Run backtest"} onPress={run} loading={busy} />
    </>
  );
}

export default function BacktestScreen({ navigation }: any) {
  const [mode, setMode] = useState<"stocks" | "options">("options");
  return (
    <Screen>
      <Chips options={["options", "stocks"] as const} value={mode} onChange={setMode}
        labels={{ options: "Index options (CE/PE)", stocks: "Stocks / futures" }} />
      {mode === "options" ? <OptionsBacktestScreen navigation={navigation} /> : <StocksBacktest navigation={navigation} />}
      <KiteLogout />
    </Screen>
  );
}
