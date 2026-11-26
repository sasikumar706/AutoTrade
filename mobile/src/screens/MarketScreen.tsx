import { usePollOnFocus } from "../usePoll";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, RefreshControl, Text, TextInput, View } from "react-native";
import { api, type Instrument, type MarketRow } from "../api";
import { moveColor } from "../IndexTicker";
import { C, Card, ErrorText, Screen, s, Hint } from "../ui";

const fmt = (n: number | null | undefined, d = 2) => (n === null || n === undefined ? "—" : n.toLocaleString("en-IN", { maximumFractionDigits: d }));
export const signed = (n: number | null | undefined, d = 2) => (n === null || n === undefined ? "—" : `${n > 0 ? "+" : ""}${fmt(n, d)}`);

/** Market: major indices at a glance + search any stock or index. */
export default function MarketScreen({ navigation }: any) {
  const [rows, setRows] = useState<MarketRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Instrument[]>([]);
  const [searching, setSearching] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try { setRows(await api.marketOverview()); setErr(null); } catch (e: any) { setErr(e.message); }
  }, []);
  usePollOnFocus(load, 5000);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    if (q.trim().length < 2) { setResults([]); return; }
    timer.current = setTimeout(async () => {
      setSearching(true);
      try { setResults(await api.searchCash(q)); } catch (e: any) { setErr(e.message); } finally { setSearching(false); }
    }, 350);
  }, [q]);

  const open = (exchange: string, tradingsymbol: string, name?: string) =>
    navigation.navigate("Quote", { exchange, tradingsymbol, name });

  const main = rows.filter((r) => r.tradingsymbol !== "INDIA VIX");
  const up = main.filter((r) => r.direction === "UP").length;
  const down = main.filter((r) => r.direction === "DOWN").length;
  const vix = rows.find((r) => r.tradingsymbol === "INDIA VIX");
  const mood = !main.length ? null : up > down * 2 ? { t: "Market is mostly UP today", c: C.green }
    : down > up * 2 ? { t: "Market is mostly DOWN today", c: C.red } : { t: "Market is mixed today", c: C.amber };

  return (
    <Screen refresh={<RefreshControl refreshing={refreshing} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} tintColor={C.dim} />}>
      <Pressable onPress={() => navigation.navigate("Finder")}
        style={{ flexDirection: "row", alignItems: "center", gap: 10, padding: 12, borderRadius: 14, backgroundColor: C.blue + "12", borderWidth: 1, borderColor: C.blue + "44" }}>
        <Text style={{ fontSize: 18, color: C.blue }}>✦</Text>
        <View style={{ flex: 1 }}>
          <Text style={{ color: C.blue, fontWeight: "800" }}>AI Strategy Finder</Text>
          <Text style={{ color: C.dim, fontSize: 12 }}>Test every strategy on any stock or index</Text>
        </View>
        <Text style={{ color: C.blue, fontWeight: "800" }}>›</Text>
      </Pressable>
      <View style={[s.input, { flexDirection: "row", alignItems: "center", gap: 8 }]}>
        <Text style={{ color: C.dim, fontSize: 16 }}>⌕</Text>
        <TextInput value={q} onChangeText={setQ} placeholder="Search any stock or index (e.g. RELIANCE, NIFTY IT)"
          placeholderTextColor="#9AA5B4" autoCapitalize="characters" autoCorrect={false} style={{ flex: 1, color: C.text, paddingVertical: 0 }} />
        {searching ? <ActivityIndicator color={C.dim} size="small" /> : q ? (
          <Pressable onPress={() => setQ("")} hitSlop={10}><Text style={{ color: C.dim }}>✕</Text></Pressable>
        ) : null}
      </View>

      {results.length > 0 && (
        <Card>
          {results.map((r) => (
            <Pressable key={`${r.exchange}:${r.tradingsymbol}`} onPress={() => { open(r.exchange, r.tradingsymbol, r.name); setQ(""); }}
              style={{ paddingVertical: 9, borderBottomWidth: 1, borderBottomColor: C.border, flexDirection: "row", gap: 8, alignItems: "center" }}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontWeight: "700" }}>{r.tradingsymbol}</Text>
                <Text style={s.muted} numberOfLines={1}>{r.name || "—"}</Text>
              </View>
              <Text style={{ color: r.segment === "INDICES" ? C.blue : C.dim, fontSize: 12, fontWeight: "700" }}>
                {r.segment === "INDICES" ? "INDEX" : "STOCK"} · {r.exchange}
              </Text>
            </Pressable>
          ))}
        </Card>
      )}
      {q.trim().length >= 2 && !searching && results.length === 0 && <Text style={s.muted}>No stock or index matches "{q}".</Text>}

      <ErrorText msg={err} />
      {mood && (
        <Card style={{ borderColor: mood.c, backgroundColor: mood.c + "14" }}>
          <Text style={{ color: mood.c, fontSize: 18, fontWeight: "900" }}>{mood.t}</Text>
          <Text style={s.text}>{up} of {main.length} indices up · {down} down</Text>
          {vix?.last ? (
            <Text style={s.muted}>
              Fear gauge (India VIX) {fmt(vix.last)} — {vix.last < 13 ? "calm market" : vix.last < 18 ? "normal swings" : "nervous market, bigger swings"}
              {vix.change_pct !== null ? ` (${signed(vix.change_pct)}% today)` : ""}
            </Text>
          ) : null}
        </Card>
      )}

      <Card title="Major indices">
        {main.length === 0 && !err && <Text style={s.muted}>Loading… (log in with Kite first)</Text>}
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
          {main.map((r) => {
            const col = moveColor(r.direction);
            return (
              <Pressable key={r.tradingsymbol} onPress={() => open(r.exchange, r.tradingsymbol, r.name)}
                style={{ width: "48%", flexGrow: 1, padding: 10, borderRadius: 12, backgroundColor: col + "1A", borderWidth: 1, borderColor: col + "55", gap: 2 }}>
                <Text style={{ color: C.dim, fontSize: 12, fontWeight: "700" }}>{r.name}</Text>
                <Text style={{ color: C.text, fontSize: 17, fontWeight: "800" }}>{fmt(r.last)}</Text>
                <Text style={{ color: col, fontWeight: "800" }}>
                  {r.direction === "UP" ? "▲" : r.direction === "DOWN" ? "▼" : "•"} {signed(r.change)} ({signed(r.change_pct)}%)
                </Text>
              </Pressable>
            );
          })}
        </View>
        <Hint>Tap any index for its chart, day range and trend. Green = up today, red = down, grey = flat (within ±0.15%).</Hint>
      </Card>
    </Screen>
  );
}
