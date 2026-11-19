import React, { useState } from "react";
import { Pressable, Text, View } from "react-native";
import { inr, type Watch } from "./api";
import { Badge, C, Card, InfoLine, Row, Stat, s, contractText } from "./ui";

const leanColor = (l?: string) => (l === "BULLISH" ? C.green : l === "BEARISH" ? C.red : C.amber);
const LIKELY: Record<string, { text: string; color: string }> = {
  HIGH: { text: "ORDER LIKELY", color: C.green },
  MEDIUM: { text: "SETUP BUILDING", color: C.amber },
  LOW: { text: "WAITING", color: C.dim },
  NONE: { text: "NO ORDER NOW", color: C.dim },
};

/** Live "what happens next" view for an Auto Trade strategy — updates with the price. Never places orders. */
export default function WatchCard({ w, index, autoMoney, hidePrice }: { w?: Watch | null; index: string; autoMoney?: boolean; hidePrice?: boolean }) {
  const [showChecks, setShowChecks] = useState(false);
  const [showOrder, setShowOrder] = useState(false);
  const [showWhy, setShowWhy] = useState(false);
  const [allProblems, setAllProblems] = useState(false);
  if (!w) return (
    <Card title="Next move"><Text style={s.muted}>Shows once Auto is on and prices flow.</Text></Card>
  );
  const ww = w as any;
  const moveCol = (w.change ?? 0) > 0 ? C.green : (w.change ?? 0) < 0 ? C.red : C.dim;
  const lk = LIKELY[w.likelihood ?? "LOW"];
  const o = w.expected_order;
  const up = (w.expected_direction ?? 0) > 0;
  const blocked = o && o.risk_check === "FAIL" && o.risk_problems.length > 0;
  const lean = w.lean === "BULLISH" ? { icon: "▲", text: "Leaning up", col: C.green }
    : w.lean === "BEARISH" ? { icon: "▼", text: "Leaning down", col: C.red } : { icon: "●", text: "No clear direction", col: C.amber };
  // headline = the main status; older servers send everything in one block, so fall back to its first line
  const main: string = ww.status_main ?? (w.status ?? "").split("\n")[0].replace(/ \(outside the entry window.*\)$/, "");
  const cons = ww.consensus as { up: string[]; down: string[]; total: number; min: number } | undefined;
  const pb = ww.pullback as { direction: number; until_hhmm?: string; reentry?: boolean } | undefined;
  return (
    <Card title="Next move" right={<Badge text={lk.text} color={lk.color} />}>
      {!hidePrice && (
        <View style={s.row}>
          <Text style={{ color: C.text, fontSize: 22, fontWeight: "900", flex: 1 }}>{index} {w.price?.toLocaleString("en-IN") ?? "—"}</Text>
          {w.change !== undefined && (
            <Text style={{ color: moveCol, fontWeight: "800" }}>{w.change > 0 ? "▲ +" : w.change < 0 ? "▼ " : ""}{w.change} ({w.change_pct}%)</Text>
          )}
        </View>
      )}
      {w.error ? <Text style={{ color: C.red }}>{w.error}</Text> : null}
      {w.lean && (
        <Pressable onPress={() => setShowChecks(!showChecks)} style={{ gap: 8 }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
            <View style={{ width: 34, height: 34, borderRadius: 17, backgroundColor: lean.col + "1A", alignItems: "center", justifyContent: "center" }}>
              <Text style={{ color: lean.col, fontSize: 15, fontWeight: "900" }}>{lean.icon}</Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text style={{ color: lean.col, fontWeight: "900", fontSize: 17 }}>{lean.text}</Text>
              <Text style={[s.muted, { fontSize: 12 }]}>Indicators: {w.bull_score} up · {w.bear_score} down (of {w.checks_total})</Text>
            </View>
            <Text style={{ color: C.blue, fontWeight: "800", fontSize: 13 }}>{showChecks ? "Hide" : "Why"}</Text>
          </View>
          <View style={{ flexDirection: "row", height: 8, borderRadius: 4, overflow: "hidden", backgroundColor: C.cardHi, gap: 2 }}>
            <View style={{ flex: w.bull_score ?? 0, backgroundColor: C.green }} />
            <View style={{ flex: Math.max(0, (w.checks_total ?? 0) - (w.bull_score ?? 0) - (w.bear_score ?? 0)), backgroundColor: "#D5DCE6" }} />
            <View style={{ flex: w.bear_score ?? 0, backgroundColor: C.red }} />
          </View>
        </Pressable>
      )}
      {showChecks && w.bull_checks && (
        <View style={{ gap: 6, padding: 10, borderRadius: 12, backgroundColor: C.cardHi }}>
          {w.bull_checks.map((c, i) => {
            const b = w.bear_checks?.[i]?.ok;
            const col = c.ok ? C.green : b ? C.red : C.dim;
            return (
              <View key={i} style={{ flexDirection: "row", gap: 8, alignItems: "flex-start" }}>
                <Text style={{ color: col, fontWeight: "900", width: 14 }}>{c.ok ? "▲" : b ? "▼" : "•"}</Text>
                <Text style={{ color: C.text, fontSize: 13, flex: 1, lineHeight: 18 }}>{c.label}</Text>
              </View>
            );
          })}
        </View>
      )}
      {main ? <Text style={{ color: C.ink, fontSize: 15, fontWeight: "700", lineHeight: 22 }}>{main}</Text> : null}
      {cons && cons.total > 0 && (
        <View style={{ gap: 6 }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
            <Text style={{ fontSize: 14 }}>🤝</Text>
            <Text style={{ color: C.text, fontWeight: "700", fontSize: 14, flex: 1 }}>Strategies agree</Text>
            <Text style={[s.muted, { fontSize: 12 }]}>need {cons.min}+ · {cons.total - cons.up.length - cons.down.length} silent</Text>
          </View>
          {[{ k: "UP", n: cons.up, col: C.green }, { k: "DOWN", n: cons.down, col: C.red }].map((r) => (
            <Pressable key={r.k} onPress={() => setShowWhy(!showWhy)} style={{ gap: 2 }}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
                <Text style={{ width: 64, color: r.col, fontWeight: "800", fontSize: 12 }} numberOfLines={1}>{r.k} {r.n.length}</Text>
                <View style={{ flex: 1, height: 6, borderRadius: 3, backgroundColor: C.cardHi, overflow: "hidden" }}>
                  <View style={{ width: `${(r.n.length / cons.total) * 100}%`, height: 6, backgroundColor: r.n.length >= cons.min ? r.col : r.col + "77" }} />
                </View>
                <Text style={[s.muted, { fontSize: 12, width: 30, textAlign: "right" }]}>/{cons.total}</Text>
              </View>
              {r.n.length > 0 && <Text style={[s.muted, { fontSize: 12, marginLeft: 72 }]}>{r.n.join(", ")}</Text>}
            </Pressable>
          ))}
        </View>
      )}
      {pb && (
        <InfoLine icon="⏳" color={C.blue} text={`Waiting for a pullback to the 20-EMA${pb.until_hhmm ? ` until ${pb.until_hhmm}` : ""}`}
          sub={pb.reentry ? "Re-joining a missed move" : "Signal was stretched — buys the retest"} />
      )}
      {ww.window_closed && <InfoLine icon="🕘" color={C.dim} text={ww.window_closed} />}
      {o && o.tradingsymbol && (
        <View style={{ gap: 8, padding: 12, borderRadius: 14, borderWidth: 1, borderColor: (blocked ? C.amber : up ? C.green : C.red) + "40",
          backgroundColor: (blocked ? C.amber : up ? C.green : C.red) + "0D" }}>
          <Pressable onPress={() => setShowOrder(!showOrder)} style={{ gap: 4 }}>
            <View style={s.row}>
              <Badge text={`BUY ${o.option_type ?? (up ? "CE" : "PE")}`} color={up ? C.green : C.red} />
              <Text style={{ color: C.ink, fontWeight: "800", flex: 1 }} numberOfLines={1}>{contractText(o)}</Text>
              <Text style={{ color: C.blue, fontWeight: "800", fontSize: 13 }}>{showOrder ? "Less" : "More"}</Text>
            </View>
            <View style={{ flexDirection: "row", gap: 6 }}>
              {[
                { l: "Entry", v: o.entry_price ? `₹${o.entry_price.toFixed(1)}` : "—" },
                { l: "Stop", v: o.stop_loss?.toFixed(1) ?? "—", col: C.red },
                { l: "Target", v: o.target_price?.toFixed(1) ?? "—", col: C.green },
                { l: `Max loss (${o.lots} lot)`, v: inr(o.est_max_loss), col: C.red },
              ].map((k) => (
                <View key={k.l} style={{ flex: k.l.includes("lot") ? 1.5 : 1, backgroundColor: C.card, borderRadius: 10, padding: 8 }}>
                  <Text style={[s.statLabel, { fontSize: 10 }]} numberOfLines={1}>{k.l}</Text>
                  <Text style={{ color: k.col ?? C.ink, fontWeight: "800", fontSize: 14 }} numberOfLines={1}>{k.v}</Text>
                </View>
              ))}
            </View>
          </Pressable>
          {showOrder && (<>
            <PremiumPanel w={w} />
            <Row gap={8}>
              {o.entry_limit ? <Stat label="Limit (max pay)" value={o.entry_limit.toFixed(2)} /> : null}
              <Stat label="Qty" value={`${o.quantity ?? "—"}`} />
              <Stat label="Risk:reward" value={o.risk_reward_ratio ? `1:${o.risk_reward_ratio}` : "—"} />
            </Row>
            <Text style={[s.muted, { fontSize: 12 }]}>{o.tradingsymbol}</Text>
          </>)}
          {blocked && (
            <View style={{ gap: 4 }}>
              <Text style={{ color: C.amber, fontWeight: "800" }}>⚠ Skipped now</Text>
              {(allProblems ? o.risk_problems : o.risk_problems.slice(0, 1)).map((p, i) =>
                <Text key={i} style={{ color: C.text, fontSize: 13, lineHeight: 18 }}>• {p}</Text>)}
              {o.risk_problems.length > 1 && (
                <Text onPress={() => setAllProblems(!allProblems)} style={{ color: C.blue, fontSize: 12, fontWeight: "700" }}>
                  {allProblems ? "Less" : `+${o.risk_problems.length - 1} more`}</Text>
              )}
            </View>
          )}
        </View>
      )}
      <Text style={[s.muted, { fontSize: 12 }]}>
        Updated {w.as_of.slice(11, 19)}{w.candle_closes_at ? ` · next check ${w.candle_closes_at}` : ""}
      </Text>
    </Card>
  );
}

/** Live premium of the strike Auto Trade would trade (refreshed ~5 s). */
export function PremiumPanel({ w }: { w?: Watch | null }) {
  const o = w?.expected_order;
  if (!w || !o) return null;
  return (
    <>
      {w.option?.ltp ? (() => {
            const op = w.option!;
            const col = (op.change ?? 0) > 0 ? C.green : (op.change ?? 0) < 0 ? C.red : C.dim;
            const tcol = (op.tick ?? 0) > 0 ? C.green : (op.tick ?? 0) < 0 ? C.red : C.dim;
            return (
              <View style={{ padding: 10, borderRadius: 10, backgroundColor: C.card, borderWidth: 1, borderColor: C.border, gap: 4 }}>
                <Text style={s.statLabel}>{o.strike} {o.option_type ?? ""} premium · live</Text>
                <View style={s.row}>
                  <Text style={{ color: C.text, fontSize: 26, fontWeight: "900" }}>₹{(op.ltp ?? 0).toFixed(2)}</Text>
                  {op.tick ? <Text style={{ color: tcol, fontWeight: "800" }}>{op.tick > 0 ? "▲" : "▼"} {Math.abs(op.tick).toFixed(2)}</Text> : null}
                  <View style={{ flex: 1 }} />
                  {op.change !== undefined && (
                    <Text style={{ color: col, fontWeight: "800" }}>{op.change > 0 ? "+" : ""}{op.change} ({op.change_pct}%) today</Text>
                  )}
                </View>
                <Text style={s.muted}>
                  Bid {op.bid ?? "—"} · Ask {op.ask ?? "—"} · H {op.high ?? "—"} · L {op.low ?? "—"}
                </Text>
              </View>
            );
      })() : null}
    </>
  );
}

/** Compact live strip for other tabs: index price/move + the chosen strike's live premium. */
export function LiveStrip({ w, index }: { w?: Watch | null; index: string }) {
  if (!w) return null;
  const moveCol = (w.change ?? 0) > 0 ? C.green : (w.change ?? 0) < 0 ? C.red : C.dim;
  const o = w.expected_order;
  return (
    <Card>
      <View style={s.row}>
        <Text style={{ color: C.text, fontSize: 20, fontWeight: "900", flex: 1 }}>{index} {w.price?.toLocaleString("en-IN") ?? "—"}</Text>
        {w.change !== undefined && (
          <Text style={{ color: moveCol, fontWeight: "800" }}>{w.change > 0 ? "▲ +" : w.change < 0 ? "▼ " : ""}{w.change} pts ({w.change_pct}%)</Text>
        )}
      </View>
      {o?.tradingsymbol ? <Text style={s.muted}>Next strike: {contractText(o)}</Text> : (
        <Text style={s.muted}>No strike yet — waits for a direction.</Text>
      )}
      <PremiumPanel w={w} />
    </Card>
  );
}
