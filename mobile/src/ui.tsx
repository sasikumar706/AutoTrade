import { useSafeAreaInsets } from "react-native-safe-area-context";
import React, { useState } from "react";
import {
  ActivityIndicator, Platform, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View, type RefreshControlProps, type ViewStyle,
} from "react-native";
import Svg, { Line, Path, Text as SvgText } from "react-native-svg";

export const C = {
  // "trading desk" light theme: cool grey canvas, white cards, deep navy ink, one confident blue,
  // and profit/loss colours tuned to stay readable in sunlight
  bg: "#EDF1F7",
  card: "#FFFFFF",
  cardHi: "#F5F7FB",
  border: "#E2E8F0",
  text: "#0F1B2D",
  dim: "#5B6B80",
  green: "#0B8A4C",
  red: "#D92D3A",
  amber: "#B45D09",
  blue: "#2556D9",
  ink: "#0F1B2D",
};

export const pnlColor = (n: number | null | undefined) => (!n ? C.text : n > 0 ? C.green : C.red);

export function Screen({ children, refresh }: { children: React.ReactNode; refresh?: React.ReactElement<RefreshControlProps> }) {
  const insets = useSafeAreaInsets(); // keep the last buttons above Android's navigation bar
  const bottom = Platform.OS === "android" ? Math.max(insets.bottom, 48) : insets.bottom;  // some phones report 0
  return (
    <ScrollView style={{ flex: 1, backgroundColor: C.bg }} contentContainerStyle={{ padding: 16, paddingBottom: 48 + bottom, gap: 12 }}
      keyboardShouldPersistTaps="handled" refreshControl={refresh}>
      {children}
    </ScrollView>
  );
}

export function Card({ title, children, style, right }: { title?: string; children: React.ReactNode; style?: ViewStyle; right?: React.ReactNode }) {
  return (
    <View style={[s.card, style]}>
      {title ? (
        <View style={s.cardHead}>
          <Text style={s.cardTitle}>{title}</Text>
          {right}
        </View>
      ) : null}
      {children}
    </View>
  );
}

/** A card that opens and closes: the title and a one-line summary always show, the details only when opened. */
export function Section({ title, summary, children, open: open0 = false, right }: {
  title: string; summary?: string; children: React.ReactNode; open?: boolean; right?: React.ReactNode;
}) {
  const [open, setOpen] = useState(open0);
  return (
    <View style={s.card}>
      <Pressable onPress={() => setOpen(!open)} hitSlop={6} style={{ gap: 2 }}>
        <View style={s.cardHead}>
          <Text style={[s.cardTitle, { flex: 1 }]}>{title}</Text>
          {right}
          <View style={{ width: 28, height: 28, borderRadius: 14, backgroundColor: open ? C.blue + "14" : C.cardHi, alignItems: "center", justifyContent: "center", marginLeft: 6 }}>
            <Text style={{ color: open ? C.blue : C.dim, fontSize: 12, fontWeight: "800" }}>{open ? "▲" : "▼"}</Text>
          </View>
        </View>
        {summary && !open ? <Text style={{ color: C.dim, fontSize: 13 }} numberOfLines={2}>{summary}</Text> : null}
      </Pressable>
      {open ? children : null}
    </View>
  );
}

export function Btn({ label, onPress, kind = "primary", loading, disabled }: {
  label: string; onPress: () => void; kind?: "primary" | "danger" | "ghost" | "warn"; loading?: boolean; disabled?: boolean;
}) {
  const bg = kind === "primary" ? C.blue : kind === "danger" ? C.red : kind === "warn" ? C.amber : "transparent";
  return (
    <Pressable onPress={onPress} disabled={disabled || loading}
      style={({ pressed }) => [s.btn, { backgroundColor: bg, opacity: disabled ? 0.4 : pressed ? 0.75 : 1 },
        kind === "ghost" && { borderWidth: 1, borderColor: C.border, backgroundColor: C.card },
        kind !== "ghost" && !disabled && { shadowColor: bg, shadowOpacity: 0.25, shadowRadius: 6, shadowOffset: { width: 0, height: 3 }, elevation: 2 }]}>
      {loading ? <ActivityIndicator color={kind === "ghost" ? C.blue : "#fff"} /> : <Text style={[s.btnText, kind === "ghost" && { color: C.text }]}>{label}</Text>}
    </Pressable>
  );
}

/** Square icon button with a short caption under it (for action rows). */
export function IconBtn({ icon, label, onPress, kind = "ghost", loading, disabled }: {
  icon: string; label: string; onPress: () => void; kind?: "primary" | "danger" | "ghost" | "warn"; loading?: boolean; disabled?: boolean;
}) {
  const col = kind === "primary" ? C.blue : kind === "danger" ? C.red : kind === "warn" ? C.amber : C.dim;
  return (
    <Pressable onPress={onPress} disabled={disabled || loading} accessibilityLabel={label}
      style={({ pressed }) => ({ ...(label ? { flex: 1, minWidth: 64, paddingVertical: 8 } : { width: 44, height: 44, justifyContent: "center" as const }),
        alignItems: "center", borderRadius: 12, borderWidth: 1,
        borderColor: col + "88", backgroundColor: col + (pressed ? "33" : "18"), opacity: disabled ? 0.4 : 1, gap: 2 })}>
      {loading ? <ActivityIndicator color={col} /> : <Text style={{ fontSize: 22, color: col }}>{icon}</Text>}
      {label ? <Text style={{ color: col, fontSize: 11, fontWeight: "700" }} numberOfLines={1}>{label}</Text> : null}
    </Pressable>
  );
}

export function Field({ label, value, onChangeText, numeric, placeholder, hint, secure }: {
  label: string; value: string; onChangeText: (v: string) => void; numeric?: boolean; placeholder?: string; hint?: string; secure?: boolean;
}) {
  return (
    <View style={{ flex: 1, minWidth: 130 }}>
      <Text style={s.label}>{label}</Text>
      <TextInput value={value} onChangeText={onChangeText} placeholder={placeholder} placeholderTextColor="#9AA5B4"
        keyboardType={numeric ? "decimal-pad" : "default"} autoCapitalize="none" autoCorrect={false}
        secureTextEntry={secure} style={s.input} />
      {hint ? <Text style={s.hint}>{hint}</Text> : null}
    </View>
  );
}

export function Chips<T extends string>({ options, value, onChange, labels }: {
  options: readonly T[]; value: T; onChange: (v: T) => void; labels?: Partial<Record<T, string>>;
}) {
  return (
    <View style={s.chips}>
      {options.map((o) => (
        <Pressable key={o} onPress={() => onChange(o)} style={[s.chip, value === o && s.chipOn]}>
          <Text style={[s.chipText, value === o && { color: "#fff" }]}>{labels?.[o] ?? o}</Text>
        </Pressable>
      ))}
    </View>
  );
}

export function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <View style={s.row}>
      <Text style={{ color: C.text, flex: 1 }}>{label}</Text>
      <Switch value={value} onValueChange={onChange} trackColor={{ true: C.blue, false: "#CBD5E1" }} thumbColor="#FFFFFF" />
    </View>
  );
}

export function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <View style={s.stat}>
      <Text style={s.statLabel}>{label}</Text>
      <Text style={[s.statValue, color ? { color } : null]}>{value}</Text>
    </View>
  );
}

export function Row({ children, gap = 10 }: { children: React.ReactNode; gap?: number }) {
  return <View style={{ flexDirection: "row", flexWrap: "wrap", gap }}>{children}</View>;
}

export function ErrorText({ msg }: { msg?: string | null }) {
  return msg ? <Text style={{ color: C.red, marginTop: 4 }}>{msg}</Text> : null;
}

export function Badge({ text, color }: { text: string; color: string }) {
  return (
    <View style={{ backgroundColor: color + "1F", borderRadius: 999, paddingHorizontal: 10, paddingVertical: 3, borderWidth: 1, borderColor: color + "40" }}>
      <Text style={{ color, fontWeight: "800", fontSize: 12, letterSpacing: 0.2 }}>{text}</Text>
    </View>
  );
}

/** Simple SVG line chart: equity (or any series) with min/max labels. */
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2026-11-05T10:35:12" → "5 Nov 10:35" (adds seconds when asked). */
export function when(iso?: string | null, seconds = false): string {
  if (!iso || iso.length < 16) return iso ?? "—";
  const m = Number(iso.slice(5, 7));
  return `${Number(iso.slice(8, 10))} ${MONTHS[m - 1] ?? iso.slice(5, 7)} ${iso.slice(11, seconds ? 19 : 16)}`;
}

export function LineChart({ values, height = 160, color = C.blue, baseline, lines = [] }: {
  values: number[]; height?: number; color?: string; baseline?: number; lines?: { value: number; color: string; label: string }[];
}) {
  const [w, setW] = useState(0);
  if (values.length < 2) return <Text style={{ color: C.dim }}>Not enough data to chart</Text>;
  const lv = lines.map((l) => l.value);
  const min = Math.min(...values, baseline ?? Infinity, ...lv);
  const max = Math.max(...values, baseline ?? -Infinity, ...lv);
  const pad = 14;
  const span = max - min || 1;
  const x = (i: number) => (i / (values.length - 1)) * w;
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - pad * 2);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <View onLayout={(e) => setW(e.nativeEvent.layout.width)} style={{ height }}>
      {w > 0 && (
        <Svg width={w} height={height}>
          {baseline !== undefined && (
            <Line x1={0} x2={w} y1={y(baseline)} y2={y(baseline)} stroke={C.dim} strokeDasharray="4 4" strokeWidth={1} />
          )}
          {lines.map((l) => (
            <React.Fragment key={l.label}>
              <Line x1={0} x2={w} y1={y(l.value)} y2={y(l.value)} stroke={l.color} strokeDasharray="6 4" strokeWidth={1.5} />
              <SvgText x={w - 4} y={y(l.value) - 3} fill={l.color} fontSize={10} textAnchor="end">{l.label} {l.value.toFixed(1)}</SvgText>
            </React.Fragment>
          ))}
          <Path d={d} stroke={color} strokeWidth={2} fill="none" />
          <SvgText x={4} y={11} fill={C.dim} fontSize={10}>{Math.round(max).toLocaleString("en-IN")}</SvgText>
          <SvgText x={4} y={height - 3} fill={C.dim} fontSize={10}>{Math.round(min).toLocaleString("en-IN")}</SvgText>
        </Svg>
      )}
    </View>
  );
}

export const s = StyleSheet.create({
  card: { backgroundColor: C.card, borderRadius: 18, padding: 16, borderWidth: 1, borderColor: "#E6EBF2", gap: 12,
    shadowColor: "#1E3A8A", shadowOpacity: 0.07, shadowRadius: 12, shadowOffset: { width: 0, height: 4 }, elevation: 2 },
  cardHead: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 },
  cardTitle: { color: C.ink, fontSize: 16, fontWeight: "800", letterSpacing: 0.1 },
  btn: { borderRadius: 14, paddingVertical: 14, paddingHorizontal: 16, alignItems: "center", justifyContent: "center", minHeight: 50 },
  btnText: { color: "#fff", fontWeight: "800", fontSize: 16, letterSpacing: 0.2 },
  label: { color: C.dim, fontSize: 13, fontWeight: "700", marginBottom: 6 },
  hint: { color: C.dim, fontSize: 12, marginTop: 4 },
  input: { backgroundColor: C.cardHi, color: C.text, borderRadius: 12, borderWidth: 1, borderColor: C.border, paddingHorizontal: 14,
    paddingVertical: 11, fontSize: 16, minHeight: 48 },
  chips: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 999, borderWidth: 1, borderColor: C.border, backgroundColor: C.card, minHeight: 36, justifyContent: "center" },
  chipOn: { backgroundColor: C.blue, borderColor: C.blue },
  chipText: { color: C.text, fontWeight: "700", fontSize: 14 },
  row: { flexDirection: "row", alignItems: "center", gap: 10, minHeight: 44 },
  stat: { backgroundColor: C.cardHi, borderRadius: 14, paddingVertical: 10, paddingHorizontal: 12, minWidth: "30%", flexGrow: 1,
    borderWidth: 1, borderColor: "#EBEFF5" },
  statLabel: { color: C.dim, fontSize: 11, fontWeight: "700", letterSpacing: 0.3, textTransform: "uppercase" },
  statValue: { color: C.ink, fontSize: 18, fontWeight: "800", marginTop: 3, fontVariant: ["tabular-nums"] },
  muted: { color: C.dim, fontSize: 14, lineHeight: 20 },
  text: { color: C.text, fontSize: 15, lineHeight: 21 },
});

const MON_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "NIFTY 25150 PE · ITM · exp 17 Nov" instead of the cut-off NIFTY26N1725150PE */
export function contractText(x: any): string {
  if (!x?.strike) return x?.tradingsymbol ?? "—";
  const ix = /^([A-Z]+?)(?=\d)/.exec(String(x.tradingsymbol ?? ""))?.[1] ?? "";
  const e = String(x.expiry ?? "");
  const exp = e.length >= 10 ? `${Number(e.slice(8, 10))} ${MON_SHORT[Number(e.slice(5, 7)) - 1] ?? ""}` : e;
  return `${ix} ${Number(x.strike)} ${x.option_type ?? String(x.action ?? "").slice(-2)}${x.moneyness ? ` · ${x.moneyness}` : ""}${exp ? ` · exp ${exp}` : ""}`.trim();
}

/** Compact action button: icon + label side by side, 44 px tall (Pause / Stop / Sell all). */
export function ActionBtn({ icon, label, onPress, kind = "ghost", loading, disabled }: {
  icon: string; label: string; onPress: () => void; kind?: "primary" | "danger" | "ghost" | "warn"; loading?: boolean; disabled?: boolean;
}) {
  const col = kind === "primary" ? C.blue : kind === "danger" ? C.red : kind === "warn" ? C.amber : C.ink;
  const solid = kind === "primary";
  return (
    <Pressable onPress={onPress} disabled={disabled || loading} accessibilityLabel={label}
      style={({ pressed }) => ({ flex: 1, minHeight: 44, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
        borderRadius: 12, paddingHorizontal: 10, borderWidth: solid ? 0 : 1, borderColor: kind === "ghost" ? C.border : col + "55",
        backgroundColor: solid ? (pressed ? "#1D47B8" : C.blue) : pressed ? col + "1F" : kind === "ghost" ? C.card : col + "0F",
        opacity: disabled ? 0.4 : 1 })}>
      {loading ? <ActivityIndicator color={solid ? "#fff" : col} size="small" /> : (<>
        <Text style={{ fontSize: 15, color: solid ? "#fff" : col }}>{icon}</Text>
        <Text style={{ color: solid ? "#fff" : col, fontSize: 14, fontWeight: "800" }} numberOfLines={1}>{label}</Text>
      </>)}
    </Pressable>
  );
}

/** One row of tabs that scrolls sideways — never wraps onto a second line. */
export function TabBar<T extends string>({ tabs, value, onChange }: { tabs: { key: T; label: string }[]; value: T; onChange: (t: T) => void }) {
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false}
      contentContainerStyle={{ gap: 4, padding: 4, backgroundColor: C.card, borderRadius: 14, borderWidth: 1, borderColor: C.border }}>
      {tabs.map((t) => {
        const on = t.key === value;
        return (
          <Pressable key={t.key} onPress={() => onChange(t.key)}
            style={{ paddingHorizontal: 14, paddingVertical: 9, borderRadius: 10, backgroundColor: on ? C.blue : "transparent" }}>
            <Text style={{ color: on ? "#fff" : C.dim, fontWeight: "800", fontSize: 14 }}>{t.label}</Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

/** Small icon + text line used for "what's going on" notes. */
export function InfoLine({ icon, text, color, sub }: { icon: string; text: string; color?: string; sub?: string }) {
  return (
    <View style={{ flexDirection: "row", gap: 10, alignItems: "flex-start" }}>
      <Text style={{ fontSize: 15, width: 20, textAlign: "center" }}>{icon}</Text>
      <View style={{ flex: 1, gap: 1 }}>
        <Text style={{ color: color ?? C.text, fontSize: 14, fontWeight: "700", lineHeight: 20 }}>{text}</Text>
        {sub ? <Text style={{ color: C.dim, fontSize: 13, lineHeight: 18 }}>{sub}</Text> : null}
      </View>
    </View>
  );
}

/** Help text kept out of the way: a small "ⓘ" line; the explanation shows only when tapped. */
export function Hint({ children, label = "How it works" }: { children: React.ReactNode; label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Pressable onPress={() => setOpen(!open)} hitSlop={6} style={{ gap: 4 }}>
      <Text style={{ color: C.blue, fontSize: 12, fontWeight: "700" }}>ⓘ {open ? "Hide" : label}</Text>
      {open ? <Text style={{ color: C.dim, fontSize: 13, lineHeight: 18 }}>{children}</Text> : null}
    </Pressable>
  );
}
