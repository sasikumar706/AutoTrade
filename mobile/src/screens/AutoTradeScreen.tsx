import { usePollOnFocus } from "../usePoll";
import ReportCard from "../ReportCard";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Pressable, RefreshControl, Text, View } from "react-native";
import { api, DEFAULT_RISK, inr, type AutoSnap, type RiskSettings, type StrategyInfo, type WhyItem } from "../api";
import { WhyList } from "../WhyList";
import { confirmClear as confirmClearRecords } from "../KiteLogout";
import WatchCard from "../WatchCard";
import { defaultSelection, OptionPicker, RiskForm, strikeMethodParam, type OptionSelection } from "../OptionControls";
import { INTRADAY, useStrategies } from "../StrategyForm";
import { Badge, Btn, C, Card, Chips, ErrorText, Field, IconBtn, Row, Screen, Section, Stat, Toggle, pnlColor, s, when, contractText, ActionBtn, TabBar, Hint } from "../ui";
import { PositionRow } from "./TradeTicket";

/** "fast 9 · slow 21", or for a combo: "SMA crossover (fast 9 · slow 21) + Supertrend (period 10 · multiplier 3)". */
export const paramText = (p: Record<string, any> | undefined) => {
  if (!p) return "";
  if (Array.isArray(p.members)) {
    return p.members.map((m: any) => `${m.strategy}${m.params && Object.keys(m.params).length ? ` (${Object.entries(m.params).map(([k, v]) => `${k} ${v}`).join(" · ")})` : ""}`).join(" + ");
  }
  return Object.entries(p).map(([k, v]) => `${k} ${v}`).join(" · ");
};

/** ON, or holding / entering a position — shown above the rest. */
export const isLive = (a: AutoSnap) =>
  a.enabled || (!!a.position && !["POSITION_CLOSED", "ORDER_REJECTED"].includes(a.position.state));

// Index to show: before the AI's first pick with "AI chooses", say so instead of a placeholder index
const ixLabel = (a: AutoSnap) => {
  const c: any = a.config;
  return c.autopilot && (c.autopilot_index ?? "ANY") === "ANY" && !a.ai_pick ? "AI picks index" : c.index;
};
const stratLabel = (a: AutoSnap) =>
  a.config.autopilot
    ? `${(a.config as any).entry_source === "indicators" ? "📊 Indicators" : "✦ Autopilot"}${a.ai_picking ? " (choosing…)" : a.ai_pick?.name ? `: ${a.ai_pick.name} · ${a.config.interval.replace("minute", "m")}` : ""}`
    : a.config.strategy_source === "ai"
      ? `✦ AI${a.ai_picking ? " (choosing…)" : a.ai_pick?.name ? `: ${a.ai_pick.name}` : ""}`
      : a.config.strategy;

export const autoStateColor = (st: string) =>
  st === "POSITION_OPEN" ? C.green : st === "SESSION_COMPLETED" || st === "IDLE" ? C.dim
    : st.includes("REJECT") || st.includes("FAIL") || st === "RECONCILIATION_REQUIRED" ? C.red : st === "ANALYZING" ? C.blue : C.amber;

// Trading-window presets (IST). Exits stay before Zerodha's ~15:20 intraday square-off.
const WINDOWS = {
  morning: { label: "Morning 9:30–10:30", analysis_start: "09:15", entry_start: "09:30", entry_end: "10:30", exit_time: "10:30" },
  noon: { label: "Till noon 9:30–12:00", analysis_start: "09:15", entry_start: "09:30", entry_end: "12:00", exit_time: "12:15" },
  afternoon: { label: "Afternoon 12:00–14:30", analysis_start: "11:45", entry_start: "12:00", entry_end: "14:30", exit_time: "15:00" },
  full: { label: "Full day 9:30–14:45", analysis_start: "09:15", entry_start: "09:30", entry_end: "14:45", exit_time: "15:10" },
} as const;
type WindowKey = keyof typeof WINDOWS | "custom";
const windowOf = (f: { analysis_start: string; entry_start: string; entry_end: string; exit_time: string }): WindowKey =>
  (Object.keys(WINDOWS) as (keyof typeof WINDOWS)[]).find((k) => {
    const w = WINDOWS[k];
    return w.analysis_start === f.analysis_start && w.entry_start === f.entry_start && w.entry_end === f.entry_end && w.exit_time === f.exit_time;
  }) ?? "custom";
const HM = /^([01]\d|2[0-3]):[0-5]\d$/;

type FormState = {
  autopilot: boolean; aIndex: "ANY" | "NIFTY" | "BANKNIFTY" | "SENSEX"; entrySource: "strategy" | "indicators";
  source: "ai" | "manual";
  mode: "paper" | "live"; sel: OptionSelection; strategy: string; params: Record<string, string>; interval: string;
  side: "BUY" | "SELL"; allowSell: boolean; product: "MIS" | "NRML"; risk: RiskSettings;
  analysis_start: string; entry_start: string; entry_end: string; exit_time: string;
  max_trades_per_day: string; max_daily_loss: string; max_capital: string; max_spread_pct: string;
  min_confidence: string; max_open_gap_pct: string; skip_expiry_day: boolean; entry_tolerance_pts: string; fit_lots: boolean; join_window: string; trend_override: "0" | "5" | "6"; consensus: "0" | "2" | "3" | "4"; anySignal: boolean;
  max_vix: string; min_adx: string; skip_lunch: boolean; time_stop: string; quickPts: string; quickBars: string; quickMode: "auto" | "points" | "off"; ivMode: "careful" | "skip" | "off"; costRatio: "0" | "2" | "3" | "4"; sameDir: "small" | "block" | "off"; oiWalls: boolean; stretchMode: "pullback" | "skip" | "off"; reentry2: boolean; repick: boolean; strict: boolean; luckLive: boolean;
  capital: string; riskPct: string; weekLoss: string; monthLoss: string; streak: string;
  autoMoney: boolean; paperCapital: string; maxLots: string;
  maxOpen: "1" | "2" | "3"; reentry: boolean; smartExit: boolean; scaleOut: boolean; liveRisk: boolean; cooldown: "0" | "2" | "3" | "6"; trendDay: "off" | "paper" | "always"; dayBuilder: "off" | "paper" | "always"; practice: boolean; resume: boolean; itm: boolean; expiryMode: "normal" | "careful" | "skip"; eventMode: "off" | "careful" | "skip";
};

const FILTER_DEFAULTS = { maxOpen: "2" as const, reentry: true, smartExit: true, scaleOut: true, liveRisk: true, cooldown: "2" as const, autoMoney: true, paperCapital: "300000", maxLots: "10", dayBuilder: "paper" as const, trendDay: "paper" as const, practice: true, resume: true, itm: true, expiryMode: "careful" as const, eventMode: "skip" as const, capital: "", riskPct: "1", weekLoss: "", monthLoss: "", streak: "3", max_vix: "30", min_adx: "15", skip_lunch: true, time_stop: "6", quickPts: "20", quickBars: "3", quickMode: "auto" as const, ivMode: "careful" as const, costRatio: "3" as const, sameDir: "small" as const, oiWalls: true, stretchMode: "pullback" as const, reentry2: true, repick: true, strict: true, luckLive: true };
const filterBody = (f: FormState) => ({
  max_vix: Number(f.max_vix) || 0, min_adx: Number(f.min_adx) || 0, skip_lunch: f.skip_lunch,
  time_stop_bars: Number(f.time_stop) || 0, quick_target_pts: f.quickMode === "off" ? 0 : Number(f.quickPts) || 20, quick_target_auto: f.quickMode === "auto", quick_target_after_bars: Math.max(1, Number(f.quickBars) || 3),
  iv_mode: f.ivMode, min_move_cost_ratio: Number(f.costRatio), same_direction_mode: f.sameDir, oi_walls: f.oiWalls, stretch_mode: f.stretchMode, trend_reentry: f.reentry2, midday_repick: f.repick ? "11:45" : "", strict_proof: f.strict,
  live_needs_luck_check: f.luckLive,
  max_open_positions: Number(f.maxOpen), reentry_after_exit: f.reentry, smart_exit: f.smartExit, scale_out: f.scaleOut, live_risk: f.liveRisk, cooldown_after_loss_bars: Number(f.cooldown), auto_money: f.autoMoney, paper_capital: Number(f.paperCapital) || 300000, max_auto_lots: Math.min(50, Math.max(1, Number(f.maxLots) || 10)), day_builder: f.dayBuilder, trend_day_entry: f.trendDay, paper_practice: f.practice, auto_resume_paper: f.resume, autopilot_itm: f.itm, expiry_day_mode: f.expiryMode, event_day_mode: f.eventMode,
  account_capital: Number(f.capital) || 0, risk_per_trade_pct: Number(f.riskPct) || 1,
  max_weekly_loss: Number(f.weekLoss) || 0, max_monthly_loss: Number(f.monthLoss) || 0, max_consecutive_losses: Number(f.streak) || 0,
});

const defaultForm = (): FormState => ({
  autopilot: true, aIndex: "ANY", entrySource: "strategy", source: "ai", mode: "paper", sel: { ...defaultSelection(true), expiry: "" }, strategy: "morning_momentum", params: {}, interval: "5minute",
  side: "BUY", allowSell: false, product: "MIS", risk: { ...DEFAULT_RISK },
  analysis_start: "09:15", entry_start: "09:30", entry_end: "10:30", exit_time: "10:30",
  max_trades_per_day: "10", max_daily_loss: "4000", max_capital: "50000", max_spread_pct: "3", min_confidence: "60",
  max_open_gap_pct: "2", skip_expiry_day: true, entry_tolerance_pts: "1", fit_lots: true, join_window: "15", trend_override: "0", consensus: "0", anySignal: false,
  ...FILTER_DEFAULTS,
});

const fromSnap = (a: AutoSnap): FormState => {
  const c = a.config;
  return {
    autopilot: !!c.autopilot, aIndex: ((c as any).autopilot_index ?? "ANY") as any, entrySource: ((c as any).entry_source ?? "strategy") as any, source: c.strategy_source === "ai" ? "ai" : "manual", mode: c.mode, sel: { index: c.index, expiry: c.expiry === "NEAREST" ? "" : c.expiry, optionType: c.option_rule,
      method: ["ATM", "ITM", "OTM"].includes(c.strike_method) ? c.strike_method : "STRIKE",
      strike: ["ATM", "ITM", "OTM"].includes(c.strike_method) ? null : Number(c.strike_method), offset: c.strike_offset, lots: c.lots },
    strategy: c.strategy === "combo" ? "morning_momentum" : c.strategy,
    params: c.strategy === "combo" ? {} : Object.fromEntries(Object.entries(c.params ?? {}).map(([k, v]) => [k, String(v)])), interval: c.interval,
    side: c.side, allowSell: c.allow_option_selling, product: c.product, risk: { ...DEFAULT_RISK, ...c.risk },
    analysis_start: c.analysis_start, entry_start: c.entry_start, entry_end: c.entry_end, exit_time: c.exit_time,
    max_trades_per_day: String(c.max_trades_per_day), max_daily_loss: String(c.max_daily_loss), max_capital: String(c.max_capital),
    max_spread_pct: String(c.max_spread_pct), min_confidence: String(c.min_confidence), max_open_gap_pct: String(c.max_open_gap_pct),
    skip_expiry_day: c.skip_expiry_day, entry_tolerance_pts: String(c.entry_tolerance_pts ?? 1), fit_lots: c.fit_lots_to_limits ?? true, join_window: String(c.join_window_minutes ?? 15), trend_override: String(c.trend_override_min ?? 0) as any, consensus: String(c.consensus_min ?? 0) as any, anySignal: c.any_strategy_entry ?? false,
    maxOpen: String(c.max_open_positions ?? 2) as any, reentry: c.reentry_after_exit ?? true, smartExit: c.smart_exit ?? true, scaleOut: (c as any).scale_out ?? true, liveRisk: (c as any).live_risk ?? true,
    cooldown: String(c.cooldown_after_loss_bars ?? 2) as any, autoMoney: c.auto_money ?? true, paperCapital: String(c.paper_capital ?? 300000), maxLots: String((c as any).max_auto_lots ?? 10), dayBuilder: c.day_builder ?? "paper", trendDay: c.trend_day_entry ?? "paper", practice: c.paper_practice ?? true, resume: c.auto_resume_paper ?? true, itm: c.autopilot_itm ?? true, expiryMode: c.expiry_day_mode ?? "careful", eventMode: c.event_day_mode ?? "skip",
    capital: c.account_capital ? String(c.account_capital) : "", riskPct: String(c.risk_per_trade_pct ?? 1),
    weekLoss: c.max_weekly_loss ? String(c.max_weekly_loss) : "", monthLoss: c.max_monthly_loss ? String(c.max_monthly_loss) : "",
    streak: String(c.max_consecutive_losses ?? 3),
    max_vix: String(c.max_vix ?? 30), min_adx: String(c.min_adx ?? 15), skip_lunch: c.skip_lunch ?? true,
    time_stop: String(c.time_stop_bars ?? 6), quickPts: String((c as any).quick_target_pts || 20), quickBars: String((c as any).quick_target_after_bars ?? 3),
    quickMode: (c as any).quick_target_pts === 0 ? "off" : ((c as any).quick_target_auto ?? true) ? "auto" : "points",
    ivMode: (c as any).iv_mode ?? "careful", costRatio: String((c as any).min_move_cost_ratio ?? 3) as any,
    sameDir: (c as any).same_direction_mode ?? "small", oiWalls: (c as any).oi_walls ?? true, stretchMode: (c as any).stretch_mode ?? "pullback", reentry2: (c as any).trend_reentry ?? true, repick: (c.midday_repick ?? "11:45") !== "", strict: c.strict_proof ?? true,
    luckLive: c.live_needs_luck_check ?? true,
  };
};

const toBody = (f: FormState) => f.autopilot ? {
  // Autopilot: the AI fills index, candle size, strategy, CE/PE, strike, stop/target and lots each morning
  autopilot: true, autopilot_index: f.aIndex, entry_source: f.entrySource, strategy_source: "ai", mode: f.mode, index: f.aIndex === "ANY" ? "NIFTY" : f.aIndex, strategy: "morning_momentum", params: {}, interval: "5minute",
  side: "BUY", option_rule: "AUTO", expiry: "NEAREST", skip_expiry_day: true, strike_method: "ATM", strike_offset: 1,
  lots: Math.max(1, Number(f.sel.lots) || 1), product: "MIS",
  analysis_start: "09:15", entry_start: "09:30", entry_end: "14:45", exit_time: "15:10",
  max_trades_per_day: Number(f.max_trades_per_day), max_daily_loss: Number(f.max_daily_loss), max_capital: Number(f.max_capital),
  max_spread_pct: 3, min_confidence: 60, max_open_gap_pct: Number(f.max_open_gap_pct) || 0, allow_option_selling: false,
  entry_tolerance_pts: Number(f.entry_tolerance_pts) || 0, fit_lots_to_limits: true, join_window_minutes: Number(f.join_window) || 15, trend_override_min: Number(f.trend_override), consensus_min: Number(f.consensus), any_strategy_entry: f.anySignal, ...filterBody(f),
} : ({
  autopilot: false, strategy_source: f.source, mode: f.mode, index: f.sel.index, strategy: f.strategy,
  params: Object.fromEntries(Object.entries(f.params).map(([k, v]) => [k, Number(v)])), interval: f.interval,
  side: f.side, option_rule: f.sel.optionType, expiry: f.sel.expiry || "NEAREST", skip_expiry_day: f.skip_expiry_day,
  strike_method: strikeMethodParam(f.sel), strike_offset: f.sel.offset, lots: f.sel.lots, product: f.product, risk: f.risk,
  analysis_start: f.analysis_start, entry_start: f.entry_start, entry_end: f.entry_end, exit_time: f.exit_time,
  max_trades_per_day: Number(f.max_trades_per_day), max_daily_loss: Number(f.max_daily_loss), max_capital: Number(f.max_capital),
  max_spread_pct: Number(f.max_spread_pct), min_confidence: Number(f.min_confidence), max_open_gap_pct: Number(f.max_open_gap_pct),
  allow_option_selling: f.allowSell, entry_tolerance_pts: Number(f.entry_tolerance_pts), fit_lots_to_limits: f.fit_lots, join_window_minutes: Number(f.join_window), trend_override_min: Number(f.trend_override),
  consensus_min: Number(f.consensus), any_strategy_entry: f.anySignal, ...filterBody(f),
});

/** Market filters, time-stop and midday re-pick — shared by both forms. */
function SafetyCard({ f, set }: { f: FormState; set: (p: Partial<FormState>) => void }) {
  const cap = Number(f.capital) || 0;
  const onOff = (v: string) => (v === "paper" ? "paper" : v === "always" ? "paper + live" : "off");
  return (<>
    {!(f.autopilot && f.autoMoney) && (
      <Section title="💰 Money safety" summary={`${cap ? `₹${cap.toLocaleString("en-IN")} · ${f.riskPct}% per trade` : "capital not set"} · week ${f.weekLoss || "off"} · month ${f.monthLoss || "off"} · stop after ${f.streak} losses`}>
        <Row>
          <Field label="Your trading capital ₹" numeric value={f.capital} onChangeText={(t) => set({ capital: t })} hint="blank = not used" />
          <Field label="Max risk per trade %" numeric value={f.riskPct} onChangeText={(t) => set({ riskPct: t })} hint="pros use 0.5–1%" />
        </Row>
        <Row>
          <Field label="Max loss per week ₹" numeric value={f.weekLoss} onChangeText={(t) => set({ weekLoss: t })} hint="blank = off" />
          <Field label="Max loss per month ₹" numeric value={f.monthLoss} onChangeText={(t) => set({ monthLoss: t })} hint="blank = off" />
        </Row>
        <Field label="Stop for the day after N losses in a row" numeric value={f.streak} onChangeText={(t) => set({ streak: t })} hint="0 = off" />
      </Section>
    )}

    <Section title="🛡 Safety filters"
      summary={`VIX ≤ ${f.max_vix || "off"} · ADX ≥ ${f.min_adx || "off"}${f.autopilot ? ` · gap ≤ ${f.max_open_gap_pct || "off"}%` : ""} · lunch ${f.skip_lunch ? "skipped" : "traded"} · expiry ${f.expiryMode} · events ${f.eventMode} · time-stop ${f.time_stop || "off"}`}>
      <Row>
        <Field label="Max India VIX" numeric value={f.max_vix} onChangeText={(t) => set({ max_vix: t })} hint="no new trades above · 0 = off" />
        <Field label="Min trend strength (ADX)" numeric value={f.min_adx} onChangeText={(t) => set({ min_adx: t })} hint="skip choppy market · 0 = off" />
      </Row>
      {f.autopilot && (
        <Field label="Skip the day if the opening gap is above %" numeric value={f.max_open_gap_pct}
          onChangeText={(t) => set({ max_open_gap_pct: t })} hint="e.g. 2 or 2.5 · 0 = never skip" />
      )}
      <Toggle label="Skip lunch hour (12:30–13:15)" value={f.skip_lunch} onChange={(skip_lunch) => set({ skip_lunch })} />
      <Text style={s.label}>Index expiry day (NIFTY Tue · SENSEX Thu)</Text>
      <Chips options={["careful", "skip", "normal"] as const} value={f.expiryMode} onChange={(expiryMode) => set({ expiryMode })}
        labels={{ careful: "Careful (1 lot, till 13:30)", skip: "Skip", normal: "Normal" }} />
      <Text style={s.label}>RBI / US Fed / Budget days</Text>
      <Chips options={["skip", "careful", "off"] as const} value={f.eventMode} onChange={(eventMode) => set({ eventMode })}
        labels={{ skip: "Skip", careful: "Careful (1 lot)", off: "Normal" }} />
      <Field label="Time-stop after N candles" numeric value={f.time_stop} onChangeText={(t) => set({ time_stop: t })}
        hint="exit a trade that isn't 0.25R in profit by then · 0 = off" />
      <Hint>Filters only block NEW entries; an open trade is always managed to its stop, target or exit.</Hint>
    </Section>

    <Section title="⚙️ Trades & entries"
      summary={`${f.max_trades_per_day} trades/day · ${f.maxOpen === "1" ? "1 at a time" : `up to ${f.maxOpen} open`} · AI exits ${f.smartExit ? "on" : "off"} · quick target ${f.quickMode === "off" ? "off" : f.quickMode === "auto" ? "auto" : `+${f.quickPts} pts`} · re-entry ${f.reentry ? "on" : "off"} · wait ${f.cooldown === "0" ? "none" : `${f.cooldown} candles`} after a loss${f.autopilot ? ` · ${f.itm ? "ITM" : "ATM"} strike` : ""}`}>
      <Field label="Max trades per day" numeric value={f.max_trades_per_day} onChangeText={(t) => set({ max_trades_per_day: t })}
        hint="1–20 · the daily loss stop still applies" />
      <Text style={s.label}>Trades open at the same time</Text>
      <Chips options={["1", "2", "3"] as const} value={f.maxOpen} onChange={(maxOpen) => set({ maxOpen })}
        labels={{ "1": "1 at a time", "2": "Up to 2", "3": "Up to 3" }} />
      <Toggle label="🤖 AI manages open trades (exit when the market turns, lock profit, let strong moves run)" value={f.smartExit}
        onChange={(smartExit) => set({ smartExit })} />
      {f.smartExit && <Toggle label="⏱ Re-check open trades every ~10 s (give-back, sharp moves, stalling at target)" value={f.liveRisk}
        onChange={(liveRisk) => set({ liveRisk })} />}
      {f.smartExit && <Toggle label="✂️ 2+ lots: book half at +1R, rest rides with the stop at entry" value={f.scaleOut}
        onChange={(scaleOut) => set({ scaleOut })} />}
      <Text style={s.label}>🎯 Quick target for slow trades</Text>
      <Chips options={["auto", "points", "off"] as const} value={f.quickMode} onChange={(quickMode) => set({ quickMode })}
        labels={{ auto: "Auto (sized to the trade)", points: "Fixed points", off: "Off" }} />
      {f.quickMode !== "off" && (
        <View style={{ flexDirection: "row", gap: 8 }}>
          {f.quickMode === "points" && <View style={{ flex: 1 }}><Field label="Points (₹ on option)" numeric value={f.quickPts} onChangeText={(t) => set({ quickPts: t })} /></View>}
          <View style={{ flex: 1 }}><Field label="After N candles" numeric value={f.quickBars} onChangeText={(t) => set({ quickBars: t })} /></View>
        </View>
      )}
      <Hint>{f.quickMode === "off" ? "Trades wait for the full target, stop or exit time." :
        `If the full target isn't hit within ${f.quickBars || 3} candles, the target comes down to entry + ${f.quickMode === "auto" ? "a size that fits this trade (the smaller of 0.8× its stop and 15% of the premium, never below 2.5× costs)" : `${f.quickPts || 0} pts`}. Strong moves the AI is riding are left alone.`}</Hint>
      <Toggle label="After a trade closes, the same setup may enter again" value={f.reentry} onChange={(reentry) => set({ reentry })} />
      <Text style={s.label}>After a losing trade, wait before the next entry</Text>
      <Chips options={["2", "3", "6", "0"] as const} value={f.cooldown} onChange={(cooldown) => set({ cooldown })}
        labels={{ "2": "2 candles", "3": "3 candles", "6": "6 candles", "0": "Don't wait" }} />
      {f.autopilot && <Toggle label="Buy 1 strike in-the-money (ITM) instead of ATM" value={f.itm} onChange={(itm) => set({ itm })} />}
      <Hint>The day's loss limit counts every open trade's stop-loss, so trades together never risk more than the day allows.</Hint>
    </Section>

    <Section title="🎯 Option-buyer checks"
      summary={`stretched: ${f.stretchMode} · IV ${f.ivMode} · target ≥ ${f.costRatio === "0" ? "any" : `${f.costRatio}× costs`} · OI walls ${f.oiWalls ? "on" : "off"} · same-way 2nd trade ${f.sameDir === "small" ? "1 lot" : f.sameDir}`}>
      <Text style={s.label}>Move already stretched (far from VWAP, RSI extreme) — don't chase</Text>
      <Chips options={["pullback", "skip", "off"] as const} value={f.stretchMode} onChange={(stretchMode) => set({ stretchMode })}
        labels={{ pullback: "Wait for pullback, then enter", skip: "Skip", off: "Off" }} />
      <Toggle label="↩️ Missed a move? Join it on the next pullback to the 20-EMA while the trend holds (up to 2 tries)" value={f.reentry2}
        onChange={(reentry2) => set({ reentry2 })} />
      <Text style={s.label}>Option price (IV) check — don't overpay for premium</Text>
      <Chips options={["careful", "skip", "off"] as const} value={f.ivMode} onChange={(ivMode) => set({ ivMode })}
        labels={{ careful: "1 lot when high, skip extreme", skip: "Skip when high", off: "Off" }} />
      <Text style={s.label}>Target must be at least … × the trade's costs</Text>
      <Chips options={["3", "4", "2", "0"] as const} value={f.costRatio} onChange={(costRatio) => set({ costRatio })}
        labels={{ "3": "3× (recommended)", "4": "4×", "2": "2×", "0": "Off" }} />
      <Toggle label="Set the target just before a big open-interest wall (skip when it leaves no room)" value={f.oiWalls}
        onChange={(oiWalls) => set({ oiWalls })} />
      <Text style={s.label}>Second trade betting the same way (two CEs or two PEs)</Text>
      <Chips options={["small", "block", "off"] as const} value={f.sameDir} onChange={(sameDir) => set({ sameDir })}
        labels={{ small: "1 lot only", block: "Not allowed", off: "Normal size" }} />
      <Hint>Pros check these before every option buy: expensive premium loses even when the direction is right, small targets get eaten by costs, and option sellers defend big OI strikes.</Hint>
    </Section>

    <Section title="🧠 Smart features"
      summary={`Day Builder ${onOff(f.dayBuilder)} · Trend Day ${onOff(f.trendDay)} · 11:45 re-pick ${f.repick ? "on" : "off"} · practice ${f.practice ? "on" : "off"}`}>
      <Text style={s.label}>🧠 Day Builder — builds a strategy for today at 10:15 / 11:45 / 13:15</Text>
      <Chips options={["paper", "always", "off"] as const} value={f.dayBuilder} onChange={(dayBuilder) => set({ dayBuilder })}
        labels={{ paper: "Paper only", always: "Paper + Live", off: "Off" }} />
      <Text style={s.label}>📈 Trend Day — buys the dip on strong one-way days</Text>
      <Chips options={["paper", "always", "off"] as const} value={f.trendDay} onChange={(trendDay) => set({ trendDay })}
        labels={{ paper: "Paper only", always: "Paper + Live", off: "Off" }} />
      <Toggle label="Re-test strategies at 11:45 if no trade is open" value={f.repick} onChange={(repick) => set({ repick })} />
      <Toggle label="Paper: practise with a weaker pick when nothing passes" value={f.practice} onChange={(practice) => set({ practice })} />
      <Toggle label="Paper: switch back on by itself after a server restart" value={f.resume} onChange={(resume) => set({ resume })} />
    </Section>

    <Section title="🔬 Proof rules (advanced)" summary={`strict proof ${f.strict ? "on" : "off"} · live needs luck check ${f.luckLive ? "on" : "off"}`}>
      <Toggle label="Strict proof (15+ unseen trades, profit factor 1.2+, survives double costs)" value={f.strict} onChange={(strict) => set({ strict })} />
      <Toggle label="LIVE trades only picks that pass the luck check" value={f.luckLive} onChange={(luckLive) => set({ luckLive })} />
      <Hint>
        The AI test charges Zerodha's real fees, slippage and time decay, and uses the same filters as live trading. The luck check
        compares the result with what the best of many no-edge tries would score by chance.
      </Hint>
    </Section>
  </>);
}

const LABEL_COLOR: Record<string, string> = { "TREND UP": C.green, "TREND DOWN": C.red, RANGE: C.blue, VOLATILE: C.amber, MIXED: C.dim };

/** Day Builder: today's market profile, similar past days and the strategy built for the rest of the day. */
function DayBuildCard({ b, building, mode, live, onBuild, busy }: {
  b?: Record<string, any> | null; building: boolean; mode: string; live: boolean; onBuild: () => void; busy: boolean;
}) {
  const today = new Date().toISOString().slice(0, 10);
  const cur = b && b.day === today ? b : null;
  const pf = cur?.profile;
  const best = cur?.best ?? (live ? null : cur?.practice);
  const off = mode === "off" || (mode === "paper" && live);
  return (
    <Card title="🧠 Day Builder — today's own strategy">
      {off && <Text style={s.muted}>Off for this strategy ({mode === "off" ? "turned off" : "paper only"}). Change it in Settings → Safety.</Text>}
      {building && <Text style={{ color: C.blue, fontWeight: "700" }}>Reading today's market and testing strategies on similar days…</Text>}
      {!cur && !building && <Text style={s.muted}>Builds automatically at 10:15, 11:45 and 13:15 (when no trade is open), or tap below.</Text>}
      {pf && (<>
        <Text style={{ color: LABEL_COLOR[pf.label] ?? C.text, fontSize: 18, fontWeight: "900" }}>Today: {pf.label} day</Text>
        <Text style={s.muted}>
          At {cur!.checkpoint}: {pf.move_pct > 0 ? "+" : ""}{pf.move_pct}% from yesterday · gap {pf.gap_pct > 0 ? "+" : ""}{pf.gap_pct}% ·
          {" "}{Math.round(pf.above_vwap * 100)}% of candles above VWAP · {pf.vwap_crosses} VWAP crossings · range {pf.range_pct}% · ADX {Math.round(pf.adx)}
        </Text>
        <Text style={s.muted}>
          Compared with {cur!.similar_days?.length ?? 0} most similar past days ({(cur!.similar_days ?? []).filter((d: any) => d.label === pf.label).length} of the
          same type) · {cur!.tested} strategy settings tested on what happened AFTER {cur!.checkpoint} on those days
        </Text>
      </>)}
      {best ? (
        <View style={{ padding: 10, borderRadius: 10, backgroundColor: (cur?.best ? C.green : C.amber) + "18", gap: 3 }}>
          <View style={s.row}>
            <Text style={[s.text, { fontWeight: "800", flex: 1 }]}>{best.name}</Text>
            <Badge text={cur?.best ? "BUILT · PASSED" : "PRACTICE · LUCK NOT RULED OUT"} color={cur?.best ? C.green : C.amber} />
          </View>
          <Text style={s.muted}>{paramText(best.params)}</Text>
          <Text style={s.text}>
            On similar days: {best.trades} trades · win {best.win_rate_pct}% · {inr(best.net)} per lot · older half {inr(best.older_half)} ·
            newer half {inr(best.newer_half)} · costs doubled {inr(best.double_cost_net)} · luck score {best.t_stat} (needs {cur?.luck_needed})
          </Text>
          <Text style={s.muted}>Auto Trade now also takes this strategy's own signals for the rest of today (shown as 🧠 in Stats).</Text>
        </View>
      ) : cur && !building ? <Text style={s.text}>{cur.why}</Text> : null}
      {!off && <Btn label={building ? "Building…" : "Build for today now"} kind="ghost" onPress={onBuild} loading={busy || building} disabled={building} />}
    </Card>
  );
}

/** Real option spreads measured from the quotes Auto Trade saved; used as the AI test's slippage. */
function SpreadCard() {
  const [m, setM] = useState<Awaited<ReturnType<typeof api.spreads>> | null>(null);
  useEffect(() => { api.spreads().then(setM).catch(() => {}); }, []);
  if (!m) return null;
  return (
    <Card title="📏 Real option spreads">
      {m.median_spread_pct === null ? (
        <Hint>Collecting… Auto Trade saves the option's bid/ask every minute while it runs ({m.days} day(s) so far).
          After 3 days the AI test uses YOUR measured spread instead of the standard 1% estimate.</Hint>
      ) : (<>
        <Row gap={8}>
          <Stat label="Typical spread" value={`${m.median_spread_pct}%`} />
          <Stat label="Worst 10%" value={`${m.p90_spread_pct}%`} />
          <Stat label="Slippage used" value={`${m.slippage_pct_used}% / side`} />
        </Row>
        <Text style={s.muted}>
          {m.samples} quotes over {m.days} day(s) · {m.slippage_source}
          {Object.keys(m.by_hour).length ? ` · by hour: ${Object.entries(m.by_hour).map(([h, v]) => `${h}h ${v}%`).join(", ")}` : ""}
        </Text>
      </>)}
    </Card>
  );
}

/** Replays past days through the real engine and compares with random entries. */
function ReplayCard({ id }: { id: string }) {
  const [r, setR] = useState<Record<string, any> | null>(null);
  const [job, setJob] = useState<string | null>(null);
  const [prog, setProg] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [days, setDays] = useState<"10" | "20" | "40">("20");
  useEffect(() => { api.replayLast(id).then((x) => setR(x && x.summary ? x : null)).catch(() => {}); }, [id]);
  useEffect(() => {
    if (!job) return;
    const t = setInterval(async () => {
      try {
        const j = await api.job(job);
        setProg(j.progress);
        if (j.status !== "running") {
          clearInterval(t); setJob(null);
          if (j.status === "done") setR(j.result); else setErr(j.error ?? j.status);
        }
      } catch (e: any) { setErr(e.message); }
    }, 2000);
    return () => clearInterval(t);
  }, [job]);
  const start = async () => {
    setErr(null); setProg("Starting…");
    try { setJob((await api.replayStart(id, Number(days))).id); } catch (e: any) { setErr(e.message); }
  };
  const v = r?.verdict;
  const vc = v?.level === "good" ? C.green : v?.level === "promising" ? C.blue : v?.level === "unknown" ? C.dim : C.red;
  return (
    <Card title="🔁 Honest test: replay past days">
      <Hint>
        Runs these exact settings — morning pick, entries, filters, stops, limits — over past days with only the data each day had,
        then compares with random entries using the same rules and costs. Takes a few minutes on the phone.
      </Hint>
      <Chips options={["10", "20", "40"] as const} value={days} onChange={setDays} labels={{ "10": "10 days", "20": "20 days", "40": "40 days" }} />
      <Btn label={job ? prog || "Running…" : "Run replay"} onPress={start} loading={!!job} disabled={!!job} />
      <ErrorText msg={err} />
      {r && (<>
        <Text style={{ color: vc, fontWeight: "800", fontSize: 15 }}>{v?.text}</Text>
        <Row gap={8}>
          <Stat label="Trades" value={String(r.summary.trades)} />
          <Stat label="Net after costs" value={inr(r.summary.net)} color={pnlColor(r.summary.net)} />
          <Stat label="Win rate" value={`${r.summary.win_rate_pct}%`} />
          <Stat label="Profit factor" value={r.summary.profit_factor?.toString() ?? "—"} />
          <Stat label="Worst drop" value={inr(r.summary.max_drawdown)} color={C.red} />
          {r.random && <Stat label="Beats random" value={`${r.random.beats_pct}% of runs`} color={r.random.beats_pct >= 90 ? C.green : C.amber} />}
        </Row>
        {r.random && (
          <Text style={s.muted}>
            Random entries ({r.random.runs} runs): average {inr(r.random.random_mean)}, usual range {inr(r.random.random_p10)} to {inr(r.random.random_p90)}.
          </Text>
        )}
        {r.picker && (
          <Text style={s.muted}>
            Morning pick: proven on {r.picker.days_with_proven_pick} of {r.picker.days} days · {r.picker.profitable_pick_days} profitable,
            {" "}{r.picker.losing_pick_days} losing · luck check passed {r.picker.luck_passed_days} day(s)
            {r.picker.strategies_used?.length ? ` · used: ${r.picker.strategies_used.join(", ")}` : ""}
          </Text>
        )}
        {Object.entries((r.summary.by_entry_type ?? {}) as Record<string, any>).map(([k, g]) => (
          <Text key={k} style={s.muted}>{TYPE_ICON[k] ?? "•"} {k}: {g.trades} trades, {inr(g.net)}</Text>
        ))}
        <Text style={s.muted}>{r.config.from} → {r.config.to} · {r.config.index} {String(r.config.interval).replace("minute", "-min")} · ran {String(r.ran_at ?? "").slice(0, 16).replace("T", " ")}</Text>
        {(r.limits as string[] | undefined)?.map((l, i) => <Text key={i} style={[s.muted, { fontSize: 11 }]}>ⓘ {l}</Text>)}
      </>)}
    </Card>
  );
}

/** Morning checklist: trading day, Kite login, contracts, margin, live prices. Runs by itself at 09:00. */
function ReadinessCard() {
  const [h, setH] = useState<Awaited<ReturnType<typeof api.autoHealth>> | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = useCallback(async () => {
    setBusy(true);
    try { setH(await api.autoHealth()); setErr(null); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }, []);
  useEffect(() => { run(); }, [run]);
  return (
    <Card title={h ? (h.ok ? "✅ Ready to trade" : "⚠️ Check before trading") : "Morning check"}
      right={<IconBtn icon="↻" label="" onPress={run} loading={busy} />}>
      {h?.checks.map((c) => (
        <Text key={c.name} style={{ color: c.ok ? C.text : C.red, fontSize: 14 }}>
          {c.ok ? "✓" : "✗"} <Text style={{ fontWeight: "700" }}>{c.name}</Text> — {c.detail}
        </Text>
      ))}
      <ErrorText msg={err} />
      <Hint>Runs by itself at 09:00 on trading days and alerts your phone if anything fails. Live prices are also watched all day.</Hint>
    </Card>
  );
}

export function AutoForm({ initial, onSaved, autoId }: { initial: FormState; onSaved: (a: AutoSnap) => void; autoId?: string }) {
  const { list } = useStrategies();
  const [f, setF] = useState<FormState>(initial);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (p: Partial<FormState>) => setF({ ...f, ...p });
  const strat: StrategyInfo | undefined = list.find((x) => x.key === f.strategy);
  useEffect(() => {
    if (strat && Object.keys(f.params).length === 0) set({ params: Object.fromEntries(strat.params.map((p) => [p.key, String(p.default)])) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strat?.key]);

  const win = windowOf(f);
  const [custom, setCustom] = useState(win === "custom");

  async function save() {
    setErr(null);
    const bad = (["analysis_start", "entry_start", "entry_end", "exit_time"] as const).find((k) => !HM.test(f[k]));
    if (bad) return setErr(`Times must be HH:MM (24-hour), e.g. 09:30 — check "${bad.replace("_", " ")}"`);
    setBusy(true);
    try {
      onSaved(autoId ? await api.updateAuto(autoId, toBody(f)) : await api.createAuto(toBody(f)));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const head = (
    <Card title="How should it trade?">
      <Chips options={["auto", "custom"] as const} value={f.autopilot ? "auto" : "custom"} onChange={(v) => set({ autopilot: v === "auto" })}
        labels={{ auto: "✦ AI Autopilot (recommended)", custom: "I'll set everything" }} />
      {f.autopilot && (
        <Hint>
          The AI tests every strategy each morning and trades only what made money on unseen data — index, strategy, CE/PE, strike,
          stop, target and lots are all decided for you. Options are only bought, never sold.
        </Hint>
      )}
      {f.autopilot && (<>
        <Text style={s.label}>Index</Text>
        <Chips options={["ANY", "NIFTY", "BANKNIFTY", "SENSEX"] as const} value={f.aIndex} onChange={(aIndex) => set({ aIndex })}
          labels={{ ANY: "✦ AI chooses", NIFTY: "NIFTY", BANKNIFTY: "BANKNIFTY", SENSEX: "SENSEX" }} />
        <Text style={s.muted}>{f.aIndex === "ANY" ? "AI tests NIFTY, BANKNIFTY and SENSEX each morning and trades the best one." : `Only ${f.aIndex} is tested and traded.`}</Text>
        <Text style={s.label}>Trade on</Text>
        <Chips options={["strategy", "indicators"] as const} value={f.entrySource} onChange={(entrySource) => set({ entrySource })}
          labels={{ strategy: "✦ Best strategy (AI picks)", indicators: "📊 Indicators only" }} />
        <Text style={s.muted}>{f.entrySource === "indicators"
          ? "Trades only when the 6 trend indicators agree (EMA20/50, RSI, VWAP, MACD, ADX, yesterday's pivot). The AI still tests it on past days with real costs each morning and tunes how many must agree — if it doesn't hold up, it waits instead of trading."
          : "The AI tests all 13 strategies (including Trend Score, the indicators-only one) each morning and trades the one that held up best."}</Text>
      </>)}
      <Text style={s.label}>Mode</Text>
      <Chips options={["paper", "live"] as const} value={f.mode} onChange={(mode) => set({ mode })}
        labels={{ paper: "📝 Paper (practice)", live: "💰 Live (real money)" }} />
      {f.mode === "live" && <Text style={{ color: C.amber }}>Live needs a static-IP server with real-money orders switched on. You'll confirm again when enabling.</Text>}
    </Card>
  );
  if (f.autopilot) {
    return (
      <>
        {head}
        <Card title="💰 Money">
          <Toggle label="Let the app decide money, lots and risk (recommended)" value={f.autoMoney} onChange={(autoMoney) => set({ autoMoney })} />
          {f.autoMoney && (<>
            <Field label="Paper capital ₹" numeric value={f.paperCapital} onChangeText={(t) => set({ paperCapital: t })}
              hint="pretend account size · LIVE uses your real Kite funds" />
            <Field label="Max lots per trade" numeric value={f.maxLots} onChangeText={(t) => set({ maxLots: t })}
              hint="the AI never goes above this; the risk budget usually allows fewer" />
            <Text style={s.muted}>
              → ₹{Math.round((Number(f.paperCapital) || 0) * 0.01).toLocaleString("en-IN")} risk per trade (1%, up to 1.5% for strong setups) ·
              day stop −₹{Math.round((Number(f.paperCapital) || 0) * 0.02).toLocaleString("en-IN")} · week −5% · month −10% · lots sized per trade from that risk, up to {f.maxLots || 10}.
              Risk is halved on nervous, expiry and event days and after 2 losses in a row.
            </Text>
          </>)}
        </Card>
        {!f.autoMoney && <Section title="💰 Your money limits" open
          summary={`max ₹${f.max_capital} premium · max loss ₹${f.max_daily_loss}/day · ${f.max_trades_per_day} trades/day · ${f.sel.lots} lots max`}>
          <Row>
            <Field label="Max capital per trade ₹" numeric value={f.max_capital} onChangeText={(t) => set({ max_capital: t })} hint="premium it may pay" />
            <Field label="Max loss per day ₹" numeric value={f.max_daily_loss} onChangeText={(t) => set({ max_daily_loss: t })} hint="stops for the day after this" />
          </Row>
          <Field label="Entry price tolerance ₹" numeric value={f.entry_tolerance_pts} onChangeText={(t) => set({ entry_tolerance_pts: t })}
            hint={`buys up to ₹${f.entry_tolerance_pts || 0} above the quote at signal time (LIMIT order) · 0 = market order`} />
          <Text style={s.label}>Also enter when other strategies signal</Text>
        <Toggle label="Any one strategy's fresh signal" value={f.anySignal} onChange={(anySignal) => set({ anySignal })} />
        <Chips options={["0", "2", "3", "4"] as const} value={f.consensus} onChange={(consensus) => set({ consensus })}
          labels={{ "0": "Agreement off", "2": "2+ agree", "3": "3+ agree", "4": "4+ agree" }} />
        <Hint>
          Besides the AI's chosen strategy, all 12 strategies are watched (each with its best settings from today's test). With "any one",
          a fresh buy/sell signal from any of them opens a trade that follows that strategy's own exit. With agreement, a trade opens when that
          many strategies point the same way. One position at a time; daily limits apply.
        </Hint>
        <Text style={s.label}>Strong-trend override</Text>
          <Chips options={["0", "5", "6"] as const} value={f.trend_override} onChange={(trend_override) => set({ trend_override })}
            labels={{ "0": "Off — strategy signals only", "5": "5 of 6 agree", "6": "6 of 6 agree" }} />
          <Hint>
            When the strategy has no signal but this many of the 6 indicator checks agree on a closed candle (and none disagree), Auto Trade
            enters in that direction anyway — once per direction per day. It exits when the trend fades, or at stop-loss/target/close time.
            This entry is not part of the AI's tested results.
          </Hint>
          <Row>
            <Field label="Max lots per trade" numeric value={String(f.sel.lots)} onChangeText={(t) => set({ sel: { ...f.sel, lots: Number(t) || 1 } })} />
          </Row>
          <Hint>
            Each trade risks at most half your daily loss limit (₹{Math.round(Number(f.max_daily_loss || 0) / 2).toLocaleString("en-IN")}), so at
            least two trades fit in a day. Trades 9:30–14:45, everything closed by 15:10.
          </Hint>
        </Section>}
        <SafetyCard f={f} set={set} />
        <ErrorText msg={err} />
        <Btn label={autoId ? "Save changes" : "Create Autopilot"} onPress={save} loading={busy} />
      </>
    );
  }

  return (
    <>
      {head}
      <OptionPicker value={f.sel} onChange={(sel) => set({ sel })} allowAuto allowNearest title="Instrument & contract rules" />
      <Card title="Strategy">
        <Chips options={["ai", "manual"] as const} value={f.source} onChange={(source) => set({ source })}
          labels={{ ai: "✦ AI picks the best (recommended)", manual: "I choose" }} />
        {f.source === "ai" ? (
          <Hint>
            Every morning the AI tests all strategies and settings on the index's recent candles, then trades only the best one
            that made money on data it had never seen. If none passes, Auto Trade waits that day. Each order shows why it was placed.
          </Hint>
        ) : (<>
        <View style={s.chips}>
          {list.filter((x) => x.intraday_only || ["supertrend", "sma_cross", "macd", "rsi_reversion", "bollinger"].includes(x.key)).map((st) => (
            <Pressable key={st.key} onPress={() => set({ strategy: st.key, params: Object.fromEntries(st.params.map((p) => [p.key, String(p.default)])) })}
              style={[s.chip, f.strategy === st.key && s.chipOn]}>
              <Text style={[s.chipText, f.strategy === st.key && { color: "#fff" }]}>{st.name}</Text>
            </Pressable>
          ))}
        </View>
        {strat && <Text style={s.muted}>{strat.description}</Text>}
        <Row>
          {(strat?.params ?? []).map((p) => (
            <Field key={p.key} label={p.label} numeric value={f.params[p.key] ?? ""} onChangeText={(t) => set({ params: { ...f.params, [p.key]: t } })} />
          ))}
        </Row>
        </>)}
        <Text style={s.label}>Candle interval</Text>
        <Chips options={INTRADAY.slice(0, 5)} value={f.interval as any} onChange={(interval) => set({ interval })}
          labels={{ minute: "1m", "3minute": "3m", "5minute": "5m", "10minute": "10m", "15minute": "15m" }} />
        <Text style={s.label}>Direction</Text>
        <Chips options={["BUY", "SELL"] as const} value={f.side} onChange={(side) => set({ side })}
          labels={{ BUY: "Buy options (long only)", SELL: "Sell options (short)" }} />
        {f.side === "SELL" && <Toggle label="Allow option selling" value={f.allowSell} onChange={(allowSell) => set({ allowSell })} />}
        <Chips options={["MIS", "NRML"] as const} value={f.product} onChange={(product) => set({ product })} />
        <Toggle label="On expiry day, trade the next expiry" value={f.skip_expiry_day} onChange={(skip_expiry_day) => set({ skip_expiry_day })} />
      </Card>
      <RiskForm value={f.risk} onChange={(risk) => set({ risk })} />
      <Card title="When to trade (IST)">
        <Chips options={[...Object.keys(WINDOWS), "custom"] as WindowKey[]} value={custom ? "custom" : win}
          onChange={(k) => {
            if (k === "custom") return setCustom(true);
            setCustom(false);
            const { label, ...times } = WINDOWS[k];
            set(times);
          }}
          labels={{ ...Object.fromEntries(Object.entries(WINDOWS).map(([k, w]) => [k, w.label])), custom: "Custom" }} />
        {custom ? (
          <>
            <Row>
              <Field label="Watch from" value={f.analysis_start} onChangeText={(t) => set({ analysis_start: t })} placeholder="09:15" />
              <Field label="New trades from" value={f.entry_start} onChangeText={(t) => set({ entry_start: t })} placeholder="09:30" />
            </Row>
            <Row>
              <Field label="New trades until" value={f.entry_end} onChangeText={(t) => set({ entry_end: t })} placeholder="14:45" />
              <Field label="Close everything at" value={f.exit_time} onChangeText={(t) => set({ exit_time: t })} placeholder="15:10" hint="15:15 latest" />
            </Row>
          </>
        ) : null}
        <Text style={s.muted}>
          Watches from {f.analysis_start} (no orders) · new trades {f.entry_start}–{f.entry_end} · every position closed by {f.exit_time}.
          {win === "full" ? " Full day means more trades — and more chances to lose; keep max trades/day and max loss/day low." : ""}
        </Text>
      </Card>
      <SafetyCard f={f} set={set} />
      <Card title="Limits">
        <Row>
          <Field label="Max loss/day ₹" numeric value={f.max_daily_loss} onChangeText={(t) => set({ max_daily_loss: t })} />
        </Row>
        <Row>
          <Field label="Max capital/trade ₹" numeric value={f.max_capital} onChangeText={(t) => set({ max_capital: t })} />
          <Field label="Max spread %" numeric value={f.max_spread_pct} onChangeText={(t) => set({ max_spread_pct: t })} />
        </Row>
        <Row>
          <Field label="Min confidence %" numeric value={f.min_confidence} onChangeText={(t) => set({ min_confidence: t })} />
          <Field label="Skip day if gap > %" numeric value={f.max_open_gap_pct} onChangeText={(t) => set({ max_open_gap_pct: t })} />
        </Row>
        <Field label="Entry price tolerance ₹" numeric value={f.entry_tolerance_pts} onChangeText={(t) => set({ entry_tolerance_pts: t })}
          hint="default ₹1 · 0 = market order" />
        <Hint>
          The option is bought with a LIMIT order up to ₹{f.entry_tolerance_pts || 0} above the quote at signal time (e.g. quote ₹74.15 →
          pays at most ₹{(74.15 + Number(f.entry_tolerance_pts || 0)).toFixed(2)}), so a small move in the premium doesn't stop the entry and a big jump
          doesn't overpay. Not filled in 90 s → cancelled.
        </Hint>
        <Text style={s.label}>Also enter when other strategies signal</Text>
        <Toggle label="Any one strategy's fresh signal" value={f.anySignal} onChange={(anySignal) => set({ anySignal })} />
        <Chips options={["0", "2", "3", "4"] as const} value={f.consensus} onChange={(consensus) => set({ consensus })}
          labels={{ "0": "Agreement off", "2": "2+ agree", "3": "3+ agree", "4": "4+ agree" }} />
        <Hint>
          Besides the AI's chosen strategy, all 12 strategies are watched (each with its best settings from today's test). With "any one",
          a fresh buy/sell signal from any of them opens a trade that follows that strategy's own exit. With agreement, a trade opens when that
          many strategies point the same way. One position at a time; daily limits apply.
        </Hint>
        <Text style={s.label}>Strong-trend override</Text>
        <Chips options={["0", "5", "6"] as const} value={f.trend_override} onChange={(trend_override) => set({ trend_override })}
          labels={{ "0": "Off — strategy signals only", "5": "5 of 6 agree", "6": "6 of 6 agree" }} />
        <Hint>
          When the strategy has no signal but this many of the 6 indicator checks agree on a closed candle (and none disagree), Auto Trade
          enters in that direction anyway — once per direction per day. It exits when the trend fades, or at stop-loss/target/close time.
          This entry is not part of the AI's tested results.
        </Hint>
        <Toggle label="Reduce lots automatically to fit these limits" value={f.fit_lots} onChange={(fit_lots) => set({ fit_lots })} />
        <Field label="Join a move that started up to (min) ago" numeric value={f.join_window} onChangeText={(t) => set({ join_window: t })}
          hint="0 = only fresh signals · default 15" />
        <Hint>
          If the strategy signalled a few minutes ago (e.g. while you were enabling, or a limit blocked it), Auto Trade joins it once — but not
          if {f.sel.index} has already moved more than 0.3% since the signal. Quantity is never increased to recover a loss.
        </Hint>
      </Card>
      <ErrorText msg={err} />
      <Btn label={autoId ? "Save changes" : "Create strategy"} onPress={save} loading={busy} />
    </>
  );
}

export default function AutoTradeScreen({ navigation }: any) {
  const [autos, setAutos] = useState<AutoSnap[]>([]);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const load = useCallback(async () => {
    try { setAutos(await api.autos()); setErr(null); } catch (e: any) { setErr(e.message); }
  }, []);
  usePollOnFocus(load, 4000);

  function kill() {
    Alert.alert("KILL SWITCH", "Disable every Auto Trade strategy and close every managed position now?", [
      { text: "Cancel", style: "cancel" },
      { text: "STOP EVERYTHING", style: "destructive", onPress: async () => {
        try { await api.kill(); load(); Alert.alert("Done", "Everything stopped. Verify in Kite."); }
        catch (e: any) { Alert.alert("Kill switch failed", e.message + "\nClose positions in Kite manually!"); }
      } },
    ]);
  }

  const active = autos.some((a) => a.enabled || (a.position && !["POSITION_CLOSED", "ORDER_REJECTED"].includes(a.position.state)));
  return (
    <Screen refresh={<RefreshControl refreshing={refreshing} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} tintColor={C.dim} />}>
      <ErrorText msg={err} />
      {autos.length > 0 && (
        <Card title="Your Auto Trade strategies">
          {[...autos].sort((x, y) => Number(isLive(y)) - Number(isLive(x))).map((a, i) => (
            <Pressable key={a.id} onPress={() => navigation.navigate("AutoDetail", { id: a.id })}
              style={{ paddingVertical: 10, borderTopWidth: i ? 1 : 0, borderTopColor: C.border, gap: 4 }}>
              <View style={s.row}>
                <Badge text={a.config.mode.toUpperCase()} color={a.config.mode === "live" ? C.red : C.blue} />
                <Text style={[s.text, { flex: 1, fontWeight: "800" }]} numberOfLines={1}>{ixLabel(a)} · {stratLabel(a)}</Text>
                <Badge text={a.enabled ? (a.paused ? "PAUSED" : "ON") : "OFF"} color={a.enabled ? (a.paused ? C.amber : C.green) : C.dim} />
              </View>
              <Text style={{ color: a.day_blocked ? C.red : C.dim, fontSize: 13 }} numberOfLines={1}>{a.day_blocked ?? a.message}</Text>
              <View style={s.row}>
                <Text style={[s.text, { flex: 1 }]}>
                  Today <Text style={{ color: pnlColor(a.realized_pnl + a.unrealized_pnl), fontWeight: "800" }}>{inr(a.realized_pnl + a.unrealized_pnl, 0)}</Text>
                  {"  ·  "}{a.trades_today}/{a.config.max_trades_per_day} trades
                </Text>
                <Text style={{ color: C.blue, fontWeight: "700" }}>Open ›</Text>
              </View>
            </Pressable>
          ))}
        </Card>
      )}
      {!creating ? (
        <View style={{ flexDirection: "row" }}>
          <IconBtn icon="＋" label={autos.length ? "Add another strategy" : "Create your first Auto Trade"} kind="primary" onPress={() => setCreating(true)} />
        </View>
      ) : (<>
        <AutoForm initial={defaultForm()} onSaved={(a) => { setCreating(false); load(); navigation.navigate("AutoDetail", { id: a.id }); }} />
        <Btn label="Cancel" kind="ghost" onPress={() => setCreating(false)} />
      </>)}
      {autos.length === 0 && !creating && (
        <Text style={s.muted}>A new strategy starts in PAPER mode and stays off until you tap ▶ Start.</Text>
      )}
      {autos.length > 0 && <ReadinessCard />}
      <Pressable onPress={() => navigation.navigate("Swing")}>
        <Card title="📈 Swing trend (holds for weeks)" right={<Text style={{ color: C.blue, fontWeight: "800" }}>Open ›</Text>}>
          <Hint>Buys a monthly in-the-money CE while the 10-day average is above the 50-day, sells when it turns down. A few trades a year — the one idea that held up on 15 years of real data.</Hint>
        </Card>
      </Pressable>
      {autos.length > 0 && <ReportCard source="auto" mode="all" />}
      <Btn label="📒 Zerodha account — all trades & charges" kind="ghost" onPress={() => navigation.navigate("Zerodha")} />
      {active && <Btn label="⛔ Emergency stop — stop all & sell all" kind="danger" onPress={kill} />}
      {autos.length > 0 && (
        <View style={{ flexDirection: "row" }}><IconBtn icon="🗑" label="Clear Auto Trade history" onPress={() => confirmClearRecords("Clear Auto Trade history?",
          "Removes closed Auto Trade positions (and their stats), every strategy's order history and logs. Strategies, settings and open positions are kept.",
          async () => {
            const r = await api.clearPositions(undefined, "auto");
            await Promise.all(autos.flatMap((a) => [api.autoControl(a.id, "clear_signals"), api.autoControl(a.id, "clear_log")]));
            return r.deleted;
          }, load)} /></View>
      )}
    </Screen>
  );
}

export function AutoDetailScreen({ route, navigation }: any) {
  const id: string = route.params.id;
  const [a, setA] = useState<AutoSnap | null>(null);
  const [stats, setStats] = useState<Record<string, any> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [preview, setPreview] = useState<Record<string, any> | null>(null);
  const [openSig, setOpenSig] = useState<number | null>(null);
  const [showMoney, setShowMoney] = useState(false);
  const [tab, setTab0] = useState<"now" | "ai" | "orders" | "stats" | "settings" | "log">("now");
  const setTab = (t: typeof tab) => {
    tabRef.current = t;
    setTab0(t);
    if (t === "stats") { statsAt.current = 0; api.analytics(`auto:${id}`).then(setStats).catch(() => {}); }
  };
  const tabRef = useRef("now");
  const statsAt = useRef(0);
  const load = useCallback(async () => {
    try {
      const x = await api.auto(id);
      setA(x);
      // stats are heavier: only while the Stats tab is open (and at most every 10 s)
      if (tabRef.current === "stats" && Date.now() - statsAt.current > 10_000) {
        statsAt.current = Date.now();
        setStats(await api.analytics(`auto:${id}`));
      }
      setErr(null);
    } catch (e: any) { setErr(e.message); }
  }, [id]);
  usePollOnFocus(load, 2500);

  async function control(action: string, confirm = "") {
    setBusy(action);
    try { setA(await api.autoControl(id, action, confirm)); } catch (e: any) { Alert.alert(action, e.message); } finally { setBusy(null); }
  }
  function enable() {
    if (!a) return;
    const c = a.config;
    const msg = `${c.index} · ${c.strategy_source === "ai" ? "AI picks the best proven strategy each morning" : c.strategy} · ${c.lots} lot(s) · ${c.side === "BUY" ? "buy options" : "SELL options"}\n` +
      `Entries ${c.entry_start}–${c.entry_end}, all exits by ${c.exit_time} IST\nMax ${c.max_trades_per_day} trades/day · max loss/day ₹${c.max_daily_loss} · max capital/trade ₹${c.max_capital}`;
    if (c.mode === "live") {
      Alert.alert("Enable LIVE Auto Trade?", msg + "\n\nREAL orders will be placed automatically.", [
        { text: "Cancel", style: "cancel" },
        { text: "Enable LIVE", style: "destructive", onPress: () => control("enable", "I UNDERSTAND REAL MONEY") },
      ]);
    } else {
      Alert.alert("Enable PAPER Auto Trade?", msg, [{ text: "Cancel", style: "cancel" }, { text: "Enable", onPress: () => control("enable") }]);
    }
  }
  function confirmClear(what: "clear_signals" | "clear_log") {
    Alert.alert(what === "clear_signals" ? "Clear order history?" : "Clear activity log?",
      what === "clear_signals" ? "Removes the list of past signals and their reasons. Positions, P&L and stats are kept."
        : "Removes all activity log lines. Nothing else changes.", [
        { text: "Cancel", style: "cancel" },
        { text: "Clear", style: "destructive", onPress: () => control(what) },
      ]);
  }
  function closePos() {
    const n = a?.open_positions?.length || 1;
    Alert.alert(n > 1 ? `Sell all ${n} open trades?` : "Sell this trade now?",
      `${n > 1 ? "Every open trade" : a?.position?.spec.tradingsymbol ?? "The open trade"} is closed at market now. Auto Trade stays on and can take its next signal.`, [
      { text: "Cancel", style: "cancel" }, { text: "Sell", style: "destructive", onPress: () => control("close_position") },
    ]);
  }
  async function remove() {
    try { await api.deleteAuto(id); navigation.goBack(); } catch (e: any) { Alert.alert("Delete", e.message); }
  }
  async function dryRun(direction: number) {
    try { setPreview(await api.autoPreview(id, direction)); } catch (e: any) { Alert.alert("Preview", e.message); }
  }

  if (!a) return <Screen><ErrorText msg={err} /><Text style={s.muted}>Loading…</Text></Screen>;
  const c = a.config;
  const today = new Date().toLocaleDateString("en-CA");
  const sigs = a.signals.filter((x: any) => String(x.signal_time ?? "").slice(0, 10) === today);  // a fresh page every day
  const sig = sigs.length ? sigs[sigs.length - 1] : null;
  const posActive = a.position && !["POSITION_CLOSED", "ORDER_REJECTED"].includes(a.position.state);
  return (
    <Screen>
      {(() => {
        const w = a.watch;
        const px = w?.price ?? a.spot;
        const ch = w?.change, chp = w?.change_pct;
        const pnl = (a.realized_pnl ?? 0) + (a.unrealized_pnl ?? 0);
        const st = a.state;
        const fresh = sig?.signal_time ? Date.now() - new Date(sig.signal_time).getTime() < 15 * 60_000 : false;
        const rejected = st.includes("REJECT") && !a.day_blocked && fresh;  // an old skip shouldn't sit there all day
        const tone = a.day_blocked ? C.red : rejected ? C.amber : autoStateColor(st);
        const stateText = a.day_blocked ?? (rejected ? `Last signal skipped — ${a.message}`
          : st.includes("REJECT") ? (w?.status ?? a.trend_note ?? "Watching for the next setup")
          : `${st.replace(/_/g, " ").toLowerCase().replace(/^./, (x) => x.toUpperCase())} — ${a.message}`);
        return (
          <View style={[s.card, { gap: 14 }]}>
            <View style={{ flexDirection: "row", alignItems: "flex-start", gap: 10 }}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.dim, fontSize: 12, fontWeight: "800", letterSpacing: 0.4 }}>{ixLabel(a).toUpperCase()}</Text>
                <Text style={{ color: C.ink, fontSize: 26, fontWeight: "900", fontVariant: ["tabular-nums"] }}>
                  {px ? px.toLocaleString("en-IN", { maximumFractionDigits: 1 }) : "—"}
                </Text>
                {ch !== undefined && ch !== null ? (
                  <Text style={{ color: pnlColor(ch), fontWeight: "800", fontSize: 13 }}>{ch > 0 ? "▲ +" : ch < 0 ? "▼ " : ""}{ch} ({chp}%) today</Text>
                ) : null}
              </View>
              <View style={{ alignItems: "flex-end", gap: 6 }}>
                <Badge text={a.enabled ? (a.paused ? "PAUSED" : "● RUNNING") : "OFF"} color={a.enabled ? (a.paused ? C.amber : C.green) : C.dim} />
                <Badge text={c.mode.toUpperCase()} color={c.mode === "live" ? C.red : C.blue} />
              </View>
            </View>
            <Text style={{ color: C.text, fontWeight: "700", fontSize: 14 }} numberOfLines={1}>{stratLabel(a)}</Text>
            <View style={{ flexDirection: "row", borderRadius: 14, backgroundColor: C.cardHi, borderWidth: 1, borderColor: "#EBEFF5" }}>
              {[
                { l: "Today P&L", v: inr(pnl, 0), col: pnlColor(pnl) },
                { l: "Trades", v: `${a.trades_today}/${c.max_trades_per_day}` },
                { l: "Can risk", v: inr(a.remaining_risk_budget, 0) },
              ].map((k, n) => (
                <View key={k.l} style={{ flex: 1, paddingVertical: 10, alignItems: "center", borderLeftWidth: n ? 1 : 0, borderLeftColor: C.border }}>
                  <Text style={s.statLabel}>{k.l}</Text>
                  <Text style={{ color: k.col ?? C.ink, fontWeight: "900", fontSize: 17, marginTop: 2, fontVariant: ["tabular-nums"] }} numberOfLines={1}>{k.v}</Text>
                </View>
              ))}
            </View>
            {a.unrealized_pnl ? <Text style={[s.muted, { marginTop: -6 }]}>Booked {inr(a.realized_pnl, 0)} · open {inr(a.unrealized_pnl, 0)}</Text> : null}
            <View style={{ flexDirection: "row", gap: 8, alignItems: "flex-start", padding: 10, borderRadius: 12, backgroundColor: tone + "12" }}>
              <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: tone, marginTop: 6 }} />
              <Text style={{ flex: 1, color: C.text, fontSize: 13, lineHeight: 19 }} numberOfLines={3}>{stateText}</Text>
            </View>
            {a.state === "SESSION_COMPLETED" && c.exit_time < "15:00" && (
              <Text style={{ color: C.amber }}>Trading window ended at {c.exit_time}. Change "When to trade" in Settings to trade till 14:45.</Text>
            )}
            {a.money ? (
              <Pressable onPress={() => setShowMoney(!showMoney)} style={{ flexDirection: "row", alignItems: "center" }}>
                <Text style={[s.muted, { flex: 1, fontSize: 13 }]} numberOfLines={1}>
                  💰 ₹{a.money.capital.toLocaleString("en-IN")} · risk ₹{a.money.risk_per_trade.toLocaleString("en-IN")}/trade · day stop −₹{a.money.day_stop.toLocaleString("en-IN")}
                  {a.money.why_lower?.length ? " · ⚠ lower risk today" : ""}
                </Text>
                <Text style={{ color: C.blue, fontWeight: "800", fontSize: 13 }}>{showMoney ? "Less" : "More"}</Text>
              </Pressable>
            ) : null}
        {a.money && showMoney && (
          <View style={{ padding: 10, borderRadius: 10, backgroundColor: C.blue + "10", gap: 2 }}>
            <Text style={s.muted}>
              Automatic money from your {a.money.source}: normal risk {a.money.risk_pct}% per trade (strong setups up to 1.5%) · week stop
              −₹{a.money.week_stop.toLocaleString("en-IN")} · month −₹{a.money.month_stop.toLocaleString("en-IN")} · premium ≤ ₹{a.money.max_premium.toLocaleString("en-IN")}
            </Text>
            {a.money.why_lower?.length ? <Text style={{ color: C.amber, fontSize: 13 }}>Lower risk today: {a.money.why_lower.join(", ")}</Text> : null}
            {a.sizing ? <Text style={s.muted}>Last trade sized at {a.sizing.risk_pct}% = ₹{a.sizing.risk_rupees.toLocaleString("en-IN")} ({a.sizing.setup.replace("_", " ")})</Text> : null}
            <Text style={s.muted}>
              New trades {c.entry_start}–{c.entry_end} · closed by {c.exit_time} · {c.strike_method} strike · {c.interval.replace("minute", "-min")} candles
              {a.last_tick ? ` · last price ${a.last_tick.slice(11, 19)}` : ""}
            </Text>
          </View>
        )}
            <View style={{ flexDirection: "row", gap: 8 }}>
              {!a.enabled
                ? <ActionBtn icon="▶" label="Start" kind={c.mode === "live" ? "warn" : "primary"} onPress={enable} loading={busy === "enable"} />
                : a.paused
                  ? <ActionBtn icon="▶" label="Resume" kind="primary" onPress={() => control("resume")} loading={busy === "resume"} />
                  : <ActionBtn icon="⏸" label="Pause" onPress={() => control("pause")} loading={busy === "pause"} />}
              {a.enabled && <ActionBtn icon="⏹" label="Stop" kind="danger" onPress={() =>
                Alert.alert("Stop Auto Trade?", "No new trades and any pending entry is cancelled. An open trade keeps its stop and target — use Sell all to close it now.",
                  [{ text: "Cancel", style: "cancel" }, { text: "Stop", style: "destructive", onPress: () => control("disable") }])}
                loading={busy === "disable"} />}
              {posActive && <ActionBtn icon="💰" label="Sell all" kind="danger" onPress={closePos} loading={busy === "close_position"} />}
            </View>
          </View>
        );
      })()}

      <TabBar value={tab} onChange={setTab} tabs={[
        { key: "now", label: "Now" }, { key: "ai", label: c.strategy_source === "ai" ? "AI pick" : "Strategy" },
        { key: "orders", label: `Orders · ${sigs.length}` }, { key: "stats", label: "Stats" },
        { key: "settings", label: "Settings" }, { key: "log", label: "Log" }]} />

{tab === "now" && (<>
      {(a.open_positions?.length ?? 0) > 1 ? (
        <Card title={`Open trades (${a.open_positions!.length})`}>
          {a.open_positions!.map((p) => (
            <PositionRow key={p.id} p={p} onPress={() => navigation.navigate("Position", { id: p.id })} />
          ))}
          <Text style={s.muted}>Tap a trade for its live price, stop and target. 💰 Sell all closes every open trade.</Text>
        </Card>
      ) : posActive && a.position ? (
        <Card title="Open trade">
          <PositionRow p={a.position} onPress={() => navigation.navigate("Position", { id: a.position!.id })} onClose={closePos} />
        </Card>
      ) : null}
      {a.enabled && a.trend_note ? <Card><Text style={{ color: C.text, fontWeight: "700" }}>📈 {a.trend_note}</Text></Card> : null}
      {a.enabled ? <WatchCard w={a.watch} index={c.index} autoMoney={!!a.money} hidePrice /> : (
        <Card><Text style={s.muted}>Tap ▶ Start to see the live price, which way the market leans and the next order it expects.</Text></Card>
      )}
      {!posActive && a.position && (
        <Card title="Last trade">
          <PositionRow p={a.position} onPress={() => navigation.navigate("Position", { id: a.position!.id })} />
        </Card>
      )}
      </>)}

{tab === "ai" && (<>
      {c.strategy_source !== "ai" && (
        <Card title="Your chosen strategy">
          <Text style={[s.text, { fontWeight: "800" }]}>{c.strategy}</Text>
          <Text style={s.muted}>{paramText(c.params) || "default settings"}</Text>
        </Card>
      )}
      {c.strategy_source === "ai" && (
        <Card title="Today's AI strategy" right={a.ai_picking ? <Badge text="TESTING…" color={C.blue} />
          : a.ai_pick?.strategy ? ((a.ai_pick as any).practice || a.ai_pick.unproven ? <Badge text="PRACTICE" color={C.amber} />
            : <Badge text="PROVEN" color={C.green} />) : a.ai_pick ? <Badge text="WAIT TODAY" color={C.amber} /> : null}>
          {a.ai_picking && <Text style={s.muted}>Testing every strategy on recent candles — a few minutes on the phone. Current trades keep running.</Text>}
          {!a.ai_picking && a.ai_pick?.strategy && (<>
            <Text style={[s.text, { fontWeight: "800", fontSize: 16 }]}>{a.ai_pick.name}</Text>
            {c.autopilot && (
              <Text style={s.text}>
                {c.index} · {c.interval.replace("minute", "-min")} candles · up → BUY CE, down → BUY PE · {c.strike_method === "ITM" ? "1 strike in-the-money" : `${c.strike_method} strike`}
                {c.risk?.sl_value ? ` · stop ${c.risk.sl_value} pts · target ${c.risk.target_value}× the stop` : ""}
              </Text>
            )}
            {a.ai_pick.test && (
              <Row gap={8}>
                <Stat label="On unseen data" value={a.ai_pick.test.net_pnl !== undefined ? inr(a.ai_pick.test.net_pnl) : `${a.ai_pick.test.total_return_pct}%`}
                  color={pnlColor(a.ai_pick.test.net_pnl ?? a.ai_pick.test.total_return_pct)} />
                <Stat label="Trades" value={String(a.ai_pick.test.trades)} />
                <Stat label="Win rate" value={`${a.ai_pick.test.win_rate_pct}%`} />
              </Row>
            )}
            {a.ai_pick.description ? <Text style={[s.muted, { lineHeight: 19 }]}>{a.ai_pick.description}</Text> : null}
            <Text style={s.muted}>Picked {a.ai_pick.picked_at.replace("T", " ").slice(0, 16)}{a.ai_pick.combinations_tested ? ` · ${a.ai_pick.combinations_tested} settings tried` : ""}</Text>
          </>)}
          {!a.ai_picking && a.ai_pick && !a.ai_pick.strategy && (<>
            <Text style={{ color: C.amber }}>No strategy made money on unseen data — the AI waits today. Day Builder and Trend Day can still trade.</Text>
            {c.mode === "paper" && (a.ai_pick.tested ?? []).some((t) => t.test.trades >= 3) && (
              <Btn label="Practise with the best one anyway (paper)" kind="ghost" onPress={() => control("use_best_unproven")} loading={busy === "use_best_unproven"} />
            )}
          </>)}
          {!a.ai_picking && !a.ai_pick && <Text style={s.muted}>The AI picks a strategy when you start Auto Trade, and every trading morning.</Text>}
          {!a.ai_picking && !posActive && a.enabled && (
            <View style={{ flexDirection: "row" }}><IconBtn icon="↻" label="Test again now" onPress={() => control("repick")} loading={busy === "repick"} /></View>
          )}
        </Card>
      )}
      <DayBuildCard b={a.day_build} building={!!a.day_building} mode={c.day_builder ?? "paper"} live={c.mode === "live"}
        onBuild={() => control("day_build")} busy={busy === "day_build"} />
      {!a.ai_picking && a.ai_pick?.strategy && (
        <Section title="🔬 How strong is the proof?" summary={a.ai_pick.luck ? `luck check ${a.ai_pick.luck.passed ? "passed" : "not passed"} · costs ₹${Math.round(a.ai_pick.costs?.round_trip ?? 0)}/trade` : "costs, double-cost test, luck check"}>
          <ProofBox p={a.ai_pick} live={c.mode === "live"} />
        </Section>
      )}
      {c.autopilot && a.ai_pick?.risk_tuning?.best && (
        <Section title="🎯 Stop & target tuning" summary={`stop ${a.ai_pick.risk_tuning.best.stop_mult}× volatility · target ${a.ai_pick.risk_tuning.best.target_rr}× · win ${a.ai_pick.risk_tuning.best.test_win}% on unseen data`}>
          <Text style={[s.muted, { fontWeight: "700" }]}>Stop × · Target × · Win % (unseen) · ₹ per lot</Text>
          {a.ai_pick.risk_tuning.table.map((r, i) => {
            const on = r.stop_mult === a.ai_pick!.risk_tuning!.best.stop_mult && r.target_rr === a.ai_pick!.risk_tuning!.best.target_rr;
            return (
              <Text key={i} style={[s.muted, on && { color: C.blue, fontWeight: "800" }]}>
                {on ? "▶ " : "   "}{r.stop_mult} · {r.target_rr} · {r.test_win}% · ₹{Math.round(r.test_pnl).toLocaleString("en-IN")} ({r.test_trades} trades)
              </Text>
            );
          })}
        </Section>
      )}
      {!a.ai_picking && (a.ai_pick?.tested?.length ?? 0) > 0 && (
        <Section title={`📋 All ${a.ai_pick!.tested!.length} strategies tested`}
          summary={`${a.ai_pick!.tested!.filter((t) => t.robust).length} passed · tap to see why each passed or failed`}>
          {a.ai_pick!.tested!.map((t, i) => (
            <View key={i} style={{ paddingVertical: 8, borderTopWidth: i ? 1 : 0, borderTopColor: C.border, gap: 3 }}>
              <View style={s.row}>
                <Text style={{ color: t.robust ? C.green : C.red, fontWeight: "800" }}>{t.robust ? "✓" : "✗"}</Text>
                <Text style={[s.text, { flex: 1, fontWeight: "700" }]} numberOfLines={1}>{t.name}</Text>
                <Text style={{ color: pnlColor(t.test.net_pnl), fontWeight: "700" }}>{inr(t.test.net_pnl)}</Text>
              </View>
              <Text style={s.muted}>{t.test.trades} trades · win {t.test.win_rate_pct}% · {t.why}</Text>
            </View>
          ))}
        </Section>
      )}
      </>)}

{tab === "orders" && (<>
      {sigs.length === 0 ? <Card><Text style={s.muted}>No signals yet today. Each signal shows here with its reasons, placed or skipped.</Text></Card> : (
        <View style={{ flexDirection: "row" }}><IconBtn icon="🗑" label="Clear order history" onPress={() => confirmClear("clear_signals")} loading={busy === "clear_signals"} /></View>
      )}
      {sig && (
        <Card title={sig.risk_check === "PASS" ? "Why this order was placed" : "Why the last signal was skipped"}
          right={<Badge text={sig.risk_check === "PASS" ? "ORDER PLACED" : "SKIPPED"} color={sig.risk_check === "PASS" ? C.green : C.amber} />}>
          <Text style={[s.text, { fontWeight: "800", fontSize: 17 }]}>{String(sig.action).replace("_", " ")} · {contractText(sig)}</Text>
          <Text style={s.muted}>{sig.tradingsymbol ?? ""} · market {sig.market_direction}</Text>
          <Row gap={8}>
            <Stat label="Entry" value={sig.entry_price?.toFixed?.(2) ?? "—"} />
            <Stat label="Stop" value={sig.stop_loss?.toFixed?.(2) ?? "—"} color={C.red} />
            <Stat label="Target" value={sig.target_price?.toFixed?.(2) ?? "—"} color={C.green} />
            <Stat label="Qty" value={`${sig.quantity ?? "—"} (${sig.lots} lot)`} />
            <Stat label="Max loss" value={inr(sig.est_max_loss)} color={C.red} />
            <Stat label="Confidence" value={`${sig.confidence}%`} />
          </Row>
          <WhyList why={sig.why as WhyItem[] | undefined} />
          {!sig.why && sig.reason ? <Text style={s.muted}>Why: {sig.reason}</Text> : null}
          <Text style={s.muted}>Signal {when(String(sig.signal_time))} IST</Text>
        </Card>
      )}

      {sigs.length > 1 && (
        <Card title="Earlier today">
          {[...sigs].reverse().slice(1, 10).map((x, i) => (
            <Pressable key={i} onPress={() => setOpenSig(openSig === i ? null : i)}
              style={{ paddingVertical: 8, borderTopWidth: i ? 1 : 0, borderTopColor: C.border, gap: 6 }}>
              <View style={s.row}>
                <View style={{ flex: 1 }}>
                  <Text style={[s.text, { fontWeight: "800" }]}>{String(x.action).replace("_", " ")} · {contractText(x)}</Text>
                  <Text style={s.muted}>
                    {when(String(x.signal_time))}{x.lots ? ` · ${x.lots} lot` : ""}{x.entry_price ? ` · ₹${Number(x.entry_price).toFixed(2)}` : ""}
                    {x.stop_loss ? ` · SL ${Number(x.stop_loss).toFixed(1)}` : ""}{x.target_price ? ` · T ${Number(x.target_price).toFixed(1)}` : ""}
                  </Text>
                </View>
                <Badge text={x.risk_check === "PASS" ? "PLACED" : "SKIPPED"} color={x.risk_check === "PASS" ? C.green : C.amber} />
              </View>
              {openSig === i ? <WhyList why={x.why as WhyItem[] | undefined} /> : <Text style={s.muted}>Tap for reasons</Text>}
            </Pressable>
          ))}
        </Card>
      )}
      </>)}

{tab === "stats" && (<>
      {!stats && <Card><Text style={s.muted}>Loading results…</Text></Card>}
      {stats && (
        <Card title="Results">
          <Row gap={8}>
            <Stat label="Net after costs" value={inr(stats.net_after_costs)} color={pnlColor(stats.net_after_costs)} />
            <Stat label="Trades" value={String(stats.closed_trades)} />
            <Stat label="Win rate" value={`${stats.win_rate_pct}%`} />
            <Stat label="Profit factor" value={stats.profit_factor?.toString() ?? "—"} />
            <Stat label="Avg win ÷ avg loss" value={stats.avg_win_loss_ratio?.toString() ?? "—"} />
            <Stat label="Worst drop" value={inr(stats.max_drawdown)} color={C.red} />
          </Row>
          <Hint>Profit factor = ₹ won ÷ ₹ lost (above 1 = making money). Paper results use simulated fills on live prices.</Hint>
        </Card>
      )}
      {stats?.scorecard && <ScorecardCard sc={stats.scorecard} versions={stats.versions} />}
      {stats && (stats.costs_share_pct !== undefined) && (
        <Section title="💸 What costs take" summary={`charges ${inr(stats.total_charges)} · ${stats.costs_share_pct ?? "—"}% of gross profit · ${inr(stats.avg_cost_per_trade ?? 0)}/trade`}>
          <Row gap={8}>
            <Stat label="Gross before costs" value={inr(stats.gross_before_costs)} color={pnlColor(stats.gross_before_costs)} />
            <Stat label="Charges" value={inr(stats.total_charges)} color={C.red} />
            <Stat label="Share of gross" value={stats.costs_share_pct !== null ? `${stats.costs_share_pct}%` : "—"} />
            <Stat label="Per trade" value={inr(stats.avg_cost_per_trade ?? 0)} />
          </Row>
          {(stats.daily ?? []).slice(0, 7).map((d: any) => (
            <View key={d.day} style={{ flexDirection: "row", paddingVertical: 2 }}>
              <Text style={[s.muted, { flex: 1 }]}>{when(d.day + "T00:00").slice(0, -6)} · {d.trades} trades</Text>
              <Text style={{ color: pnlColor(d.net), fontWeight: "700" }}>{inr(d.net)}</Text>
              <Text style={[s.muted, { width: 90, textAlign: "right" }]}>costs {inr(d.charges ?? 0)}</Text>
            </View>
          ))}
          <Hint>Charges only (brokerage, STT, exchange, GST, stamp). Slippage is already inside the fill prices.</Hint>
        </Section>
      )}
      {stats?.tuning && <TuningCard t={stats.tuning} />}
      {stats && <Breakdown stats={stats} />}
      <ReplayCard id={a.id} />
      <ReportCard source="auto" mode={c.mode === "live" ? "live" : "paper"} />
      {stats && Object.keys(stats.skipped ?? {}).length > 0 && (
        <Section title="Why signals were skipped"
          summary={Object.entries(stats.skipped as Record<string, number>).slice(0, 3).map(([k, n]) => `${k} ${n}`).join(" · ")}>
          {Object.entries(stats.skipped as Record<string, number>).map(([k, n]) => (
            <View key={k} style={{ flexDirection: "row", paddingVertical: 3 }}>
              <Text style={[s.text, { flex: 1 }]}>{k}</Text>
              <Text style={{ color: C.text, fontWeight: "800" }}>{n}</Text>
            </View>
          ))}
          <Hint>Skipping is the filters doing their job. If one reason blocks almost everything, check that setting.</Hint>
        </Section>
      )}
      {stats && (
        <Section title="Order execution quality"
          summary={`fills ${stats.fill_rate_pct ?? "—"}% · slippage ${stats.avg_entry_slippage_pct ?? "—"}% · rejected ${stats.orders_rejected}`}>
          <Row gap={8}>
            <Stat label="Signal→order" value={stats.signal_to_order_pct !== null ? `${stats.signal_to_order_pct}%` : "—"} />
            <Stat label="Order accepted" value={stats.order_acceptance_pct !== null ? `${stats.order_acceptance_pct}%` : "—"} />
            <Stat label="Filled" value={stats.fill_rate_pct !== null ? `${stats.fill_rate_pct}%` : "—"} />
            <Stat label="Avg slippage" value={stats.avg_entry_slippage_pct !== null ? `${stats.avg_entry_slippage_pct}%` : "—"} />
            <Stat label="Rejected" value={String(stats.orders_rejected)} />
            <Stat label="Target / stop exits" value={`${stats.target_exits} / ${stats.stop_exits}`} />
          </Row>
        </Section>
      )}
      <SpreadCard />
      {a.levels?.prev_high !== undefined && (
        <Section title="Today's key levels" summary={`pivot ${a.levels.P} · prev close ${a.levels.prev_close} · gap ${a.levels.gap_pct ?? "—"}%`}>
          <Row gap={8}>
            <Stat label="Prev high" value={String(a.levels.prev_high)} />
            <Stat label="Prev low" value={String(a.levels.prev_low)} />
            <Stat label="Prev close" value={String(a.levels.prev_close)} />
            <Stat label="Pivot" value={String(a.levels.P)} />
            <Stat label="R1 / R2" value={`${a.levels.R1} / ${a.levels.R2}`} />
            <Stat label="S1 / S2" value={`${a.levels.S1} / ${a.levels.S2}`} />
          </Row>
        </Section>
      )}
      <Section title="Test an order (nothing is placed)" summary="See what would be ordered right now for an up or down signal">
        <Row>
          <View style={{ flex: 1 }}><Btn label="If up (CE)" kind="ghost" onPress={() => dryRun(1)} /></View>
          <View style={{ flex: 1 }}><Btn label="If down (PE)" kind="ghost" onPress={() => dryRun(-1)} /></View>
        </Row>
        {preview && (
          <Text style={[s.muted, { color: preview.risk_check === "PASS" ? C.green : C.red }]}>
            {preview.action} {contractText(preview)} · entry {preview.entry_price ?? "—"} · SL {preview.stop_loss ?? "—"} · T {preview.target_price ?? "—"} ·
            qty {preview.quantity ?? "—"} · max loss {inr(preview.est_max_loss)} · {preview.risk_check}{preview.risk_problems?.length ? ` — ${preview.risk_problems.join("; ")}` : ""}
          </Text>
        )}
      </Section>
      </>)}

{tab === "settings" && (<>
      {editing ? (<>
        {a.enabled && <Card><Text style={{ color: C.blue }}>Saving restarts Paper Auto Trade with the new settings straight away (today's AI pick is kept when it still applies).</Text></Card>}
        <AutoForm initial={fromSnap(a)} autoId={id} onSaved={(x) => { setA(x); setEditing(false); }} />
        <Btn label="Cancel" kind="ghost" onPress={() => setEditing(false)} />
      </>) : (
        <Card title="Current settings">
          <Text style={[s.text, { lineHeight: 21 }]}>
            {c.mode === "live" ? "💰 LIVE" : "📝 Paper"} · {c.autopilot ? "✦ AI Autopilot" : c.strategy_source === "ai" ? "AI picks the strategy" : c.strategy} · {ixLabel(a)}{c.autopilot ? ((c as any).autopilot_index ?? "ANY") === "ANY" ? " (AI chooses index)" : " (fixed index)" : ""} ·
            {" "}{c.interval.replace("minute", "-min")} candles{"\n"}
            New trades {c.entry_start}–{c.entry_end}, all closed by {c.exit_time}{"\n"}
            {a.money ? `Money automatic: ₹${a.money.capital.toLocaleString("en-IN")} capital` : `${c.lots} lot(s) · max loss ₹${c.max_daily_loss}/day · ${c.max_trades_per_day} trades/day`}
            {" "}· up to {c.max_open_positions ?? 1} open · Day Builder {c.day_builder ?? "off"} · Trend Day {c.trend_day_entry ?? "off"}
          </Text>
          {posActive ? <Text style={{ color: C.amber }}>An open trade is running — settings can be changed after it closes.</Text>
            : c.mode === "live" && a.enabled ? <Text style={{ color: C.amber }}>Stop LIVE Auto Trade (⏹) to change its settings.</Text> : null}
          <Row>
            {!posActive && !(c.mode === "live" && a.enabled) && <IconBtn icon="✎" label="Change settings" onPress={() => setEditing(true)} />}
            {!a.enabled && !posActive && <IconBtn icon="🗑" label="Delete" kind="danger" onPress={() =>
              Alert.alert("Delete this strategy?", "Its settings and log are removed. Past trades stay in your reports.",
                [{ text: "Cancel", style: "cancel" }, { text: "Delete", style: "destructive", onPress: remove }])} />}
          </Row>
        </Card>
      )}
      </>)}

{tab === "log" && (<>
      <Card title="Activity" right={a.timeline.length ? (
        <Pressable onPress={() => confirmClear("clear_log")} hitSlop={10}><Text style={{ color: C.red, fontWeight: "700" }}>Clear log</Text></Pressable>
      ) : null}>
        {a.timeline.length === 0 && <Text style={s.muted}>Log is empty.</Text>}
        {[...a.timeline].reverse().slice(0, 50).map((e, i) => (
          <Text key={i} style={[s.muted, { color: e.kind === "alert" || e.kind === "risk" ? C.amber : e.kind === "trade" ? C.green : C.dim }]}>
            {when(e.t, true)}  {e.msg}
          </Text>
        ))}
      </Card>
      </>)}

    </Screen>
  );
}


/** How strong today's proof is: real costs, double-cost stress test and the luck check. */
function ProofBox({ p, live }: { p: NonNullable<AutoSnap["ai_pick"]>; live: boolean }) {
  if (!p.luck && !p.costs) return null;
  const lk = p.luck;
  return (
    <View style={{ padding: 10, borderRadius: 10, backgroundColor: (lk?.passed ? C.green : C.amber) + "18", gap: 3 }}>
      <Text style={[s.text, { fontWeight: "800" }]}>🔬 How strong is this proof?</Text>
      {p.costs && (
        <Text style={s.text}>
          • Tested with real costs: ₹{Math.round(p.costs.round_trip)} per lot per trade (Zerodha charges + {p.costs.slippage_pct_per_side}% slippage
          each way{p.costs.slippage_source ? ` — ${p.costs.slippage_source}` : ""}) + ₹{p.costs.theta_per_candle} time decay per candle held
        </Text>
      )}
      {p.stress && (
        <Text style={s.text}>
          • With costs doubled: ₹{Math.round(p.stress.test_pnl).toLocaleString("en-IN")} per lot on unseen data {p.stress.test_pnl > 0 ? "✓ still profitable" : "✗"}
        </Text>
      )}
      {lk && (
        <Text style={{ color: lk.passed ? C.green : C.amber, fontWeight: "700" }}>
          • Luck check: score {lk.t_stat ?? "—"} vs {lk.needed} needed (best of {lk.trials} tries with no real edge reaches ~{lk.needed} by chance) —{" "}
          {lk.passed ? "PASSED, unlikely to be luck" : live ? "NOT passed, so LIVE waits today" : "NOT passed: could be luck, so paper only"}
        </Text>
      )}
    </View>
  );
}

const TYPE_ICON: Record<string, string> = { daybuilt: "🧠", trendday: "📈", strategy: "🎯", any: "⚡", agreement: "🤝", override: "🚀", late: "⏱", pullback: "↩️", manual: "✋" };

/** Results by how trades were entered, AI-test vs real win rate, and a day-by-day journal. */
function ScorecardCard({ sc, versions }: { sc: Record<string, any>; versions?: any[] }) {
  const items: any[] = sc.items ?? [];
  const passed = items.filter((i) => i.ok === true).length;
  const fmt = (i: any) => i.value === null || i.value === undefined ? "—" : i.key === "net" ? inr(i.value) : i.key === "dd" || i.key === "costs" ? `${i.value}%` : String(i.value);
  const cur = (versions ?? []).slice(-1)[0];
  return (
    <Card title="🚦 Go-live scorecard" right={<Badge text={sc.ready ? "READY" : `${passed}/${items.length}`} color={sc.ready ? C.green : C.amber} />}>
      <Text style={[s.text, { fontWeight: "700", color: sc.ready ? C.green : C.text }]}>{sc.verdict}</Text>
      {items.map((i) => (
        <View key={i.key} style={{ flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 3 }}>
          <Text style={{ fontSize: 16, width: 20, color: i.ok === true ? C.green : i.ok === false ? C.red : C.dim }}>{i.ok === true ? "✓" : i.ok === false ? "✗" : "·"}</Text>
          <Text style={[s.text, { flex: 1 }]}>{i.label}</Text>
          <Text style={{ color: C.text, fontWeight: "700" }}>{fmt(i)}</Text>
          <Text style={[s.muted, { width: 92, textAlign: "right", fontSize: 12 }]}>{i.need}</Text>
        </View>
      ))}
      <Hint>
        Paper trades of settings version {sc.version}{cur?.since ? ` (since ${when(cur.since)})` : ""} only — changing settings starts a new
        version, so the record always matches what would go live.{cur?.changes?.length ? ` Last change: ${cur.changes.slice(0, 5).join(", ")}.` : ""}
      </Hint>
    </Card>
  );
}

function TuningCard({ t }: { t: Record<string, any> }) {
  return (
    <Section title="🔧 Self-tuning (from your own trades)"
      summary={t.ready ? `${(t.items ?? []).filter((i: any) => i.suggested !== "keep").length} suggestion(s) · full target reached ${t.target_reach_pct ?? "—"}%`
        : `needs ${t.need} trades with move tracking — ${t.trades} so far`}>
      <Row gap={8}>
        <Stat label="Avg best move" value={t.avg_mfe_pts !== undefined ? `+${t.avg_mfe_pts} pts` : "—"} color={C.green} />
        <Stat label="Avg worst move" value={t.avg_mae_pts !== undefined ? `${t.avg_mae_pts} pts` : "—"} color={C.red} />
        <Stat label="Full target reached" value={t.target_reach_pct !== null && t.target_reach_pct !== undefined ? `${t.target_reach_pct}%` : "—"} />
      </Row>
      {(t.items ?? []).map((i: any) => (
        <View key={i.key} style={{ gap: 2, paddingVertical: 4 }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
            <Text style={[s.text, { fontWeight: "800", flex: 1 }]}>{i.title}</Text>
            <Text style={s.muted}>{i.now}</Text>
            <Text style={{ color: C.dim }}>→</Text>
            <Badge text={i.suggested} color={i.suggested === "keep" ? C.dim : C.blue} />
          </View>
          <Text style={s.muted}>{i.why}</Text>
        </View>
      ))}
      <Hint>Worked out from how far each trade went for and against you while it was open. Suggestions only — change a setting yourself if it makes sense (that starts a new settings version).</Hint>
    </Section>
  );
}

function Breakdown({ stats }: { stats: Record<string, any> }) {
  const types = Object.entries((stats.by_entry_type ?? {}) as Record<string, any>).sort((x, y) => y[1].trades - x[1].trades);
  const proofs = Object.entries((stats.by_proof ?? {}) as Record<string, any>).sort((x, y) => y[1].trades - x[1].trades);
  const drift = stats.drift;
  const daily = (stats.daily ?? []) as any[];
  const [open, setOpen] = useState<string | null>(daily[0]?.day ?? null);
  if (!types.length && !daily.length) return null;
  return (<>
    {types.length > 0 && (
      <Card title="Which entries make money">
        {types.map(([k, v]) => (
          <View key={k} style={{ flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: C.border }}>
            <Text style={{ fontSize: 18 }}>{TYPE_ICON[k] ?? "•"}</Text>
            <View style={{ flex: 1 }}>
              <Text style={{ color: C.text, fontWeight: "700" }}>{v.label}</Text>
              <Text style={s.muted}>{v.trades} trades · win {v.win_rate_pct}% · avg {inr(v.avg_net)}</Text>
            </View>
            <Text style={{ color: pnlColor(v.net), fontWeight: "800" }}>{inr(v.net)}</Text>
          </View>
        ))}
        <Text style={s.muted}>Turn off an entry type in Settings if it keeps losing after 15–20 trades.</Text>
      </Card>
    )}
    {Object.keys(stats.by_version ?? {}).length > 1 && (
      <Section title="Results by settings version" open
        summary={Object.entries(stats.by_version as Record<string, any>).map(([k, v]) => `v${k} ${inr(v.net)}`).join(" · ")}>
        {Object.entries(stats.by_version as Record<string, any>).reverse().map(([k, v]) => {
          const ver = (stats.versions ?? []).find((x: any) => String(x.v) === k);
          return (
            <View key={k} style={{ flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: C.border }}>
              <Badge text={`v${k}`} color={String(stats.settings_version) === k ? C.blue : C.dim} />
              <View style={{ flex: 1 }}>
                <Text style={s.muted}>{v.trades} trades · win {v.win_rate_pct}% · PF {v.profit_factor ?? "—"}{ver?.since ? ` · from ${when(ver.since)}` : ""}</Text>
                {ver?.changes?.length ? <Text style={[s.muted, { fontSize: 12 }]} numberOfLines={1}>changed: {ver.changes.join(", ")}</Text> : null}
              </View>
              <Text style={{ color: pnlColor(v.net), fontWeight: "800" }}>{inr(v.net)}</Text>
            </View>
          );
        })}
      </Section>
    )}
    {proofs.length > 1 && (
      <Card title="Strict picks vs practice picks">
        {proofs.map(([k, v]) => (
          <View key={k} style={{ flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: C.border }}>
            <View style={{ flex: 1 }}>
              <Text style={{ color: C.text, fontWeight: "700" }}>{v.label}</Text>
              <Text style={s.muted}>{v.trades} trades · win {v.win_rate_pct}% · avg {inr(v.avg_net)}</Text>
            </View>
            <Text style={{ color: pnlColor(v.net), fontWeight: "800" }}>{inr(v.net)}</Text>
          </View>
        ))}
        <Hint>If practice picks keep losing, the strict proof is right to WAIT. Only strict picks can trade LIVE.</Hint>
      </Card>
    )}
    {drift && (
      <Card title="AI test vs real results">
        <Row gap={8}>
          <Stat label="AI test win rate" value={`${drift.expected_win_pct}%`} />
          <Stat label="Real win rate" value={`${drift.actual_win_pct}%`} color={drift.gap_pct >= -10 ? C.green : C.red} />
          <Stat label="Trades compared" value={String(drift.trades)} />
        </Row>
        <Text style={{ color: drift.trades < 10 ? C.dim : drift.gap_pct >= -10 ? C.green : C.red, fontWeight: "700" }}>{drift.verdict}</Text>
      </Card>
    )}
    {daily.length > 0 && (
      <Card title="Daily journal">
        {daily.map((d) => (
          <Pressable key={d.day} onPress={() => setOpen(open === d.day ? null : d.day)}
            style={{ paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border, gap: 4 }}>
            <View style={{ flexDirection: "row", alignItems: "center" }}>
              <Text style={{ color: C.text, fontWeight: "700", flex: 1 }}>{d.day} · {d.trades} trade{d.trades === 1 ? "" : "s"} · win {d.win_rate_pct}%</Text>
              <Text style={{ color: pnlColor(d.net), fontWeight: "800" }}>{inr(d.net)}</Text>
            </View>
            {open === d.day && (d.trades_list as any[]).map((t, i) => (
              <Text key={i} style={s.muted}>
                {TYPE_ICON[t.entry_type] ?? "•"} {t.entry_time}–{t.exit_time} {t.symbol} · {String(t.exit_reason ?? "").replace(/_/g, " ")} ·{" "}
                <Text style={{ color: pnlColor(t.net), fontWeight: "700" }}>{inr(t.net)}</Text>
              </Text>
            ))}
          </Pressable>
        ))}
      </Card>
    )}
  </>);
}
