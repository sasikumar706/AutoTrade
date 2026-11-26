import { usePollOnFocus } from "../usePoll";
import React, { useCallback, useLayoutEffect, useState } from "react";
import { RefreshControl, Text, View } from "react-native";
import { api, type Instrument, type MarketDetail } from "../api";
import { moveColor } from "../IndexTicker";
import { Badge, Btn, C, Card, ErrorText, LineChart, Row, Screen, Stat, pnlColor, s } from "../ui";
import { signed } from "./MarketScreen";

const fmt = (n: number | null | undefined, d = 2) => (n === null || n === undefined ? "—" : n.toLocaleString("en-IN", { maximumFractionDigits: d }));
const vol = (n: number | null) => (!n ? "—" : n >= 1e7 ? `${(n / 1e7).toFixed(2)} Cr` : n >= 1e5 ? `${(n / 1e5).toFixed(2)} L` : n.toLocaleString("en-IN"));
const verdictColor = (v?: string) => (v === "BULLISH" ? C.green : v === "BEARISH" ? C.red : C.amber);

/** Detail for any stock or index: price, today's move, chart, day/52-week range, returns, trend. */
export default function QuoteScreen({ route, navigation }: any) {
  const { exchange, tradingsymbol, name } = route.params as { exchange: string; tradingsymbol: string; name?: string };
  const [d, setD] = useState<MarketDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  useLayoutEffect(() => { navigation.setOptions({ title: tradingsymbol }); }, [navigation, tradingsymbol]);
  const load = useCallback(async () => {
    try { setD(await api.marketDetail(exchange, tradingsymbol)); setErr(null); } catch (e: any) { setErr(e.message); }
  }, [exchange, tradingsymbol]);
  usePollOnFocus(load, 30_000);

  if (!d) return <Screen><ErrorText msg={err} /><Text style={s.muted}>Loading {name ?? tradingsymbol}…</Text></Screen>;
  const q = d.quote;
  const col = moveColor(q.direction);
  const isIndex = d.instrument.segment === "INDICES";
  const words = q.change === null ? "" : q.direction === "UP" ? "Up today" : q.direction === "DOWN" ? "Down today" : "Almost unchanged today";
  const inst: Instrument = { instrument_token: 0, exchange: d.instrument.exchange, tradingsymbol: d.instrument.tradingsymbol,
    name: d.instrument.name, instrument_type: d.instrument.instrument_type, segment: d.instrument.segment, lot_size: d.instrument.lot_size || 1 };

  return (
    <Screen refresh={<RefreshControl refreshing={refreshing} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} tintColor={C.dim} />}>
      <Card style={{ borderColor: col + "88" }}>
        <View style={s.row}>
          <Text style={[s.text, { flex: 1, fontWeight: "700" }]} numberOfLines={1}>{d.instrument.name || name || tradingsymbol}</Text>
          <Badge text={`${isIndex ? "INDEX" : "STOCK"} · ${d.instrument.exchange}`} color={isIndex ? C.blue : C.dim} />
        </View>
        <Text style={{ color: C.text, fontSize: 32, fontWeight: "900" }}>{fmt(q.last)}</Text>
        <Text style={{ color: col, fontSize: 17, fontWeight: "800" }}>
          {q.direction === "UP" ? "▲" : q.direction === "DOWN" ? "▼" : "•"} {signed(q.change)} pts ({signed(q.change_pct)}%) · {words}
        </Text>
        <ErrorText msg={err} />
        {d.intraday.closes.length > 1 && (
          <>
            <LineChart values={d.intraday.closes} baseline={q.prev_close ?? undefined} color={col} height={150} />
            <Text style={s.muted}>
              {d.intraday.day} · {d.intraday.times[0]}–{d.intraday.times[d.intraday.times.length - 1]} (5-min) · dashed line = yesterday's close
            </Text>
          </>
        )}
      </Card>

      {q.low !== null && q.high !== null && (
        <Card title="Today's range">
          <View style={{ height: 8, borderRadius: 4, backgroundColor: C.cardHi }}>
            {q.range_pos !== null && (
              <View style={{ position: "absolute", left: `${Math.min(97, Math.max(0, q.range_pos))}%`, top: -4, width: 6, height: 16, borderRadius: 3, backgroundColor: col }} />
            )}
          </View>
          <View style={s.row}>
            <Text style={[s.muted, { flex: 1 }]}>Low {fmt(q.low)}</Text>
            <Text style={s.muted}>High {fmt(q.high)}</Text>
          </View>
          <Row gap={8}>
            <Stat label="Open" value={fmt(q.open)} />
            <Stat label="Prev close" value={fmt(q.prev_close)} />
            {!isIndex && <Stat label="Volume" value={vol(q.volume)} />}
            {!isIndex && <Stat label="Avg price" value={fmt(q.avg_price)} />}
            {!isIndex && q.bid ? <Stat label="Bid / Ask" value={`${fmt(q.bid)} / ${fmt(q.ask)}`} /> : null}
            {!isIndex && q.upper_circuit ? <Stat label="Circuit" value={`${fmt(q.lower_circuit)} – ${fmt(q.upper_circuit)}`} /> : null}
          </Row>
        </Card>
      )}

      {d.stats.high_52w !== undefined && (
        <Card title="Longer view">
          <Row gap={8}>
            <Stat label="52-week high" value={fmt(d.stats.high_52w)} />
            <Stat label="52-week low" value={fmt(d.stats.low_52w)} />
            <Stat label="From 52W high" value={`${signed(d.stats.from_52w_high_pct)}%`} color={pnlColor(d.stats.from_52w_high_pct)} />
          </Row>
          <View style={{ flexDirection: "row", gap: 6, flexWrap: "wrap" }}>
            {Object.entries(d.stats.returns ?? {}).map(([k, v]) => (
              <View key={k} style={{ flexGrow: 1, minWidth: 56, alignItems: "center", padding: 8, borderRadius: 10, backgroundColor: pnlColor(v) === C.text ? C.cardHi : pnlColor(v) + "1F" }}>
                <Text style={s.statLabel}>{k}</Text>
                <Text style={{ color: pnlColor(v), fontWeight: "800" }}>{signed(v)}%</Text>
              </View>
            ))}
          </View>
        </Card>
      )}

      {d.trend && (
        <Card title="Trend (daily chart)" right={<Badge text={d.trend.verdict} color={verdictColor(d.trend.verdict)} />}>
          <Text style={[s.text, { lineHeight: 21 }]}>{d.trend.plain}</Text>
          {d.trend.signals.map((x, i) => (
            <Text key={i} style={{ color: x.vote > 0 ? C.green : x.vote < 0 ? C.red : C.dim, fontSize: 13 }}>
              {x.vote > 0 ? "▲" : x.vote < 0 ? "▼" : "•"}  <Text style={{ color: C.text }}>{x.reason}</Text>
            </Text>
          ))}
          <Text style={s.muted}>Information only — not a buy or sell recommendation.</Text>
        </Card>
      )}

      <Btn label="✦ Analyse with AI Finder" onPress={() => navigation.navigate("Finder", { inst, nonce: Date.now() })} />
    </Screen>
  );
}
