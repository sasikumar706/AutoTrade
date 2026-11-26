import * as SecureStore from "expo-secure-store";

export type Param = { key: string; label: string; default: number; min: number | null; max: number | null; step: number; kind: "int" | "float" };
export type StrategyInfo = { key: string; name: string; description: string; intraday_only: boolean; params: Param[] };
export type Instrument = {
  instrument_token: number; exchange: string; tradingsymbol: string; name: string;
  instrument_type: string; segment: string; expiry?: string | null; strike?: number; lot_size: number; tick_size?: number;
};
export type Trade = {
  side: "LONG" | "SHORT"; qty: number; entry_time: string; entry_price: number; exit_time: string; exit_price: number;
  gross_pnl: number; charges: number; net_pnl: number; exit_reason: string; entry_reason?: string; return_pct?: number;
};
export type Metrics = {
  initial_capital: number; final_equity: number; net_pnl: number; total_return_pct: number; cagr_pct: number | null;
  max_drawdown_pct: number; sharpe: number | null; sortino: number | null; calmar: number | null; trades: number;
  win_rate_pct: number; avg_win: number; avg_loss: number; profit_factor: number | null; expectancy: number;
  total_charges: number; gross_pnl: number; max_consecutive_losses: number; bars: number; start: string; end: string;
  buy_and_hold_pct: number;
};
export type BacktestResult = {
  id?: string; metrics: Metrics; trades: Trade[]; interval: string;
  equity_curve: { t: string; equity: number }[]; drawdown_curve: { t: string; dd: number }[];
  instrument: Partial<Instrument>; config: Record<string, unknown>;
};
export type SessionSnapshot = {
  id: string; status: "running" | "stopped" | "halted" | "error" | "finished"; message: string;
  config: Record<string, any>; started: string; ltp: number | null; last_tick: string | null;
  position: { qty: number; entry_price: number | null; entry_time: string | null; stop: number | null; target: number | null; unrealized: number };
  pending_orders: string[]; realized_pnl: number; charges: number; net_pnl: number; trades: Trade[];
  trade_count: number; candles_seen: number;
  running_candle: { date: string; open: number; high: number; low: number; close: number; volume: number } | null;
};
export type SessionEvent = { ts: number; kind: string; data: any };
export type AuthStatus = {
  logged_in: boolean; live_trading_enabled: boolean;
  session: { user_id: string; user_name: string; login_time: string; expires: string } | null;
  ticker: { connected: boolean; last_error: string; started: boolean };
};

export type IndicatorSignal = { indicator: string; value: string | number | null; vote: number; weight: number; reason: string };
export type Snapshot = {
  as_of: string; close: number; verdict: "BULLISH" | "BEARISH" | "NEUTRAL"; score: number; regime: "TRENDING" | "RANGING";
  volatility_atr_pct: number; signals: IndicatorSignal[]; suggested_style: string;
};
export type FinderRow = {
  strategy: string; name: string; params: Record<string, number>;
  train: { trades: number; total_return_pct: number; win_rate_pct: number; profit_factor: number | null; max_drawdown_pct: number };
  test: { trades: number; net_pnl: number; total_return_pct: number; win_rate_pct: number; profit_factor: number | null; max_drawdown_pct: number };
  score: number | null; robust: boolean; now: { action: string; position: string; since: string | null };
  option_type?: "CE" | "PE" | null; // only for NIFTY/BANKNIFTY/SENSEX analyses
};
export type AnalyzeResult = {
  instrument: Partial<Instrument>; interval: string; snapshot: Snapshot; summary: string; summary_source: "rules" | "claude";
  ai_available: boolean; ai_error: string | null;
  product?: string; allow_short?: boolean; index_key?: IndexKey | null; id?: string | null; created?: number | null;
  forecast?: Forecast | null;
  finder: null | { train_period: [string, string]; test_period: [string, string]; combinations_tested: number;
    buy_and_hold_test_pct: number; ranking: FinderRow[]; product: string };
};

export type IndexMove = {
  last: number | null; prev_close: number | null; change: number | null; change_pct: number | null;
  open?: number; high?: number; low?: number; direction: "UP" | "DOWN" | "FLAT";
};

export type MarketQuote = IndexMove & {
  range_pos: number | null; volume: number | null; avg_price: number | null; upper_circuit: number | null;
  lower_circuit: number | null; bid: number | null; ask: number | null; oi: number | null; last_trade_time: string | null;
};
export type MarketRow = MarketQuote & { exchange: string; tradingsymbol: string; name: string };
export type MarketDetail = {
  instrument: { exchange: string; tradingsymbol: string; name: string; instrument_type: string; segment: string; lot_size: number };
  quote: MarketQuote;
  stats: { high_52w?: number; low_52w?: number; from_52w_high_pct?: number | null; returns?: Record<string, number | null> };
  intraday: { day: string | null; times: string[]; closes: number[] };
  trend: null | { verdict: string; score: number; regime: string; plain: string;
    signals: { indicator: string; value: any; vote: number; reason: string }[] };
};

export type Forecast = {
  direction: "UP" | "DOWN" | "NONE"; headline: string; strategy: string | null; strategy_key?: string; params?: Record<string, number>;
  signal?: string; fresh?: boolean; since?: string | null; close: number | null; interval: string;
  expected_pts?: number | null; risk_pts?: number | null; win_rate_pct?: number; trades?: number;
  target_price?: number | null; stop_price?: number | null; hold_minutes?: number | null; edge_pts?: number | null;
  agree_with_indicators?: boolean;
};

export type AnalysisItem = {
  id: string; created: number; title: string; interval: string; verdict: string; score: number;
  top: string | null; top_action: string | null; index_key: IndexKey | null;
};

export type RiskSettings = {
  sl_mode: "pct" | "points" | "price"; sl_value: number;
  target_mode: "rr" | "pct" | "points" | "price" | "none"; target_value: number;
  trail_mode: "none" | "pct" | "points"; trail_value: number; trail_activate_rr: number;
  max_risk_per_trade: number | null;
};
export const DEFAULT_RISK: RiskSettings = {
  sl_mode: "pct", sl_value: 20, target_mode: "rr", target_value: 2.1, trail_mode: "none", trail_value: 0,
  trail_activate_rr: 1, max_risk_per_trade: null,
};
export type IndexKey = "NIFTY" | "BANKNIFTY" | "SENSEX";
export type ChainInfo = { index: IndexKey; expiries: string[]; step: number; exchange: string; future: any; spot: any };
export type ContractQuote = {
  contract: { tradingsymbol: string; strike: number; expiry: string; option_type: string; lot_size: number; moneyness: string; exchange: string };
  spot: number; lots: number; quantity: number; ltp: number | null; bid: number | null; ask: number | null; prev_close: number | null;
  spread_pct?: number; oi?: number; volume?: number; risk_error?: string; margin_required?: number; margin_available?: number; margin_error?: string;
  preview?: { entry: number; stop_loss: number; target: number | null; qty: number; lots: number; risk_reward: number | null;
    max_loss: number; target_profit: number; order_value: number; est_charges: number };
};
export type PositionSnap = {
  id: string; mode: "paper" | "live"; state: string; message: string;
  spec: { tradingsymbol: string; exchange: string; lots: number; lot_size: number; qty: number; side: number; instrument_type: string;
    strike: number; expiry: string; source: string; product: string };
  entry_price: number | null; exit_price: number | null; filled_qty: number; stop: number | null; stop0: number | null;
  target: number | null; ltp: number | null; unrealized: number; gross: number; charges: number; net: number;
  exit_reason: string | null; created: string; entry_time: string | null; exit_time: string | null;
  entry_order_id: string | null; sl_order_id: string | null; exit_order_id: string | null;
  stats: Record<string, any>; events: { t: string; kind: string; msg: string }[]; path?: [string, number][];
  booked?: { qty: number; price: number; gross: number; charges: number; time: string; reason: string }[]; booked_net?: number; initial_qty?: number;
};
export type AutoSnap = {
  id: string; config: Record<string, any>; enabled: boolean; paused: boolean; state: string; message: string;
  day: string | null; trades_today: number; realized_pnl: number; unrealized_pnl: number; remaining_risk_budget: number;
  day_blocked: string | null; trend_note?: string | null; levels: Record<string, any>; spot: number | null; last_tick: string | null;
  position: PositionSnap | null; last_signal: Record<string, any> | null; signals: Record<string, any>[];
  timeline: { t: string; kind: string; msg: string }[];
  ai_pick?: AiPick | null; ai_picking?: boolean; watch?: Watch | null; day_build?: Record<string, any> | null; day_building?: boolean;
  open_positions?: PositionSnap[];
  money?: { capital: number; source: string; risk_pct: number; risk_per_trade: number; day_stop: number; week_stop: number; month_stop: number;
    max_premium: number; trades_per_day: number; why_lower: string[] } | null;
  sizing?: { setup: string; quality: number; confidence: number | null; risk_pct: number; risk_rupees: number } | null;
};
export type Watch = {
  as_of: string; price: number | null; prev_close?: number; change?: number; change_pct?: number; phase?: string;
  status?: string; error?: string; strategy?: string; strategy_state?: string; since?: string | null;
  lean?: "BULLISH" | "BEARISH" | "NEUTRAL"; likelihood?: "HIGH" | "MEDIUM" | "LOW" | "NONE";
  bull_score?: number; bear_score?: number; checks_total?: number; candle_closes_at?: string; expected_direction?: number;
  bull_checks?: { label: string; ok: boolean }[]; bear_checks?: { label: string; ok: boolean }[];
  option?: null | { symbol: string; ltp: number | null; bid: number | null; ask: number | null; prev_close: number | null;
    open: number | null; high: number | null; low: number | null; change?: number; change_pct?: number; tick?: number; at: string };
  expected_order?: null | { action: string; tradingsymbol: string | null; strike: number | null; entry_price: number | null;
    stop_loss: number | null; target_price: number | null; quantity: number | null; lots: number; est_max_loss: number | null;
    risk_reward_ratio: number | null; risk_check: string; risk_problems: string[]; confidence: number; option_type?: "CE" | "PE"; entry_limit?: number | null };
};
export type AiPick = {
  strategy: string | null; name: string | null; params: Record<string, number>; day: string; picked_at: string;
  test?: { trades: number; total_return_pct: number | null; win_rate_pct: number; net_pnl?: number }; combinations_tested?: number;
  description?: string; unproven?: boolean;
  costs?: { round_trip: number; theta_per_candle: number; slippage_pct_per_side: number; slippage_source?: string };
  stress?: null | { test_pnl: number; test_trades: number; win_rate_pct: number };
  luck?: { t_stat: number | null; needed: number; trials: number; passed: boolean };
  risk_tuning?: null | { best: { stop_mult: number; target_rr: number; train_win: number; test_win: number; test_pnl: number;
    test_trades: number; kept: boolean };
    table: { stop_mult: number; target_rr: number; train_win: number; train_ret: number; test_win: number; test_pnl: number; test_trades: number }[] };
  tested?: { strategy: string; name: string; params: Record<string, number>; robust: boolean; why: string; description: string;
    test: { trades: number; net_pnl: number; win_rate_pct: number }; train_return_pct: number }[];
};
export type WhyItem = { group: "strategy" | "market" | "indicators" | "contract" | "risk"; ok: boolean; label: string };
export type JobSnap = { id: string; status: "running" | "done" | "error" | "cancelled"; error: string | null; progress: string; result?: any };

const KEY_URL = "server_url";
const KEY_TOKEN = "app_token";
const KEY_PC_URL = "pc_server_url";
const KEY_PC_TOKEN = "pc_app_token";
const KEY_AUTO_ON = "auto_server";

/** Two servers: the phone (Termux, 127.0.0.1) for backtests/paper, and a PC with a static IP for LIVE orders. */
export type Target = "phone" | "pc";
type Cfg = { url: string; token: string; pcUrl: string; pcToken: string; autoOn: Target };
let cache: Cfg | null = null;
let mode: string = "paper";

/** Short-lived links (10 min) to the trading report, for the phone's browser. */
export async function reportLinks(days: number, mode: "all" | "paper" | "live", source: "all" | "auto" | "manual") {
  const r = await request<{ html: string; csv: string }>("POST", "/report/link", { days, mode, source }, 20_000);
  const { url } = await base();
  return { html: url + r.html, csv: url + r.csv };
}

export async function getSettings(): Promise<Cfg & { url: string; token: string }> {
  if (cache) return cache;
  const g = async (k: string) => (await SecureStore.getItemAsync(k)) ?? "";
  cache = {
    url: await g(KEY_URL), token: await g(KEY_TOKEN),
    pcUrl: await g(KEY_PC_URL), pcToken: await g(KEY_PC_TOKEN),
    autoOn: ((await g(KEY_AUTO_ON)) === "pc" ? "pc" : "phone"),
  };
  return cache;
}

const clean = (u: string) => u.trim().replace(/\/+$/, "");

export async function saveSettings(url: string, token: string) {
  await SecureStore.setItemAsync(KEY_URL, clean(url));
  await SecureStore.setItemAsync(KEY_TOKEN, token.trim());
  cache = { ...(await getSettings()), url: clean(url), token: token.trim() };
}

export async function savePcSettings(url: string, token: string, autoOn: Target) {
  await SecureStore.setItemAsync(KEY_PC_URL, clean(url));
  await SecureStore.setItemAsync(KEY_PC_TOKEN, token.trim());
  await SecureStore.setItemAsync(KEY_AUTO_ON, autoOn);
  cache = { ...(await getSettings()), pcUrl: clean(url), pcToken: token.trim(), autoOn };
}

/** Called by the mode switch: Live (and Auto Trade if set to PC) talk to the PC server. */
export function setRoutingMode(m: string) { mode = m; }

export async function currentTarget(): Promise<Target> {
  return targetFor(mode);
}

/** Which server a mode talks to: live → PC (static IP), paper + Auto → the "Run Auto Trade on" server, paper → phone. */
export async function targetFor(m: string): Promise<Target> {
  const c = await getSettings();
  if (!c.pcUrl) return "phone";
  if (m === "live") return "pc";
  if (m === "auto") return c.autoOn;
  return "phone";
}

async function base(target?: Target) {
  const c = await getSettings();
  const t = target ?? (await currentTarget());
  return t === "pc" && c.pcUrl ? { url: c.pcUrl, token: c.pcToken, t } : { url: c.url, token: c.token, t: "phone" as Target };
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, timeoutMs = 120_000, target?: Target): Promise<T> {
  const { url, token, t } = await base(target);
  if (!url) throw new ApiError(0, "Set your server URL in Settings first");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url + path, {
      method,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
    const text = await res.text();
    let data: any = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { detail: text };
    }
    if (!res.ok) {
      const detail = typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail ?? data);
      throw new ApiError(res.status, detail || `HTTP ${res.status}`);
    }
    return data as T;
  } catch (e: any) {
    if (e instanceof ApiError) throw e;
    if (e?.name === "AbortError") throw new ApiError(0, "Request timed out");
    throw new ApiError(0, `Cannot reach ${t === "pc" ? "PC" : "phone"} server: ${e?.message ?? e}`);
  } finally {
    clearTimeout(timer);
  }
}

/** Settings → money mode (Paper / Live) and Auto or Manual. In Auto the server runs its own Autopilot + Swing. */
export type AppSettings = {
  money: "paper" | "live"; auto: boolean; intraday: boolean; swing: boolean; capital: number; swing_index: "NIFTY" | "BANKNIFTY";
  auto_id: string | null; swing_id: string | null; needs_resume: boolean; intraday_on: boolean; swing_on: boolean;
  live_trading_enabled: boolean; errors: Record<string, string>; starting?: boolean;
};

export const api = {
  appSettings: (t?: Target) => request<AppSettings>("GET", "/app-settings", undefined, 15_000, t),
  saveAppSettings: (body: Partial<AppSettings> & { confirm_live?: string }, t?: Target) =>
    request<AppSettings>("PUT", "/app-settings", body, 30_000, t),
  /** Copy today's Kite login from the phone server to the PC server (same Kite app, one login a day). */
  syncSessionToPc: async () => {
    const ex = await request<{ access_token: string; login_time: string }>("GET", "/auth/session-export", undefined, 15_000, "phone");
    return request<{ ok: boolean; session: any }>("POST", "/auth/session-import", { access_token: ex.access_token, login_time: ex.login_time }, 30_000, "pc");
  },
  authStatusOn: (t: Target) => request<AuthStatus>("GET", "/auth/status", undefined, 15_000, t),
  loginUrlPhone: () => request<{ url: string }>("GET", "/auth/login-url", undefined, 15_000, "phone"),
  healthOn: (t: Target) => request<{ ok: boolean; time: string; live_trading_enabled: boolean }>("GET", "/health", undefined, 10_000, t),
  serverInfo: (t: Target) => request<{ live_trading_enabled: boolean; public_ip: string | null; platform: string; host: string;
    max_live_lots: number; max_live_daily_loss: number; notifications?: boolean }>("GET", "/server/info", undefined, 15_000, t),
  notifyTest: (t?: Target) => request<{ ok: boolean }>("POST", "/notify/test", {}, 20_000, t),
  health: () => request<{ ok: boolean; time: string; live_trading_enabled: boolean }>("GET", "/health", undefined, 10_000),
  authStatus: () => request<AuthStatus>("GET", "/auth/status"),
  loginUrl: () => request<{ url: string }>("GET", "/auth/login-url"),
  logout: () => request("POST", "/auth/logout"),
  strategies: () => request<StrategyInfo[]>("GET", "/strategies"),
  search: (q: string, exchange?: string) =>
    request<Instrument[]>("GET", `/instruments/search?q=${encodeURIComponent(q)}${exchange ? `&exchange=${exchange}` : ""}`),
  analyze: (body: Record<string, unknown>) => request<AnalyzeResult>("POST", "/analyze", body, 600_000),
  marketOverview: () => request<MarketRow[]>("GET", "/market/overview", undefined, 15_000),
  marketDetail: (exchange: string, tradingsymbol: string) =>
    request<MarketDetail>("GET", `/market/detail?exchange=${exchange}&tradingsymbol=${encodeURIComponent(tradingsymbol)}`, undefined, 60_000),
  searchCash: (q: string) => request<Instrument[]>("GET", `/instruments/search?cash_only=true&q=${encodeURIComponent(q)}`),
  indices: () => request<Record<IndexKey, IndexMove>>("GET", "/market/indices", undefined, 15_000),
  analyses: () => request<AnalysisItem[]>("GET", "/analyses"),
  getAnalysis: (id: string) => request<AnalyzeResult>("GET", `/analyses/${id}`),
  deleteAnalysis: (id: string) => request("DELETE", `/analyses/${id}`),
  clearAnalyses: () => request<{ deleted: number }>("DELETE", "/analyses"),
  backtest: (body: Record<string, unknown>) => request<BacktestResult>("POST", "/backtest", body, 300_000),
  backtests: () => request<{ id: string; created: number; title: string; metrics: Metrics }[]>("GET", "/backtests"),
  getBacktest: (id: string) => request<BacktestResult>("GET", `/backtests/${id}`),
  deleteBacktest: (id: string) => request("DELETE", `/backtests/${id}`),
  sessions: () => request<{ active: SessionSnapshot[]; history: any[] }>("GET", "/sessions"),
  startSession: (body: Record<string, unknown>) => request<SessionSnapshot>("POST", "/sessions", body),
  session: (id: string) => request<SessionSnapshot>("GET", `/sessions/${id}`, undefined, 15_000),
  events: (id: string) => request<SessionEvent[]>("GET", `/sessions/${id}/events?limit=100`, undefined, 15_000),
  stopSession: (id: string, squareOff = true) => request<SessionSnapshot>("POST", `/sessions/${id}/stop?square_off=${squareOff}`),
  kill: () => request<{ stopped: string[] }>("POST", "/kill"),
  margins: () => request<any>("GET", "/account/margins"),
  chainInfo: (index: string) => request<ChainInfo>("GET", `/options/chain-info?index=${index}`),
  strikes: (index: string, expiry: string) => request<{ spot: number; strikes: number[]; lot_size: number }>("GET", `/options/strikes?index=${index}&expiry=${expiry}`),
  optionQuote: (body: Record<string, unknown>) => request<ContractQuote>("POST", "/options/quote", body, 30_000),
  optionsAnalysis: (index: string) => request<any>("GET", `/options/analysis?index=${index}`, undefined, 60_000),
  optionsBacktest: (body: Record<string, unknown>) => request<JobSnap>("POST", "/options/backtest", body),
  job: (id: string) => request<JobSnap>("GET", `/jobs/${id}`, undefined, 30_000),
  replayStart: (aid: string, days: number) => request<JobSnap>("POST", `/auto/${aid}/replay`, { days }, 30_000),
  replayLast: (aid: string) => request<Record<string, any>>("GET", `/auto/${aid}/replay`, undefined, 30_000),
  marketEvents: () => request<{ upcoming: { day: string; event: string }[]; mine: { day: string; event: string }[] }>("GET", "/events"),
  saveEvents: (items: { day: string; event: string }[]) =>
    request<{ upcoming: { day: string; event: string }[]; mine: { day: string; event: string }[] }>("PUT", "/events", items),
  cancelJob: (id: string) => request<JobSnap>("POST", `/jobs/${id}/cancel`),
  optionOrder: (body: Record<string, unknown>) => request<PositionSnap>("POST", "/options/order", body, 30_000),
  positionsList: (mode?: string) => request<PositionSnap[]>("GET", `/positions${mode ? `?mode=${mode}` : ""}`),
  positionsDay: (mode: string, day: string) => request<PositionSnap[]>("GET", `/positions?mode=${mode}&day=${day}&light=true`),
  /** Today only: every open position + whatever started/finished today (light rows). Falls back on older servers. */
  positionsToday: async (mode: string): Promise<PositionSnap[]> => {
    try { return await request<PositionSnap[]>("GET", `/positions/today?mode=${mode}`); }
    catch (e: any) {
      if (e?.status !== 404 && e?.status !== 405) throw e;
      const today = todayKey();
      return (await request<PositionSnap[]>("GET", `/positions?mode=${mode}`)).filter((p) =>
        !["POSITION_CLOSED", "ORDER_REJECTED", "ORDER_CANCELLED"].includes(p.state) || String(p.exit_time ?? p.entry_time ?? p.created ?? "").startsWith(today));
    }
  },
  /** Closed trades from earlier days only — the History tab. */
  positionsPast: (mode: string, source?: string) =>
    request<PositionSnap[]>("GET", `/positions/past?mode=${mode}${source ? `&source=${source}` : ""}`, undefined, 30_000),
  pnlDays: (mode: string) => request<{ day: string; trades: number; wins: number; net: number; by: Record<string, number> }[]>(
    "GET", `/positions/days?mode=${mode}`),
  position: (id: string) => request<PositionSnap>("GET", `/positions/${id}`, undefined, 15_000),
  clearPositions: (mode?: string, source?: string) =>
    request<{ deleted: number }>("DELETE", `/positions/history?${mode ? `mode=${mode}&` : ""}${source ? `source=${encodeURIComponent(source)}` : ""}`),
  clearSessions: (mode = "paper") => request<{ deleted: number }>("DELETE", `/sessions/history?mode=${mode}`),
  closePosition: (id: string) => request<PositionSnap>("POST", `/positions/${id}/close`),
  autos: () => request<AutoSnap[]>("GET", "/auto?light=true"),
  swings: () => request<Record<string, any>[]>("GET", "/swing"),
  swing: (id: string) => request<Record<string, any>>("GET", `/swing/${id}`),
  createSwing: (body: Record<string, unknown>) => request<Record<string, any>>("POST", "/swing", body),
  updateSwing: (id: string, body: Record<string, unknown>) => request<Record<string, any>>("PUT", `/swing/${id}`, body),
  swingControl: (id: string, action: string, confirm_live = "") => request<Record<string, any>>("POST", `/swing/${id}/control`, { action, confirm_live }),
  deleteSwing: (id: string) => request<{ ok: boolean }>("DELETE", `/swing/${id}`),
  swingBacktest: (index: string, sid = "") => request<Record<string, any>>("GET", `/swing-backtest?index=${index}${sid ? `&sid=${sid}` : ""}`, undefined, 120_000),
  clientLog: (entry: Record<string, unknown>) => request<{ ok: boolean }>("POST", "/client-log", entry, 10_000, "phone"),
  spreads: () => request<{ days: number; samples: number; median_spread_pct: number | null; p90_spread_pct: number | null;
    by_hour: Record<string, number>; slippage_pct_used: number; slippage_source: string }>("GET", "/spreads"),
  autoHealth: () => request<{ at: string; ok: boolean; checks: { name: string; ok: boolean; detail: string }[] }>("GET", "/auto-health", undefined, 30_000),
  createAuto: (body: Record<string, unknown>) => request<AutoSnap>("POST", "/auto", body),
  updateAuto: (id: string, body: Record<string, unknown>) => request<AutoSnap>("PUT", `/auto/${id}`, body),
  auto: (id: string) => request<AutoSnap>("GET", `/auto/${id}`, undefined, 15_000),
  autoControl: (id: string, action: string, confirm_live = "") => request<AutoSnap>("POST", `/auto/${id}/control`, { action, confirm_live }),
  deleteAuto: (id: string) => request("DELETE", `/auto/${id}`),
  autoPreview: (id: string, direction: number) => request<Record<string, any>>("POST", `/auto/${id}/preview-signal?direction=${direction}`, undefined, 30_000),
  analytics: (source?: string, mode?: string) =>
    request<Record<string, any>>("GET", `/analytics?${source ? `source=${source}&` : ""}${mode ? `mode=${mode}` : ""}`),
  positions: () => request<any>("GET", "/account/positions"),
  accountDay: (day?: string) => request<Record<string, any>>("GET", `/account/day${day ? `?day=${day}` : ""}`, undefined, 30_000),
  accountDays: () => request<Record<string, any>[]>("GET", "/account/days"),
};

export const inr = (n: number | null | undefined, digits = 0) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : (n < 0 ? "-₹" : "₹") + Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits });

export const pct = (n: number | null | undefined) => (n === null || n === undefined ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}%`);

/** Phone's local (IST) calendar day, YYYY-MM-DD. */
export const todayKey = () => new Date().toLocaleDateString("en-CA");
