import React, { useEffect, useRef, useState } from "react";
import { Pressable, Text, View } from "react-native";
import { api, type Instrument, type StrategyInfo } from "./api";
import { C, Card, Chips, ErrorText, Field, Row, s } from "./ui";

export const INTRADAY = ["minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute"] as const;
export const ALL_INTERVALS = [...INTRADAY, "day"] as const;
export type Interval = (typeof ALL_INTERVALS)[number];

export type StrategyFormValue = {
  instrument: Instrument | null;
  strategy: StrategyInfo | null;
  params: Record<string, string>;
  interval: Interval;
};

export function useStrategies() {
  const [list, setList] = useState<StrategyInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.strategies().then(setList).catch((e) => setError(e.message));
  }, []);
  return { list, error };
}

export function numericParams(v: StrategyFormValue) {
  const out: Record<string, number> = {};
  for (const p of v.strategy?.params ?? []) {
    const n = Number(v.params[p.key]);
    if (Number.isNaN(n)) throw new Error(`${p.label} must be a number`);
    out[p.key] = n;
  }
  return out;
}

export function InstrumentPicker({ value, onChange }: { value: Instrument | null; onChange: (i: Instrument | null) => void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Instrument[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    if (q.trim().length < 2) {
      setResults([]);
      return;
    }
    timer.current = setTimeout(() => {
      api.search(q).then((r) => { setResults(r); setErr(null); }).catch((e) => setErr(e.message));
    }, 350);
  }, [q]);

  if (value) {
    return (
      <Pressable onPress={() => onChange(null)} style={[s.input, { flexDirection: "row", justifyContent: "space-between" }]}>
        <Text style={{ color: C.text, fontWeight: "700" }}>
          {value.exchange}:{value.tradingsymbol}
          <Text style={{ color: C.dim, fontWeight: "400" }}>  {value.instrument_type}{value.lot_size > 1 ? ` · lot ${value.lot_size}` : ""}</Text>
        </Text>
        <Text style={{ color: C.blue }}>change</Text>
      </Pressable>
    );
  }
  return (
    <View>
      <Field label="Instrument" value={q} onChangeText={setQ} placeholder="Search e.g. INFY, NIFTY26NOVFUT" />
      <ErrorText msg={err} />
      {results.map((r) => (
        <Pressable key={`${r.exchange}:${r.tradingsymbol}`} onPress={() => { onChange(r); setQ(""); setResults([]); }}
          style={{ paddingVertical: 9, borderBottomWidth: 1, borderBottomColor: C.border }}>
          <Text style={{ color: C.text, fontWeight: "600" }}>{r.exchange}:{r.tradingsymbol}</Text>
          <Text style={s.muted}>{r.name || r.instrument_type}{r.expiry ? ` · exp ${String(r.expiry).slice(0, 10)}` : ""}{r.lot_size > 1 ? ` · lot ${r.lot_size}` : ""}</Text>
        </Pressable>
      ))}
    </View>
  );
}

export function StrategyForm({ value, onChange, intervals = ALL_INTERVALS }: {
  value: StrategyFormValue; onChange: (v: StrategyFormValue) => void; intervals?: readonly Interval[];
}) {
  const { list, error } = useStrategies();
  const set = (patch: Partial<StrategyFormValue>) => onChange({ ...value, ...patch });

  useEffect(() => {
    if (!value.strategy && list.length) pick(list[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list]);

  function pick(st: StrategyInfo) {
    const params: Record<string, string> = {};
    st.params.forEach((p) => (params[p.key] = String(p.default)));
    set({ strategy: st, params });
  }

  return (
    <Card title="Strategy">
      <InstrumentPicker value={value.instrument} onChange={(instrument) => set({ instrument })} />
      <ErrorText msg={error} />
      <Text style={s.label}>Strategy</Text>
      <View style={s.chips}>
        {list.map((st) => (
          <Pressable key={st.key} onPress={() => pick(st)} style={[s.chip, value.strategy?.key === st.key && s.chipOn]}>
            <Text style={[s.chipText, value.strategy?.key === st.key && { color: "#fff" }]}>{st.name}</Text>
          </Pressable>
        ))}
      </View>
      {value.strategy ? <Text style={s.muted}>{value.strategy.description}</Text> : null}
      <Row>
        {(value.strategy?.params ?? []).map((p) => (
          <Field key={p.key} label={p.label} numeric value={value.params[p.key] ?? ""}
            onChangeText={(t) => set({ params: { ...value.params, [p.key]: t } })}
            hint={p.min !== null && p.max !== null ? `${p.min} – ${p.max}` : undefined} />
        ))}
      </Row>
      <Text style={s.label}>Candle interval</Text>
      <Chips options={intervals} value={value.interval} onChange={(interval) => set({ interval })}
        labels={{ minute: "1m", "3minute": "3m", "5minute": "5m", "10minute": "10m", "15minute": "15m", "30minute": "30m", "60minute": "1h", day: "1D" }} />
    </Card>
  );
}
