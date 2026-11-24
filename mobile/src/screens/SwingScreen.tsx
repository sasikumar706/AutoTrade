import React, { useCallback, useEffect, useState } from "react";
import { Alert, Pressable, RefreshControl, Text, View } from "react-native";
import { api, inr } from "../api";
import { ActionBtn, Badge, Btn, C, Card, Chips, ErrorText, Field, InfoLine, Row, Screen, Section, Stat, contractText, pnlColor, s, when, Hint } from "../ui";
import { usePollOnFocus } from "../usePoll";

type Ix = "NIFTY" | "BANKNIFTY" | "SENSEX";

/** Swing trend: hold a monthly in-the-money CE for weeks while the 10-day average is above the 50-day average. */
export default function SwingScreen({ navigation, route }: any) {
  const [list, setList] = useState<Record<string, any>[]>([]);
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [sel, setSel] = useState<string | null>(route?.params?.id ?? null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [bt, setBt] = useState<Record<string, any> | null>(null);
  const [newIx, setNewIx] = useState<Ix>("BANKNIFTY");
  const [lots, setLots] = useState("1");
  const [maxPrem, setMaxPrem] = useState("");
  const [capital, setCapital] = useState("200000");
  const [alloc, setAlloc] = useState<"10" | "20" | "25">("10");
  const [maxLots, setMaxLots] = useState("10");
  const [sizing, setSizing] = useState<"capital" | "fixed">("capital");
  const [itm, setItm] = useState<"1" | "2" | "3">("2");
  const [book, setBook] = useState<"0" | "4" | "6">("6");
  const [trail, setTrail] = useState<"0" | "3" | "4">("0");
  const [guard, setGuard] = useState<"0" | "1.5" | "2" | "2.5">("2");
  const [maxLoss, setMaxLoss] = useState<"2" | "3" | "1" | "0">("2");
  const [mode, setMode] = useState<"paper" | "live">("paper");

  const load = useCallback(async () => {
    try {
      const l = await api.swings();
      setList(l);
      const id = sel ?? l[0]?.id ?? null;
      if (id && !sel) setSel(id);
      setDetail(id ? await api.swing(id) : null);
      setErr(null);
    } catch (e: any) {
      setErr(e.message);
    }
  }, [sel]);
  usePollOnFocus(load, 10_000);
  useEffect(() => { load(); }, [sel]);
  useEffect(() => {
    if (detail?.config) {
      const c = detail.config;
      setLots(String(c.lots)); setMaxPrem(c.max_premium ? String(c.max_premium) : "");
      setCapital(String(c.capital ?? 200000)); setAlloc((["10", "20", "25"].includes(String(c.alloc_pct)) ? String(c.alloc_pct) : "10") as any);
      setMaxLots(String(c.max_lots ?? 10)); setSizing(c.sizing ?? "capital"); setMode(c.mode ?? "paper");
      setItm(String(c.itm_strikes ?? 2) as any); setBook((["0", "4", "6"].includes(String(c.book_half_pct)) ? String(c.book_half_pct) : "6") as any);
      setTrail((["0", "3", "4"].includes(String(c.trail_atr)) ? String(c.trail_atr) : "0") as any);
      setGuard((["0", "1.5", "2", "2.5"].includes(String(c.crash_guard_atr ?? 2)) ? String(c.crash_guard_atr ?? 2) : "2") as any);
      setMaxLoss((["0", "1", "2", "3"].includes(String(c.max_loss_pct ?? 2)) ? String(c.max_loss_pct ?? 2) : "2") as any);
    }
  }, [detail?.id]);

  async function run(key: string, fn: () => Promise<any>) {
    setBusy(key);
    try { await fn(); await load(); } catch (e: any) { Alert.alert("Swing", e.message); } finally { setBusy(null); }
  }
  const control = (action: string, confirm = "") => run(action, () => api.swingControl(detail!.id, action, confirm));
  function start() {
    if (detail?.config.mode !== "live") return control("enable");
    Alert.alert("Start with REAL money?", "Real orders will be placed in your Zerodha account and held overnight (NRML).",
      [{ text: "Cancel", style: "cancel" }, { text: "Start LIVE", style: "destructive", onPress: () => control("enable", "I UNDERSTAND REAL MONEY") }]);
  }
  async function runBacktest(ix: string) {
    setBusy("bt");
    try { setBt(await api.swingBacktest(ix, detail?.id ?? "")); } catch (e: any) { Alert.alert("Backtest", e.message); } finally { setBusy(null); }
  }

  const w = detail;
  const sig = w?.signal;
  const p = w?.position;
  const open = p && !["POSITION_CLOSED", "ORDER_REJECTED"].includes(p.state);
  return (
    <Screen refresh={<RefreshControl refreshing={false} onRefresh={load} />}>
      <ErrorText msg={err} />
      {list.length > 1 && (
        <Chips options={list.map((x) => x.id) as any} value={sel as any} onChange={(v: any) => setSel(v)}
          labels={Object.fromEntries(list.map((x) => [x.id, `${x.config.index}${x.enabled ? " ●" : ""}`]))} />
      )}

      {w && (
        <View style={[s.card, { gap: 14 }]}>
          <View style={{ flexDirection: "row", alignItems: "flex-start", gap: 10 }}>
            <View style={{ flex: 1 }}>
              <Text style={{ color: C.dim, fontSize: 12, fontWeight: "800", letterSpacing: 0.4 }}>SWING TREND · {w.config.index}</Text>
              <Text style={{ color: sig?.trend === "UP" ? C.green : sig?.trend === "DOWN" ? C.red : C.ink, fontSize: 24, fontWeight: "900" }}>
                {sig ? (sig.trend === "UP" ? "▲ Uptrend — hold" : "▼ Downtrend — stay out") : "Not checked yet"}
              </Text>
              {sig && (
                <Text style={[s.muted, { fontSize: 13 }]}>
                  {w.config.fast}-day avg {Number(sig.fast).toLocaleString("en-IN")} vs {w.config.slow}-day {Number(sig.slow).toLocaleString("en-IN")} ({sig.gap_pct > 0 ? "+" : ""}{sig.gap_pct}%)
                  {sig.since ? ` · since ${when(sig.since + "T00:00").slice(0, -6)}` : ""}
                </Text>
              )}
            </View>
            <View style={{ alignItems: "flex-end", gap: 6 }}>
              <Badge text={w.enabled ? "● RUNNING" : "OFF"} color={w.enabled ? C.green : C.dim} />
              <Badge text={w.config.mode.toUpperCase()} color={w.config.mode === "live" ? C.red : C.blue} />
            </View>
          </View>
          {w.config.sizing === "capital" && w.equity != null && (
            <InfoLine icon="💰" text={`Money for this strategy: ${inr(w.equity)}`}
              sub={`Started ${inr(w.config.capital)} · closed trades ${inr(w.realized ?? 0)} · each buy uses ${w.config.alloc_pct}% (max ${w.config.max_lots} lots)`} />
          )}
          {w.watching && (
            <InfoLine icon="👀" color={w.watching.room_pct < 0.5 ? C.red : C.text}
              text={`Watching every minute: ${w.config.index} ${Number(w.watching.spot).toLocaleString("en-IN")} · sells below ${Number(w.watching.level).toLocaleString("en-IN")}`}
              sub={`${w.watching.room_pct}% room to the crash guard (yesterday ${Number(w.watching.prev_close).toLocaleString("en-IN")} − ${w.config.crash_guard_atr} daily ranges) · ${w.watching.at.slice(11, 16)}`} />
          )}
          <InfoLine icon="🕒" text={`Decides once a day at ${w.config.check_time} on trading days`}
            sub={w.last_check ? `Last checked ${w.last_check}${sig?.checked_at ? ` ${sig.checked_at.slice(11, 16)}` : ""}` : "Not checked yet"} />
          <View style={{ flexDirection: "row", gap: 8 }}>
            {w.enabled
              ? <ActionBtn icon="⏹" label="Stop" kind="danger" onPress={() => control("disable")} loading={busy === "disable"} />
              : <ActionBtn icon="▶" label="Start" kind={w.config.mode === "live" ? "warn" : "primary"} onPress={start} loading={busy === "enable"} />}
            <ActionBtn icon="↻" label="Check now" onPress={() => control("check_now")} loading={busy === "check_now"} />
            {open && <ActionBtn icon="💰" label="Sell" kind="danger" onPress={() =>
              Alert.alert("Sell the swing position now?", "It will buy again at the next daily check if the trend is still up — Stop it first to stay out.",
                [{ text: "Cancel", style: "cancel" }, { text: "Sell", style: "destructive", onPress: () => control("close_position") }])}
              loading={busy === "close_position"} />}
          </View>
        </View>
      )}

      {w && (
        <Card title={open ? "Holding" : "Position"}>
          {!p && <Text style={s.muted}>No position yet. When the trend is up at the daily check, it buys a monthly in-the-money CE.</Text>}
          {p && (
            <Pressable onPress={() => navigation.navigate("Position", { id: p.id })} style={{ gap: 8 }}>
              <View style={s.row}>
                <Badge text={open ? "OPEN" : p.state === "POSITION_CLOSED" ? "CLOSED" : p.state} color={open ? C.green : C.dim} />
                <Text style={{ color: C.ink, fontWeight: "800", flex: 1 }} numberOfLines={1}>{contractText({ ...p, option_type: "CE" })}</Text>
              </View>
              <Row gap={8}>
                <Stat label="Entry" value={p.entry_price ? `₹${p.entry_price.toFixed(1)}` : "—"} />
                <Stat label="Now" value={p.ltp ? `₹${p.ltp.toFixed(1)}` : "—"} />
                <Stat label={open ? "Open P&L" : "Net"} value={inr(open ? p.unrealized : p.net)} color={pnlColor(open ? p.unrealized : p.net)} />
              </Row>
              <Text style={s.muted}>
                {p.qty} qty{p.days_left != null ? ` · ${p.days_left} days to expiry (rolls at ${w.config.roll_days})` : ""}
                {p.stop ? ` · emergency stop ₹${p.stop.toFixed(1)}` : ""}{p.exit_reason ? ` · exit: ${p.exit_reason.replace("_", " ")}` : ""}
              </Text>
            </Pressable>
          )}
        </Card>
      )}

      {w && (w.trades?.length ?? 0) > 0 && (
        <Section detail title="📒 Swing trades" summary={`${w.trades.length} · net ${inr(w.trades.reduce((a: number, t: any) => a + (t.net ?? 0), 0))}`}>
          {w.trades.map((t: any) => (
            <Pressable key={t.id} onPress={() => navigation.navigate("Position", { id: t.id })}
              style={{ flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: C.border }}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontWeight: "700" }} numberOfLines={1}>{contractText({ ...t, option_type: "CE" })}</Text>
                <Text style={[s.muted, { fontSize: 12 }]}>
                  {t.entry_time ? when(t.entry_time) : "—"}{t.exit_time ? ` → ${when(t.exit_time)}` : " → open"}{t.exit_reason ? ` · ${t.exit_reason.replace("_", " ")}` : ""}
                </Text>
              </View>
              <Text style={{ color: pnlColor(t.net ?? t.unrealized), fontWeight: "800" }}>{inr(t.net ?? t.unrealized)}</Text>
            </Pressable>
          ))}
        </Section>
      )}

      <Card detail title="📊 How this rule did on past data">
        <Hint>Runs the same rule on up to 10 years of daily {w?.config.index ?? newIx} candles from Kite, 1 lot, option priced from India VIX.</Hint>
        <Btn label={busy === "bt" ? "Testing…" : `Test on ${w?.config.index ?? newIx}`} kind="ghost" onPress={() => runBacktest(w?.config.index ?? newIx)} loading={busy === "bt"} />
        {bt && (<>
          <Text style={[s.muted, { fontSize: 13 }]}>{bt.from} → {bt.to} · {bt.index}</Text>
          <Row gap={8}>
            <Stat label="Net (1 lot)" value={inr(bt.net)} color={pnlColor(bt.net)} />
            <Stat label="Per year" value={inr(bt.per_year)} color={pnlColor(bt.per_year)} />
            <Stat label="Trades · wins" value={`${bt.trades} · ${bt.wins}`} />
          </Row>
          <Row gap={8}>
            <Stat label="Worst drop" value={inr(bt.max_drawdown)} color={C.red} />
            <Stat label="In market" value={`${bt.in_market_pct}% of days`} />
            <Stat label="Profit factor" value={bt.profit_factor != null ? String(bt.profit_factor) : "—"} />
          </Row>
          {bt.sizing === "capital" && (
            <View style={{ padding: 10, borderRadius: 12, backgroundColor: pnlColor(bt.capital_end - bt.capital_start) + "12", gap: 4 }}>
              <Text style={{ color: C.ink, fontWeight: "800" }}>
                {inr(bt.capital_start)} → {inr(bt.capital_end)} with lots growing/shrinking ({bt.alloc_pct}% per buy, {bt.lots_range?.[0]}–{bt.lots_range?.[1]} lots)
              </Text>
              <Text style={s.muted}>Worst drop along the way {inr(bt.capital_worst_drop)}</Text>
              {Object.entries(bt.capital_yearly_pct ?? {}).map(([y, v]: any) => (
                <View key={y} style={{ flexDirection: "row" }}>
                  <Text style={[s.muted, { flex: 1 }]}>{y}</Text>
                  <Text style={{ color: pnlColor(v), fontWeight: "700" }}>{v > 0 ? "+" : ""}{v}%</Text>
                </View>
              ))}
            </View>
          )}
          <Hint>
            Just holding 1 lot's worth of the index made {inr(bt.buy_hold)} (worst drop {inr(bt.buy_hold_max_drawdown)}). The rule's job is
            to keep most of the uptrend while stepping aside in big falls.
          </Hint>
          <View style={{ gap: 2 }}>
            {Object.entries(bt.yearly ?? {}).map(([y, v]: any) => (
              <View key={y} style={{ flexDirection: "row" }}>
                <Text style={[s.muted, { flex: 1 }]}>{y}</Text>
                <Text style={{ color: pnlColor(v), fontWeight: "700" }}>{inr(v)}</Text>
              </View>
            ))}
          </View>
          <Text style={[s.muted, { fontSize: 12 }]}>{bt.note}{bt.vix_history ? "" : " (India VIX history unavailable — used 15%.)"}</Text>
        </>)}
      </Card>

      {w && (
        <Section title="⚙️ Settings" summary={`${w.config.sizing === "capital" ? `${w.config.alloc_pct}% of money, max ${w.config.max_lots} lots` : `${w.config.lots} lot`} · ${w.config.itm_strikes ?? 2} ITM · guard ${w.config.crash_guard_atr ? `${w.config.crash_guard_atr} ATR` : "off"} · book half ${w.config.book_half_pct ? `+${w.config.book_half_pct}%` : "off"}${w.config.trail_atr ? ` · trail ${w.config.trail_atr} ATR` : ""} · check ${w.config.check_time} · ${w.config.max_loss_pct ? `max loss ${w.config.max_loss_pct}%` : `stop −${w.config.disaster_stop_pct}%`}${w.config.max_premium ? ` · cap ₹${w.config.max_premium}` : ""}`}>
          <Text style={s.label}>Money</Text>
          <Chips options={["paper", "live"] as const} value={mode} onChange={setMode}
            labels={{ paper: "📝 Paper (simulated)", live: "💹 Live (real money)" }} />
          {mode === "live" && (
            <Text style={{ color: C.red, fontSize: 13, lineHeight: 18 }}>
              Real NRML orders in your Zerodha account, held overnight for weeks. Needs the static-IP server with live trading on.
              Stop the strategy before switching, then Start again — you'll confirm real money. Use "Starting money" ≤ what you can afford to lose.
            </Text>
          )}
          <Text style={s.label}>Lots</Text>
          <Chips options={["capital", "fixed"] as const} value={sizing} onChange={setSizing}
            labels={{ capital: "Grow with my money", fixed: "Fixed lots" }} />
          {sizing === "capital" ? (<>
            <Row>
              <Field label="Starting money ₹" numeric value={capital} onChangeText={setCapital} />
              <Field label="Max lots" numeric value={maxLots} onChangeText={setMaxLots} />
            </Row>
            <Text style={s.label}>Put into premium at each buy</Text>
            <Chips options={["10", "20", "25"] as const} value={alloc} onChange={setAlloc} labels={{ "10": "10% (recommended)", "20": "20% (bigger swings)", "25": "25%" }} />
            <Hint>Lots = that % of your current money ÷ one lot's premium. After profits it buys more lots, after losses fewer. Above 25% the past data shows it can wipe out the money.</Hint>
          </>) : (
            <Field label="Lots" numeric value={lots} onChangeText={setLots} />
          )}
          <Field label="Max premium ₹ per buy (0 = none)" numeric value={maxPrem} onChangeText={setMaxPrem} />
          <Text style={s.label}>Strike</Text>
          <Chips options={["1", "2", "3"] as const} value={itm} onChange={setItm} labels={{ "1": "1 in the money", "2": "2 in the money (recommended)", "3": "3 in the money" }} />
          <Text style={s.label}>Lock profit: book half the lots when the index is up</Text>
          <Chips options={["6", "4", "0"] as const} value={book} onChange={setBook} labels={{ "6": "+6% (recommended)", "4": "+4%", "0": "Off" }} />
          <Hint>More trades end in profit and the money swings less; in exchange the biggest trends earn a bit less. Needs 2+ lots.</Hint>
          <Text style={s.label}>Max loss per trade (sets the stop-loss)</Text>
          <Chips options={["2", "1", "3", "0"] as const} value={maxLoss} onChange={setMaxLoss}
            labels={{ "2": "2% of money (recommended)", "1": "1%", "3": "3%", "0": "Off (−60% stop)" }} />
          <Text style={s.label}>Crash guard — watched every minute while holding</Text>
          <Chips options={["2", "1.5", "2.5", "0"] as const} value={guard} onChange={setGuard}
            labels={{ "2": "2 daily ranges (recommended)", "1.5": "1.5", "2.5": "2.5", "0": "Off" }} />
          <Hint>Sells at once if the index falls this far below yesterday's close during the day, then waits for a fresh uptrend. In 15 years of data it roughly halved the worst drop.</Hint>
          <Text style={s.label}>Trailing exit (also sell after a sharp drop from the best close)</Text>
          <Chips options={["0", "3", "4"] as const} value={trail} onChange={setTrail} labels={{ "0": "Off", "3": "3 daily ranges", "4": "4 daily ranges" }} />
          <Btn label="Save" onPress={() => run("save", () => api.updateSwing(w.id, { ...w.config, mode, sizing, lots: Number(lots) || 1, max_premium: Number(maxPrem) || 0,
            capital: Number(capital) || 200000, alloc_pct: Number(alloc), max_lots: Number(maxLots) || 10,
            itm_strikes: Number(itm), book_half_pct: Number(book), trail_atr: Number(trail), crash_guard_atr: Number(guard), max_loss_pct: Number(maxLoss) }))}
            loading={busy === "save"} />
          {!open && <Btn label="Delete this swing strategy" kind="ghost" onPress={() =>
            Alert.alert("Delete?", "Its trades stay in your history.", [{ text: "Cancel", style: "cancel" },
              { text: "Delete", style: "destructive", onPress: () => run("del", async () => { await api.deleteSwing(w.id); setSel(null); }) }])} />}
        </Section>
      )}

      {w && (w.timeline?.length ?? 0) > 0 && (
        <Section detail title="📜 Log" summary={w.timeline[w.timeline.length - 1]?.msg}>
          {[...w.timeline].reverse().slice(0, 30).map((e: any, i: number) => (
            <Text key={i} style={[s.muted, { fontSize: 13 }]}>{when(e.t)} · {e.msg}</Text>
          ))}
        </Section>
      )}

      <Card title={w ? "Add another index" : "Start a swing strategy"}>
        <Hint>
          Buys a monthly in-the-money CE when the {10}-day average of the index is above its {50}-day average, holds it for weeks (rolling
          before expiry) and sells when the trend turns down. Never bets on falls. Paper first.
        </Hint>
        <Chips options={["NIFTY", "BANKNIFTY", "SENSEX"] as const} value={newIx} onChange={setNewIx} />
        <Btn label="Create (paper)" onPress={() => run("create", async () => {
          const c = await api.createSwing({ index: newIx, lots: Number(lots) || 1, mode: "paper", max_premium: Number(maxPrem) || 0,
            sizing, capital: Number(capital) || 200000, alloc_pct: Number(alloc), max_lots: Number(maxLots) || 10,
            itm_strikes: Number(itm), book_half_pct: Number(book), trail_atr: Number(trail), crash_guard_atr: Number(guard), max_loss_pct: Number(maxLoss) });
          setSel(c.id);
        })} loading={busy === "create"} />
      </Card>
    </Screen>
  );
}
