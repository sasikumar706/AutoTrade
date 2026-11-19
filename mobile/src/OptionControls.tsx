import React, { useEffect, useRef, useState } from "react";
import { Pressable, Text, View } from "react-native";
import { api, inr, type ChainInfo, type ContractQuote, type IndexKey, type RiskSettings } from "./api";
import { C, Card, Chips, ErrorText, Field, Row, Stat, Toggle, s } from "./ui";

export const INDEXES = ["NIFTY", "BANKNIFTY", "SENSEX"] as const;
export type StrikeMethod = "ATM" | "ITM" | "OTM" | "STRIKE";

export type OptionSelection = {
  index: IndexKey;
  expiry: string; // "" = nearest (auto/backtest-estimated)
  optionType: "CE" | "PE" | "AUTO";
  method: StrikeMethod;
  offset: number;
  strike: number | null;
  lots: number;
};

export const defaultSelection = (allowAuto = false): OptionSelection => ({
  index: "NIFTY", expiry: "", optionType: allowAuto ? "AUTO" : "CE", method: "ATM", offset: 1, strike: null, lots: 1,
});

export const strikeMethodParam = (v: OptionSelection) => (v.method === "STRIKE" ? String(v.strike ?? "") : v.method);

export function Stepper({ label, value, onChange, min = 1, max = 50 }: {
  label: string; value: number; onChange: (n: number) => void; min?: number; max?: number;
}) {
  const btn = (txt: string, d: number) => (
    <Pressable onPress={() => onChange(Math.min(max, Math.max(min, value + d)))}
      style={{ width: 44, height: 40, borderRadius: 8, backgroundColor: C.cardHi, borderWidth: 1, borderColor: C.border, alignItems: "center", justifyContent: "center" }}>
      <Text style={{ color: C.text, fontSize: 20, fontWeight: "800" }}>{txt}</Text>
    </Pressable>
  );
  return (
    <View style={{ flex: 1, minWidth: 140 }}>
      <Text style={s.label}>{label}</Text>
      <View style={[s.row, { gap: 8 }]}>
        {btn("−", -1)}
        <Text style={{ color: C.text, fontSize: 18, fontWeight: "800", minWidth: 32, textAlign: "center" }}>{value}</Text>
        {btn("+", 1)}
      </View>
    </View>
  );
}

/** Index, expiry, CE/PE, strike (ATM/ITM/OTM/fixed) and lots. */
export function OptionPicker({ value, onChange, allowAuto = false, allowNearest = false, title = "Option contract" }: {
  value: OptionSelection; onChange: (v: OptionSelection) => void; allowAuto?: boolean; allowNearest?: boolean; title?: string;
}) {
  const [info, setInfo] = useState<ChainInfo | null>(null);
  const [strikes, setStrikes] = useState<number[]>([]);
  const [spot, setSpot] = useState<number | null>(null);
  const [lotSize, setLotSize] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const set = (p: Partial<OptionSelection>) => onChange({ ...value, ...p });

  useEffect(() => {
    setInfo(null);
    api.chainInfo(value.index).then((i) => {
      setInfo(i);
      setErr(null);
      if (!allowNearest && (!value.expiry || !i.expiries.includes(value.expiry))) set({ expiry: i.expiries[0] ?? "" });
    }).catch((e) => setErr(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.index]);

  useEffect(() => {
    if (!value.expiry) return;
    api.strikes(value.index, value.expiry).then((r) => { setStrikes(r.strikes); setSpot(r.spot); setLotSize(r.lot_size); })
      .catch((e) => setErr(e.message));
  }, [value.index, value.expiry]);

  const expOpts = [...(allowNearest ? ["NEAREST"] : []), ...(info?.expiries.slice(0, 6) ?? [])];
  const types = (allowAuto ? ["AUTO", "CE", "PE"] : ["CE", "PE"]) as ("AUTO" | "CE" | "PE")[];
  return (
    <Card title={title}>
      <Chips options={INDEXES} value={value.index} onChange={(index) => onChange({ ...value, index, expiry: "", strike: null })} />
      <ErrorText msg={err} />
      <Text style={s.label}>Expiry {spot ? `· spot ${spot.toLocaleString("en-IN")}` : ""}</Text>
      {expOpts.length ? (
        <Chips options={expOpts} value={value.expiry || "NEAREST"} onChange={(e) => set({ expiry: e === "NEAREST" ? "" : e, strike: null })}
          labels={Object.fromEntries(expOpts.map((e) => [e, e === "NEAREST" ? "Nearest" : new Date(e + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" })]))} />
      ) : <Text style={s.muted}>{info ? "No listed expiries" : "Loading expiries…"}</Text>}
      <Text style={s.label}>Option type</Text>
      <Chips options={types} value={value.optionType} onChange={(optionType) => set({ optionType })}
        labels={{ AUTO: "Auto (CE bullish / PE bearish)", CE: "CE · Call", PE: "PE · Put" }} />
      <Text style={s.label}>Strike</Text>
      <Chips options={["ATM", "ITM", "OTM", "STRIKE"] as StrikeMethod[]} value={value.method} onChange={(method) => set({ method })}
        labels={{ STRIKE: "Pick strike" }} />
      {(value.method === "ITM" || value.method === "OTM") && (
        <Stepper label={`Strikes ${value.method === "ITM" ? "in" : "out of"} the money`} value={value.offset} min={1} max={10} onChange={(offset) => set({ offset })} />
      )}
      {value.method === "STRIKE" && (
        strikes.length ? (
          <View style={s.chips}>
            {strikes.map((k) => (
              <Pressable key={k} onPress={() => set({ strike: k })}
                style={[s.chip, value.strike === k && s.chipOn, spot !== null && Math.abs(k - spot) < (info?.step ?? 50) / 2 && { borderColor: C.amber }]}>
                <Text style={[s.chipText, value.strike === k && { color: "#fff" }]}>{k}</Text>
              </Pressable>
            ))}
          </View>
        ) : <Text style={s.muted}>{value.expiry ? "Loading strikes…" : "Pick an expiry to choose a fixed strike"}</Text>
      )}
      <Row>
        <Stepper label="Lots" value={value.lots} onChange={(lots) => set({ lots })} />
        <Stat label="Quantity" value={lotSize ? `${value.lots} × ${lotSize} = ${value.lots * lotSize}` : "—"} />
      </Row>
      <Text style={s.muted}>Lot size comes from Zerodha's instrument list for the selected contract.</Text>
    </Card>
  );
}

/** Live preview: contract, premium, qty, order value, SL/target, max loss, margin. Refreshes on every change. */
export function ContractPreview({ sel, side, product, risk, onQuote }: {
  sel: OptionSelection; side: "BUY" | "SELL"; product: string; risk: RiskSettings; onQuote?: (q: ContractQuote | null) => void;
}) {
  const [q, setQ] = useState<ContractQuote | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ready = !!sel.expiry && sel.optionType !== "AUTO" && (sel.method !== "STRIKE" || sel.strike !== null);
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    if (!ready) { setQ(null); onQuote?.(null); return; }
    timer.current = setTimeout(() => {
      api.optionQuote({ index: sel.index, expiry: sel.expiry, option_type: sel.optionType, strike_method: strikeMethodParam(sel),
        strike_offset: sel.offset, lots: sel.lots, side, product, risk })
        .then((r) => { setQ(r); setErr(null); onQuote?.(r); })
        .catch((e) => { setErr(e.message); setQ(null); onQuote?.(null); });
    }, 400);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel.index, sel.expiry, sel.optionType, sel.method, sel.offset, sel.strike, sel.lots, side, product, JSON.stringify(risk), ready]);

  if (!ready) return null;
  return (
    <Card title={q ? q.contract.tradingsymbol : "Contract preview"} right={q ? <Text style={{ color: C.amber, fontWeight: "700" }}>{q.contract.moneyness}</Text> : null}>
      <ErrorText msg={err} />
      {q && (
        <>
          <Row gap={8}>
            <Stat label="Premium (LTP)" value={q.ltp?.toFixed(2) ?? "—"} />
            <Stat label="Bid / Ask" value={`${q.bid ?? "—"} / ${q.ask ?? "—"}`} />
            <Stat label="Spread" value={q.spread_pct !== undefined ? `${q.spread_pct}%` : "—"} color={(q.spread_pct ?? 0) > 3 ? C.red : undefined} />
            <Stat label="Quantity" value={`${q.quantity} (${q.lots} lot${q.lots > 1 ? "s" : ""})`} />
          </Row>
          {q.preview ? (
            <Row gap={8}>
              <Stat label="Order value" value={inr(q.preview.order_value)} />
              <Stat label="Entry ~" value={q.preview.entry.toFixed(2)} />
              <Stat label="Stop-loss" value={q.preview.stop_loss.toFixed(2)} color={C.red} />
              <Stat label="Target" value={q.preview.target?.toFixed(2) ?? "—"} color={C.green} />
              <Stat label="Max loss" value={inr(q.preview.max_loss)} color={C.red} />
              <Stat label="Target profit" value={inr(q.preview.target_profit)} color={C.green} />
              <Stat label="Risk : reward" value={q.preview.risk_reward ? `1 : ${q.preview.risk_reward}` : "—"} />
              <Stat label="Est. charges" value={inr(q.preview.est_charges)} color={C.amber} />
            </Row>
          ) : <ErrorText msg={q.risk_error ?? "Risk check failed"} />}
          <Text style={s.muted}>
            {q.margin_required !== undefined ? `Margin required ${inr(q.margin_required)} · available ${inr(q.margin_available)}` : q.margin_error ?? ""}
          </Text>
        </>
      )}
    </Card>
  );
}

/** Mandatory stop-loss, target (incl. R:R) and trailing stop. */
export function RiskForm({ value, onChange, title = "Stop-loss & target (mandatory)" }: {
  value: RiskSettings; onChange: (r: RiskSettings) => void; title?: string;
}) {
  const set = (p: Partial<RiskSettings>) => onChange({ ...value, ...p });
  const num = (t: string) => (t.trim() === "" ? 0 : Number(t));
  return (
    <Card title={title}>
      <Text style={s.label}>Stop-loss type (on option premium)</Text>
      <Chips options={["pct", "points", "price"] as const} value={value.sl_mode} onChange={(sl_mode) => set({ sl_mode })}
        labels={{ pct: "% of premium", points: "Points", price: "Fixed price" }} />
      <Field label={value.sl_mode === "pct" ? "Stop-loss %" : value.sl_mode === "points" ? "Stop-loss points" : "Stop-loss price"}
        numeric value={String(value.sl_value)} onChangeText={(t) => set({ sl_value: num(t) })} />
      <Text style={s.label}>Target type</Text>
      <Chips options={["rr", "pct", "points", "price", "none"] as const} value={value.target_mode} onChange={(target_mode) => set({ target_mode })}
        labels={{ rr: "Risk:Reward", pct: "%", points: "Points", price: "Price", none: "None" }} />
      {value.target_mode !== "none" && (
        <Field label={value.target_mode === "rr" ? "Reward multiple (2.1 = 1:2.1 risk-to-reward)" : "Target value"} numeric
          value={String(value.target_value)} onChangeText={(t) => set({ target_value: num(t) })} />
      )}
      <Text style={s.label}>Trailing stop</Text>
      <Chips options={["none", "pct", "points"] as const} value={value.trail_mode} onChange={(trail_mode) => set({ trail_mode })}
        labels={{ none: "Off", pct: "% from best", points: "Points from best" }} />
      {value.trail_mode !== "none" && (
        <Row>
          <Field label="Trail distance" numeric value={String(value.trail_value)} onChangeText={(t) => set({ trail_value: num(t) })} />
          <Field label="Start after profit of (R)" numeric value={String(value.trail_activate_rr)} onChangeText={(t) => set({ trail_activate_rr: num(t) })} />
        </Row>
      )}
      <Toggle label="Limit loss per trade" value={value.max_risk_per_trade !== null}
        onChange={(on) => set({ max_risk_per_trade: on ? 2000 : null })} />
      {value.max_risk_per_trade !== null && (
        <Field label="Max loss per trade ₹" numeric value={String(value.max_risk_per_trade)} onChangeText={(t) => set({ max_risk_per_trade: num(t) })} />
      )}
      <Text style={s.muted}>Every trade must have a stop-loss; entries without a valid one are rejected.</Text>
    </Card>
  );
}
