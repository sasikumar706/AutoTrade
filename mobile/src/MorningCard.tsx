import React, { useEffect, useState } from "react";
import { ActivityIndicator, Text, View } from "react-native";
import { api, type IndexKey } from "./api";
import { INDEXES } from "./OptionControls";
import { Badge, C, Card, Chips, ErrorText, Row, Stat, pnlColor, s } from "./ui";

const phaseLabel: Record<string, string> = {
  CLOSED_DAY: "Market closed today", PRE_MARKET: "Pre-market", ANALYSIS: "Analysis window (9:15–9:30)",
  ENTRY: "Entry window (9:30–10:30)", MANAGE_ONLY: "Managing positions", AFTER_CUTOFF: "After 10:30 cutoff",
};

/** Previous-day levels + today's opening + indicators + ATM CE/PE premiums for an index. */
export default function MorningCard({ enabled }: { enabled: boolean }) {
  const [index, setIndex] = useState<IndexKey>("NIFTY");
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    setLoading(true);
    api.optionsAnalysis(index).then((x) => { setD(x); setErr(null); }).catch((e) => setErr(e.message)).finally(() => setLoading(false));
  }, [index, enabled]);
  const t = d?.technical;
  const verdictColor = t?.verdict === "BULLISH" ? C.green : t?.verdict === "BEARISH" ? C.red : C.amber;
  return (
    <Card title="Market view" right={t ? <Badge text={t.verdict} color={verdictColor} /> : null}>
      <Chips options={INDEXES} value={index} onChange={setIndex} />
      {!enabled && <Text style={s.muted}>Log in with Kite to see the market view.</Text>}
      {loading && <ActivityIndicator color={C.blue} />}
      <ErrorText msg={err} />
      {d && (
        <>
          <Text style={s.muted}>{phaseLabel[d.phase] ?? d.phase} · {d.time.slice(11, 16)} IST</Text>
          {d.previous_day?.high !== undefined && (
            <Row gap={8}>
              <Stat label={`Prev day (${d.previous_day.trend})`} value={`H ${d.previous_day.high} · L ${d.previous_day.low}`} />
              <Stat label="Prev close" value={String(d.previous_day.close)} />
              <Stat label="Pivot / R1 / S1" value={`${d.previous_day.P} / ${d.previous_day.R1} / ${d.previous_day.S1}`} />
            </Row>
          )}
          {d.today?.open !== undefined && (
            <Row gap={8}>
              <Stat label="Today open" value={`${d.today.open} (${d.today.gap ?? ""} ${d.today.gap_pct ?? ""}%)`} color={pnlColor(d.today.gap_pct)} />
              <Stat label="Last" value={`${d.today.last} · ${d.today.vs_levels ?? ""}`} />
            </Row>
          )}
          {t && <Text style={s.muted}>Score {t.score > 0 ? "+" : ""}{t.score} · {t.regime} · ATR {t.volatility_atr_pct}% · {t.signals.filter((x: any) => x.vote !== 0).slice(0, 3).map((x: any) => x.reason).join("; ")}</Text>}
          {d.atm_options && !d.atm_options.error && (
            <Row gap={8}>
              {(["CE", "PE"] as const).map((ot) => d.atm_options[ot] && (
                <View key={ot} style={[s.stat]}>
                  <Text style={s.statLabel}>ATM {ot} {d.atm_options[ot].strike}</Text>
                  <Text style={s.statValue}>{d.atm_options[ot].ltp ?? "—"}</Text>
                  <Text style={{ color: pnlColor(d.atm_options[ot].change_pct), fontSize: 12 }}>
                    {d.atm_options[ot].change_pct !== null ? `${d.atm_options[ot].change_pct}% vs prev close` : ""}{d.atm_options[ot].spread_pct !== null ? ` · spread ${d.atm_options[ot].spread_pct}%` : ""}
                  </Text>
                </View>
              ))}
            </Row>
          )}
          <Text style={[s.text, { fontWeight: "800" }]}>Bias: {d.bias_suggestion.replace("_", " ")}</Text>
          <Text style={s.muted}>{d.note}</Text>
        </>
      )}
    </Card>
  );
}
