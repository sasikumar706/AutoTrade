import React, { useCallback, useEffect, useState } from "react";
import { Pressable, RefreshControl, ScrollView, Text, View } from "react-native";
import { api, inr } from "../api";
import { Badge, C, Card, Chips, ErrorText, Row, Screen, Section, Stat, pnlColor, s, when, Hint } from "../ui";

const BY_COLOR = (by: string) => (by === "Kite Algo" ? C.blue : C.amber);
const num = (v: any, d = 2) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d }));

/** Every Zerodha order / fill / position for a day — placed by Kite Algo or by hand in the Kite app — with charges. */
export default function ZerodhaScreen() {
  const [day, setDay] = useState<string | null>(null); // null = today (live)
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [days, setDays] = useState<Record<string, any>[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"positions" | "fills" | "orders">("positions");
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [d, list] = await Promise.all([api.accountDay(day ?? undefined), api.accountDays().catch(() => [])]);
      setData(d);
      setDays(list);
      setErr(null);
    } catch (e: any) {
      setErr(e.message);
      setData(null);
    } finally {
      setBusy(false);
    }
  }, [day]);
  useEffect(() => { load(); }, [load]);

  const t = data?.totals ?? {};
  return (
    <Screen refresh={<RefreshControl refreshing={busy} onRefresh={load} />}>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }}>
        <Pressable onPress={() => setDay(null)} style={[s.chip, day === null && s.chipOn]}>
          <Text style={[s.chipText, day === null && { color: "#fff" }]}>Today (live)</Text>
        </Pressable>
        {days.filter((d) => d.day !== new Date().toISOString().slice(0, 10)).map((d) => (
          <Pressable key={d.day} onPress={() => setDay(d.day)} style={[s.chip, day === d.day && s.chipOn]}>
            <Text style={[s.chipText, day === d.day && { color: "#fff" }]}>{d.day.slice(5)} <Text style={{ color: day === d.day ? "#fff" : pnlColor(d.net_after_charges) }}>{inr(d.net_after_charges ?? 0)}</Text></Text>
          </Pressable>
        ))}
      </ScrollView>
      <ErrorText msg={err} />
      {data && (
        <Card title={day ? `Zerodha · ${when(day + "T00:00").slice(0, -6)}` : "Zerodha · today"}
          right={<Badge text={data.live ? "LIVE FROM KITE" : "SAVED COPY"} color={data.live ? C.green : C.dim} />}>
          <Row gap={8}>
            <Stat label="P&L (before charges)" value={inr(t.pnl ?? 0)} color={pnlColor(t.pnl)} />
            <Stat label="Charges (est.)" value={inr(t.charges ?? 0)} color={C.red} />
            <Stat label="Net in hand" value={inr(t.net_after_charges ?? 0)} color={pnlColor(t.net_after_charges)} />
          </Row>
          <Row gap={8}>
            <Stat label="Bought for" value={inr(t.buy_value ?? 0)} />
            <Stat label="Sold for" value={inr(t.sell_value ?? 0)} />
            <Stat label="Open now" value={String(t.open_positions ?? 0)} color={t.open_positions ? C.amber : undefined} />
          </Row>
          <Row gap={8}>
            <Stat label="By Kite Algo" value={inr(t.by_kite_algo ?? 0)} color={pnlColor(t.by_kite_algo)} />
            <Stat label="By hand (Kite app)" value={inr(t.by_hand ?? 0)} color={pnlColor(t.by_hand)} />
            <Stat label="Orders · rejected" value={`${t.orders ?? 0} · ${t.rejected_orders ?? 0}`} color={t.rejected_orders ? C.red : undefined} />
          </Row>
          {data.live && <Text style={s.muted}>Updated {when(data.fetched_at, true)} · pull down to refresh</Text>}
        </Card>
      )}
      {data && (
        <Chips options={["positions", "fills", "orders"] as const} value={view} onChange={setView}
          labels={{ positions: `Per contract (${data.symbols?.length ?? 0})`, fills: `Fills (${data.trades?.length ?? 0})`, orders: `Orders (${data.orders?.length ?? 0})` }} />
      )}
      {data && view === "positions" && (
        <Card>
          {(data.symbols ?? []).length === 0 && <Text style={s.muted}>No F&O trades on this day.</Text>}
          {(data.symbols ?? []).map((r: any) => (
            <Pressable key={r.symbol} onPress={() => setOpen(open === r.symbol ? null : r.symbol)}
              style={{ paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: C.border, gap: 4 }}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
                <View style={{ flex: 1 }}>
                  <Text style={{ color: C.text, fontWeight: "800" }}>{r.symbol}</Text>
                  <View style={{ flexDirection: "row", gap: 6, marginTop: 3, flexWrap: "wrap" }}>
                    <Badge text={r.by} color={BY_COLOR(r.by)} />
                    {r.open_qty ? <Badge text={`OPEN ${r.open_qty}`} color={C.amber} /> : null}
                  </View>
                </View>
                <View style={{ alignItems: "flex-end" }}>
                  <Text style={{ color: pnlColor(r.net), fontWeight: "800", fontSize: 16 }}>{inr(r.net)}</Text>
                  <Text style={[s.muted, { fontSize: 12 }]}>P&L {inr(r.pnl)} − {inr(r.charges)}</Text>
                </View>
              </View>
              <Text style={s.muted}>
                Bought {r.buy_qty} @ ₹{num(r.avg_buy)} = {inr(r.buy_value)} · Sold {r.sell_qty} @ ₹{num(r.avg_sell)} = {inr(r.sell_value)}
              </Text>
              {open === r.symbol && (
                <View style={{ backgroundColor: C.cardHi, borderRadius: 12, padding: 10, gap: 2 }}>
                  <Text style={[s.text, { fontWeight: "700" }]}>Charges ({r.orders} order{r.orders === 1 ? "" : "s"})</Text>
                  {Object.entries(r.charges_detail ?? {}).filter(([k]) => k !== "total").map(([k, v]) => (
                    <View key={k} style={{ flexDirection: "row" }}>
                      <Text style={[s.muted, { flex: 1 }]}>{k.replace("_", " ")}</Text>
                      <Text style={s.muted}>₹{num(v)}</Text>
                    </View>
                  ))}
                  <View style={{ flexDirection: "row", borderTopWidth: 1, borderTopColor: C.border, paddingTop: 4, marginTop: 2 }}>
                    <Text style={[s.text, { flex: 1, fontWeight: "700" }]}>Total</Text>
                    <Text style={[s.text, { fontWeight: "700" }]}>₹{num(r.charges)}</Text>
                  </View>
                  {r.ltp ? <Text style={s.muted}>Last price ₹{num(r.ltp)} · realised {inr(r.realised)} · open {inr(r.unrealised)}</Text> : null}
                </View>
              )}
            </Pressable>
          ))}
        </Card>
      )}
      {data && view === "fills" && (
        <Card>
          {(data.trades ?? []).length === 0 && <Text style={s.muted}>No fills on this day.</Text>}
          {(data.trades ?? []).map((f: any, i: number) => (
            <View key={i} style={{ flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border }}>
              <Badge text={f.side} color={f.side === "BUY" ? C.green : C.red} />
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontWeight: "700" }} numberOfLines={1}>{f.symbol}</Text>
                <Text style={s.muted}>{when(f.time, true)} · {f.qty} @ ₹{num(f.price)} · <Text style={{ color: BY_COLOR(f.by) }}>{f.by}</Text></Text>
              </View>
              <Text style={{ color: C.text, fontWeight: "800" }}>{inr(f.amount)}</Text>
            </View>
          ))}
        </Card>
      )}
      {data && view === "orders" && (
        <Card>
          {(data.orders ?? []).length === 0 && <Text style={s.muted}>No orders on this day.</Text>}
          {(data.orders ?? []).map((o: any, i: number) => (
            <View key={i} style={{ paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border, gap: 2 }}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
                <Badge text={o.side} color={o.side === "BUY" ? C.green : C.red} />
                <Text style={{ color: C.text, fontWeight: "700", flex: 1 }} numberOfLines={1}>{o.symbol}</Text>
                <Badge text={o.status} color={o.status === "COMPLETE" ? C.green : o.status === "REJECTED" ? C.red : C.dim} />
              </View>
              <Text style={s.muted}>
                {when(o.time, true)} · {o.type} · {o.filled ?? 0}/{o.qty} filled{o.avg_price ? ` @ ₹${num(o.avg_price)}` : ""} · <Text style={{ color: BY_COLOR(o.by) }}>{o.by}</Text>
              </Text>
              {o.reason ? <Text style={{ color: o.status === "REJECTED" ? C.red : C.dim, fontSize: 13 }}>{o.reason}</Text> : null}
            </View>
          ))}
        </Card>
      )}
      <Section title="ℹ️ About these numbers">
        <Hint>
          This shows everything in your Zerodha account for the day — trades Kite Algo placed and trades you placed yourself in the Kite app.
          Zerodha only gives today's data to apps, so Kite Algo saves a copy every evening at 15:40; past days appear above from the day you
          started. Charges are estimated from Zerodha's rates; the exact figure is in the contract note. For older history use Zerodha
          Console → Reports → Tradebook / P&L.
        </Hint>
        {data?.note ? <Text style={s.muted}>{data.note}</Text> : null}
      </Section>
    </Screen>
  );
}
