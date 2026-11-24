import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useEffect, useState } from "react";
import { Alert, Pressable, Text, View } from "react-native";
import { api, pct, type AnalysisItem, type AnalyzeResult, type FinderRow, type IndexKey, type Instrument } from "../api";
import { useMode } from "../mode";
import { IndexTicker, useIndexMoves } from "../IndexTicker";
import { lightEmoji, rowFavour, simpleSignal } from "../signal";
import { ALL_INTERVALS, INTRADAY, InstrumentPicker, type Interval } from "../StrategyForm";
import { Badge, Btn, C, Card, Chips, ErrorText, Row, Screen, Stat, Toggle, pnlColor, s, Hint } from "../ui";

const iso = (d: Date) => d.toISOString().slice(0, 10);
const daysAgo = (n: number) => iso(new Date(Date.now() - n * 86400_000));
// History used per interval: enough candles for a meaningful train/test split.
const LOOKBACK: Record<string, number> = {
  minute: 20, "3minute": 45, "5minute": 75, "10minute": 120, "15minute": 180,
  "30minute": 300, "60minute": 500, day: 1825,
};

const verdictColor = (v: string) => (v === "BULLISH" ? C.green : v === "BEARISH" ? C.red : C.amber);
const actionColor = (a: string) =>
  a === "BUY" || a === "HOLD LONG" ? C.green : a === "SELL" || a === "HOLD SHORT" ? C.red : a === "EXIT" ? C.amber : C.dim;

function ScoreBar({ score }: { score: number }) {
  const pos = (score + 100) / 2; // 0..100
  return (
    <View style={{ height: 10, borderRadius: 5, backgroundColor: C.cardHi, overflow: "hidden" }}>
      <View style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 2, backgroundColor: C.border }} />
      <View style={{
        position: "absolute", top: 0, bottom: 0,
        left: `${Math.min(pos, 50)}%`, width: `${Math.abs(pos - 50)}%`,
        backgroundColor: score >= 0 ? C.green : C.red,
      }} />
    </View>
  );
}

// One-tap index picks (spot index; the AI's CE/PE ideas are shown only for these).
const INDEX_INST: Record<IndexKey, Instrument> = {
  NIFTY: { instrument_token: 256265, exchange: "NSE", tradingsymbol: "NIFTY 50", name: "NIFTY 50", instrument_type: "EQ", segment: "INDICES", lot_size: 1 },
  BANKNIFTY: { instrument_token: 260105, exchange: "NSE", tradingsymbol: "NIFTY BANK", name: "NIFTY BANK", instrument_type: "EQ", segment: "INDICES", lot_size: 1 },
  SENSEX: { instrument_token: 265, exchange: "BSE", tradingsymbol: "SENSEX", name: "SENSEX", instrument_type: "EQ", segment: "INDICES", lot_size: 1 },
};
const ivLabel: Record<string, string> = { minute: "1m", "3minute": "3m", "5minute": "5m", "10minute": "10m", "15minute": "15m", "30minute": "30m", "60minute": "1h", day: "1D" };
const when = (t: number) => new Date(t * 1000).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export default function AiScreen({ navigation, route }: any) {
  const [inst, setInst] = useState<Instrument | null>(null);
  // opened from a Market quote: pre-select that stock/index
  useEffect(() => {
    const p = route?.params?.inst as Instrument | undefined;
    if (!p) return;
    const idx = (Object.keys(INDEX_INST) as IndexKey[]).find((k) => INDEX_INST[k].tradingsymbol === p.tradingsymbol && INDEX_INST[k].exchange === p.exchange);
    setInst(idx ? INDEX_INST[idx] : p);
  }, [route?.params?.nonce]);
  const [interval, setInterval_] = useState<Interval>("15minute");
  const [product, setProduct] = useState<"MIS" | "CNC" | "NRML">("MIS");
  const [allowShort, setAllowShort] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<AnalyzeResult | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [saveHistory, setSaveHistory] = useState(true);
  const [history, setHistory] = useState<AnalysisItem[]>([]);
  const [opening, setOpening] = useState<string | null>(null);
  const { setMode } = useMode();
  const moves = useIndexMoves();

  const loadHistory = useCallback(() => { api.analyses().then(setHistory).catch(() => {}); }, []);
  useFocusEffect(useCallback(() => { loadHistory(); }, [loadHistory]));

  async function openPast(id: string) {
    setOpening(id);
    try {
      setRes(await api.getAnalysis(id));
      setErr(null);
    } catch (e: any) {
      Alert.alert("Could not open", e.message);
    } finally {
      setOpening(null);
    }
  }

  function deletePast(it: AnalysisItem) {
    Alert.alert("Delete this analysis?", `${it.title} · ${ivLabel[it.interval] ?? it.interval} · ${when(it.created)}`, [
      { text: "Cancel", style: "cancel" },
      { text: "Delete", style: "destructive", onPress: async () => {
        try {
          await api.deleteAnalysis(it.id);
          if (res?.id === it.id) setRes(null);
          loadHistory();
        } catch (e: any) { Alert.alert("Delete failed", e.message); }
      } },
    ]);
  }

  function clearAll() {
    Alert.alert("Clear all past analyses?", `${history.length} saved analyses will be deleted.`, [
      { text: "Cancel", style: "cancel" },
      { text: "Clear all", style: "destructive", onPress: async () => {
        try { await api.clearAnalyses(); setHistory([]); setRes(null); } catch (e: any) { Alert.alert("Clear failed", e.message); }
      } },
    ]);
  }

  /** Switch to Paper/Live mode and open the option ticket with BUY <index> <CE|PE> ATM pre-filled. */
  function tradeOption(r: FinderRow | null, optionType: "CE" | "PE", target: "paper" | "live") {
    const index = res?.index_key;
    if (!index) return;
    const go = () => {
      setMode(target);
      navigation.navigate("Trade", { prefill: {
        index, optionType, nonce: Date.now(),
        note: `AI Finder${r ? `: ${r.name} says ${r.now.action}` : ""} → BUY ${index} ${optionType} (ATM)`,
      } });
    };
    if (target === "paper") return go();
    Alert.alert("Switch to LIVE mode?", `Opens the live order ticket with BUY ${index} ${optionType} (ATM) filled in. Nothing is sent until you review and confirm the order.`, [
      { text: "Cancel", style: "cancel" },
      { text: "Open live ticket", style: "destructive", onPress: go },
    ]);
  }

  const isFno = ["FUT", "CE", "PE"].includes(inst?.instrument_type ?? "");
  const products = (isFno ? ["MIS", "NRML"] : ["MIS", "CNC"]) as ("MIS" | "CNC" | "NRML")[];
  const effProduct = interval === "day" && product === "MIS" ? (isFno ? "NRML" : "CNC") : product;
  const inst_ = inst;
  const interval_ = interval;

  async function analyze(over?: { inst: Instrument; interval: Interval; product: string; allowShort: boolean }) {
    const inst = over?.inst ?? inst_;
    const interval = over?.interval ?? interval_;
    const prod = over?.product ?? effProduct;
    const short = over ? over.allowShort : allowShort && effProduct !== "CNC";
    if (!inst) return setErr("Pick an instrument first");
    setErr(null);
    setBusy(true);
    setRes(null);
    try {
      setRes(await api.analyze({
        exchange: inst.exchange, tradingsymbol: inst.tradingsymbol, interval,
        from_date: daysAgo(LOOKBACK[interval] ?? 180), to_date: daysAgo(0),
        product: prod, allow_short: short, save: saveHistory,
      }));
      loadHistory();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const resInst = res?.instrument;
  const resProduct = res?.finder?.product ?? res?.product ?? effProduct;
  const resShort = (res?.allow_short ?? allowShort) && resProduct !== "CNC";

  async function backtestThis(r: FinderRow) {
    if (!resInst?.exchange || !resInst.tradingsymbol || !res) return;
    setActing(r.strategy + "bt");
    try {
      const out = await api.backtest({
        exchange: resInst.exchange, tradingsymbol: resInst.tradingsymbol, interval: res.interval,
        from_date: daysAgo(LOOKBACK[res.interval] ?? 180), to_date: daysAgo(0),
        strategy: r.strategy, params: r.params, product: resProduct, allow_short: resShort,
      });
      navigation.navigate("BacktestResult", { result: out });
    } catch (e: any) {
      Alert.alert("Backtest failed", e.message);
    } finally {
      setActing(null);
    }
  }

  function paperThis(r: FinderRow) {
    const inst = resInst;
    if (!inst?.exchange || !inst.tradingsymbol || !res) return;
    if (!(INTRADAY as readonly string[]).includes(res.interval)) {
      return Alert.alert("Paper trading", "Paper sessions need an intraday interval (1m–60m). Re-run the analysis on an intraday interval.");
    }
    Alert.alert("Start paper trading?", `${r.name} ${JSON.stringify(r.params)} on ${inst.tradingsymbol} · ${res.interval}\nQty ${inst.lot_size || 1} · simulated orders, no real money.`, [
      { text: "Cancel", style: "cancel" },
      {
        text: "Start", onPress: async () => {
          setActing(r.strategy + "pt");
          try {
            const snap = await api.startSession({
              mode: "paper", exchange: inst.exchange, tradingsymbol: inst.tradingsymbol, interval: res.interval,
              strategy: r.strategy, params: r.params, quantity: inst.lot_size || 1,
              product: resProduct, allow_short: resShort, stop_loss_pct: 1, target_pct: 2,
              max_trades_per_day: 5, max_daily_loss: 1000,
            });
            setMode("paper");
            navigation.navigate("Session", { id: snap.id });
          } catch (e: any) {
            Alert.alert("Could not start", e.message);
          } finally {
            setActing(null);
          }
        },
      },
    ]);
  }

  const snap = res?.snapshot;
  return (
    <Screen>
      <IndexTicker moves={moves} selected={(Object.keys(INDEX_INST) as IndexKey[]).find((k) => INDEX_INST[k].tradingsymbol === inst?.tradingsymbol) ?? null}
        onPress={(k) => setInst(INDEX_INST[k])} />
      <Card title="AI Strategy Finder">
        <Hint>
          Reads the technical indicators, then tests every strategy and setting on older data and ranks them on newer data they
          never saw. Takes 10–60 seconds.
        </Hint>
        <Text style={s.label}>Index (shows CE/PE trade ideas)</Text>
        <Chips options={["NIFTY", "BANKNIFTY", "SENSEX", "OTHER"] as const}
          value={(Object.keys(INDEX_INST) as IndexKey[]).find((k) => INDEX_INST[k].tradingsymbol === inst?.tradingsymbol) ?? "OTHER"}
          onChange={(k) => setInst(k === "OTHER" ? null : INDEX_INST[k])} labels={{ OTHER: "Other…" }} />
        <InstrumentPicker value={inst} onChange={setInst} />
        <Text style={s.label}>Timeframe</Text>
        <Chips options={ALL_INTERVALS} value={interval} onChange={setInterval_}
          labels={{ minute: "1m", "3minute": "3m", "5minute": "5m", "10minute": "10m", "15minute": "15m", "30minute": "30m", "60minute": "1h", day: "1D" }} />
        <Text style={s.label}>Trading style</Text>
        <Chips options={products} value={product} onChange={setProduct}
          labels={{ MIS: "Intraday (MIS)", CNC: "Delivery (CNC)", NRML: "F&O carry (NRML)" }} />
        {interval === "day" && product === "MIS" && <Text style={s.muted}>Daily candles can't be intraday — using {effProduct}.</Text>}
        {effProduct !== "CNC" && <Toggle label="Allow short trades" value={allowShort} onChange={setAllowShort} />}
        <Toggle label="Save to past analyses" value={saveHistory} onChange={setSaveHistory} />
        <ErrorText msg={err} />
        <Btn label={busy ? "Analysing… (testing strategies)" : "Analyse with AI"} onPress={() => analyze()} loading={busy} />
      </Card>

      {res && snap && (
        <>
          <View style={[s.row, { paddingHorizontal: 4 }]}>
            <Text style={[s.text, { flex: 1, fontWeight: "700" }]} numberOfLines={1}>
              {res.instrument.exchange}:{res.instrument.tradingsymbol} · {ivLabel[res.interval] ?? res.interval}
              {res.created ? <Text style={s.muted}>  {when(res.created)}</Text> : null}
            </Text>
            <Pressable onPress={() => setRes(null)} hitSlop={10}><Text style={{ color: C.blue, fontWeight: "700" }}>Close ✕</Text></Pressable>
          </View>

          {res.forecast && (() => {
            const f = res.forecast!;
            const col = f.direction === "UP" ? C.green : f.direction === "DOWN" ? C.red : C.dim;
            const row = res.finder?.ranking.find((r) => r.strategy === f.strategy_key && JSON.stringify(r.params) === JSON.stringify(f.params));
            const fmt = (n?: number | null) => (n == null ? "—" : n.toLocaleString("en-IN", { maximumFractionDigits: 1 }));
            return (
              <Card title="AI forecast" style={{ borderColor: col, borderWidth: 2, backgroundColor: col + "12" }}
                right={<Badge text={f.direction === "NONE" ? "NO CALL" : f.direction === "UP" ? "▲ UP" : "▼ DOWN"} color={col} />}>
                <Text style={{ color: col, fontSize: 20, fontWeight: "900" }}>
                  {f.direction === "UP" ? "▲ " : f.direction === "DOWN" ? "▼ " : ""}{f.headline}
                </Text>
                {f.direction !== "NONE" && (
                  <>
                    <Row gap={8}>
                      <Stat label="Now" value={fmt(f.close)} />
                      <Stat label={`Target (${f.direction === "UP" ? "+" : "−"}${fmt(f.expected_pts)} pts)`} value={fmt(f.target_price)} color={C.green} />
                      <Stat label={`Stop (${f.direction === "UP" ? "−" : "+"}${fmt(f.risk_pts)} pts)`} value={fmt(f.stop_price)} color={C.red} />
                      <Stat label="Typical hold" value={f.hold_minutes != null ? `${f.hold_minutes} min` : "—"} />
                      <Stat label="Won on unseen data" value={`${f.win_rate_pct}% of ${f.trades}`} />
                      <Stat label="Average edge" value={f.edge_pts != null ? `${f.edge_pts > 0 ? "+" : ""}${f.edge_pts} pts` : "—"} color={pnlColor(f.edge_pts)} />
                    </Row>
                    <Text style={s.muted}>
                      Points are the average win and loss of {f.strategy}'s own trades on data it had never seen.
                      {f.agree_with_indicators ? " The indicators agree." : " The indicators do NOT agree — treat with extra care."}
                      {!f.fresh ? " The signal started earlier; part of the move may be done." : ""}
                    </Text>
                    {res.index_key ? (
                      <Row>
                        <View style={{ flex: 1 }}><Btn label={`📝 Paper ${f.direction === "UP" ? "CE" : "PE"}`}
                          onPress={() => tradeOption(row ?? null, f.direction === "UP" ? "CE" : "PE", "paper")} /></View>
                        <View style={{ flex: 1 }}><Btn label={`💰 Live ${f.direction === "UP" ? "CE" : "PE"}`} kind="warn"
                          onPress={() => tradeOption(row ?? null, f.direction === "UP" ? "CE" : "PE", "live")} /></View>
                      </Row>
                    ) : row ? (
                      <Row>
                        <View style={{ flex: 1 }}><Btn label="Paper trade this" onPress={() => paperThis(row)} /></View>
                      </Row>
                    ) : null}
                  </>
                )}
                <Text style={[s.muted, { fontSize: 12 }]}>A forecast from past patterns, not a guarantee. Check it on paper first.</Text>
              </Card>
            );
          })()}

          {res.index_key && (() => {
            const sig = simpleSignal(res, moves?.[res.index_key]);
            const opt = sig.option;
            return (
              <Card style={{ borderColor: sig.color, borderWidth: 2, backgroundColor: sig.color + "14" }}>
                <Text style={{ color: sig.color, fontSize: 22, fontWeight: "900" }}>{lightEmoji[sig.light]} {sig.headline}</Text>
                <Text style={[s.text, { lineHeight: 21 }]}>{sig.plain}</Text>
                <View style={{ gap: 4 }}>
                  {sig.checks.map((c, i) => (
                    <Text key={i} style={{ color: c.ok ? C.green : C.red, fontSize: 13 }}>{c.ok ? "✓" : "✗"}  <Text style={{ color: C.text }}>{c.label}</Text></Text>
                  ))}
                </View>
                {opt && (
                  <Row>
                    <View style={{ flex: 1 }}><Btn label={`📝 Paper ${opt}`} onPress={() => tradeOption(sig.idea, opt, "paper")} /></View>
                    {sig.liveOk && <View style={{ flex: 1 }}><Btn label={`💰 Live ${opt}`} kind="warn" onPress={() => tradeOption(sig.idea, opt, "live")} /></View>}
                  </Row>
                )}
                <Hint>
                  {sig.liveOk ? "" : "Live is offered only on a 🟢 green signal. "}
                  Only option buying is suggested, with a stop-loss — the most you can lose is the premium. No signal is right every time; start with 1 lot.
                </Hint>
                {sig.stale && <Btn label="Re-run analysis now" kind="ghost" loading={busy} onPress={() => {
                  const i = INDEX_INST[res.index_key!];
                  setInst(i); setInterval_(res.interval as Interval);
                  analyze({ inst: i, interval: res.interval as Interval, product: resProduct, allowShort: resShort });
                }} />}
              </Card>
            );
          })()}

          <Card title="Summary" right={<Badge text={res.summary_source === "claude" ? "Claude AI" : "Built-in"} color={C.blue} />}>
            <Text style={[s.text, { lineHeight: 21 }]}>{res.summary}</Text>
            {res.ai_error ? <Text style={s.muted}>Claude unavailable ({res.ai_error}); showing built-in summary.</Text> : null}
            {!res.ai_available ? <Text style={s.muted}>Tip: add ANTHROPIC_API_KEY to backend/.env for a deeper Claude-written analysis.</Text> : null}
          </Card>

          <Card detail title="Market view" right={<Badge text={snap.verdict} color={verdictColor(snap.verdict)} />}>
            <ScoreBar score={snap.score} />
            <Row gap={8}>
              <Stat label="Score" value={`${snap.score > 0 ? "+" : ""}${snap.score}`} color={verdictColor(snap.verdict)} />
              <Stat label="Regime" value={snap.regime} />
              <Stat label="Volatility (ATR)" value={`${snap.volatility_atr_pct}%`} />
            </Row>
            <Text style={s.muted}>Last close {snap.close} at {snap.as_of.replace("T", " ").slice(0, 16)} · best suited: {snap.suggested_style}</Text>
            {snap.signals.map((x, i) => (
              <View key={i} style={{ flexDirection: "row", gap: 8, paddingVertical: 6, borderTopWidth: 1, borderTopColor: C.border }}>
                <Text style={{ color: x.vote > 0 ? C.green : x.vote < 0 ? C.red : C.dim, fontWeight: "800", width: 18 }}>
                  {x.vote > 0 ? "▲" : x.vote < 0 ? "▼" : "•"}
                </Text>
                <View style={{ flex: 1 }}>
                  <Text style={[s.text, { fontWeight: "700" }]}>{x.indicator} <Text style={s.muted}>{String(x.value ?? "")}</Text></Text>
                  <Text style={s.muted}>{x.reason}</Text>
                </View>
              </View>
            ))}
          </Card>

          {res.finder && (
            <Card title="Best strategies (ranked on unseen data)">
              <Text style={s.muted}>
                Tuned on {res.finder.train_period[0].slice(0, 10)} → {res.finder.train_period[1].slice(0, 10)}, tested on{" "}
                {res.finder.test_period[0].slice(0, 10)} → {res.finder.test_period[1].slice(0, 10)} · {res.finder.combinations_tested} settings tried ·
                buy & hold in test: {pct(res.finder.buy_and_hold_test_pct)}
              </Text>
              {res.finder.ranking.length === 0 && <Text style={s.muted}>No strategy produced enough trades.</Text>}
              {res.finder.ranking.map((r, i) => (
                <View key={i} style={{ paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border, gap: 6 }}>
                  <View style={s.row}>
                    <Text style={[s.text, { fontWeight: "800", flex: 1 }]}>#{i + 1} {r.name}</Text>
                    {(() => { const f = rowFavour(r); return <Badge text={f.label} color={f.color} />; })()}
                    <Badge text={r.option_type ? `${r.option_type === "CE" ? "▲ BUY CE" : "▼ BUY PE"}` : r.now.action}
                      color={r.option_type ? rowFavour(r).color : actionColor(r.now.action)} />
                  </View>
                  <Text style={s.muted}>{Object.entries(r.params).map(([k, v]) => `${k} ${v}`).join(" · ")}</Text>
                  <Row gap={8}>
                    <Stat label="Unseen-data return" value={pct(r.test.total_return_pct)} color={pnlColor(r.test.total_return_pct)} />
                    <Stat label="Trades / win %" value={`${r.test.trades} / ${r.test.win_rate_pct}%`} />
                    <Stat label="Max DD" value={`${r.test.max_drawdown_pct}%`} color={C.red} />
                  </Row>
                  <Text style={s.muted}>Tuning period: {pct(r.train.total_return_pct)} over {r.train.trades} trades{r.now.since ? ` · in position since ${r.now.since.replace("T", " ").slice(0, 16)}` : ""}</Text>
                  {res.index_key ? (
                    <Row>
                      {r.option_type ? (
                        <>
                          <View style={{ flex: 1 }}><Btn label={`Paper ${r.option_type}`} onPress={() => tradeOption(r, r.option_type!, "paper")} /></View>
                          {(() => {
                            const sig = simpleSignal(res, moves?.[res.index_key!]);
                            return sig.liveOk && r.robust && r.option_type === sig.option
                              ? <View style={{ flex: 1 }}><Btn label={`Live ${r.option_type}`} kind="warn" onPress={() => tradeOption(r, r.option_type!, "live")} /></View>
                              : null;
                          })()}
                        </>
                      ) : <View style={{ flex: 2, justifyContent: "center" }}><Text style={[s.muted, { textAlign: "center" }]}>No CE/PE signal now</Text></View>}
                    </Row>
                  ) : (
                    <Row>
                      <View style={{ flex: 1 }}><Btn label="Paper trade" onPress={() => paperThis(r)} loading={acting === r.strategy + "pt"} /></View>
                    </Row>
                  )}
                </View>
              ))}
              <Hint>
                GOOD = made money on both older and unseen data (3+ trades). RISKY = signal, but not proven. WAIT = no signal now. Past results don't guarantee
                future returns — this is education, not investment advice.
              </Hint>
            </Card>
          )}
        </>
      )}

      <Card detail title={`Past analyses (${history.length})`} right={history.length ? (
        <Pressable onPress={clearAll} hitSlop={10}><Text style={{ color: C.red, fontWeight: "700" }}>Clear all</Text></Pressable>
      ) : null}>
        {history.length === 0 ? <Text style={s.muted}>None saved. Analyses are kept here (latest 50) while "Save to past analyses" is on.</Text> : history.map((it) => (
          <View key={it.id} style={[s.row, { paddingVertical: 9, borderBottomWidth: 1, borderBottomColor: C.border, gap: 8 }]}>
            <Pressable style={{ flex: 1 }} onPress={() => openPast(it.id)}>
              <View style={s.row}>
                <Text style={[s.text, { flex: 1, fontWeight: res?.id === it.id ? "800" : "600" }]} numberOfLines={1}>
                  {opening === it.id ? "Opening… " : ""}{it.title} · {ivLabel[it.interval] ?? it.interval}
                </Text>
                <Badge text={it.verdict} color={verdictColor(it.verdict)} />
              </View>
              <Text style={s.muted} numberOfLines={1}>
                {when(it.created)}{it.top ? ` · #1 ${it.top}: ${it.top_action}` : ""}
              </Text>
            </Pressable>
            <Pressable onPress={() => deletePast(it)} hitSlop={10}><Text style={{ color: C.dim, fontSize: 16 }}>✕</Text></Pressable>
          </View>
        ))}
      </Card>
    </Screen>
  );
}
