import type { AnalyzeResult, FinderRow, IndexMove } from "./api";
import { C } from "./ui";

/**
 * A beginner-friendly traffic light for an index CE/PE idea.
 * GREEN only when every check agrees; one miss = AMBER (paper only); more = RED (don't trade).
 * Live trading is offered only on GREEN with a fresh analysis.
 */
export type Light = "GREEN" | "AMBER" | "RED";
export type SimpleSignal = {
  light: Light; color: string; option: "CE" | "PE" | null; headline: string; plain: string;
  checks: { label: string; ok: boolean }[]; liveOk: boolean; stale: boolean; idea: FinderRow | null;
};

export const lightColor: Record<Light, string> = { GREEN: C.green, AMBER: C.amber, RED: C.red };
export const lightEmoji: Record<Light, string> = { GREEN: "🟢", AMBER: "🟡", RED: "🔴" };
const STALE_MIN = 30;

export function bestOptionIdea(res: AnalyzeResult): FinderRow | null {
  const rows = (res.finder?.ranking ?? []).filter((r) => r.option_type);
  return rows.find((r) => r.robust) ?? rows[0] ?? null;
}

/** Favourability of one strategy row: robust with a signal = good, signal but weak = risky, no signal = wait. */
export function rowFavour(r: FinderRow): { label: string; color: string } {
  const hasSignal = r.option_type || ["BUY", "SELL", "HOLD LONG", "HOLD SHORT"].includes(r.now.action);
  if (!hasSignal) return { label: "WAIT", color: C.dim };
  return r.robust ? { label: "GOOD", color: C.green } : { label: "RISKY", color: C.amber };
}

export function simpleSignal(res: AnalyzeResult, move: IndexMove | null | undefined): SimpleSignal {
  const idx = res.index_key ?? "Index";
  const idea = bestOptionIdea(res);
  const ageMin = res.created ? (Date.now() / 1000 - res.created) / 60 : 0;
  const stale = ageMin > STALE_MIN;
  if (!idea || !idea.option_type) {
    return {
      light: "RED", color: C.red, option: null, idea: null, stale, liveOk: false,
      headline: "Don't trade now — wait",
      plain: `No strategy is giving a buy or sell signal on ${idx} right now. Not trading is also a decision — it keeps your money safe.`,
      checks: [{ label: "A strategy is giving a signal now", ok: false }],
    };
  }
  const up = idea.option_type === "CE";
  const dirWord = up ? "up" : "down";
  const verdictOk = res.snapshot.verdict === (up ? "BULLISH" : "BEARISH");
  const moveOk = move?.direction === (up ? "UP" : "DOWN");
  const pts = move && move.change !== null ? ` (${move.change > 0 ? "+" : ""}${move.change} pts today)` : "";
  const checks = [
    { label: `Best strategy says ${idea.now.action} (${idea.name})`, ok: true },
    { label: "That strategy made money on data it had never seen", ok: idea.robust },
    { label: `Indicators point ${dirWord} (${res.snapshot.verdict.toLowerCase()})`, ok: verdictOk },
    { label: `${idx} is actually moving ${dirWord} today${pts}`, ok: moveOk },
    { label: `Analysis is fresh (under ${STALE_MIN} min old)`, ok: !stale },
  ];
  const misses = checks.filter((c) => !c.ok).length;
  const light: Light = misses === 0 ? "GREEN" : misses === 1 ? "AMBER" : "RED";
  const opt = `${idx} ${idea.option_type}`;
  const headline = light === "GREEN" ? `Good setup: BUY ${opt}`
    : light === "AMBER" ? `Risky: BUY ${opt} on paper only`
    : `Don't trade now — signals disagree`;
  const plain = light === "GREEN"
    ? `All checks agree the market is going ${dirWord}. Buying the ${up ? "CE (call)" : "PE (put)"} gains if ${idx} keeps going ${dirWord}. Always keep the stop-loss.`
    : light === "AMBER"
      ? `Most checks say ${dirWord}, but one doesn't (see ✗ below). Practise on paper first — don't risk real money on this one.`
      : `The checks don't agree on a direction. Trades in a confused market often lose — wait for a clearer signal.`;
  return { light, color: lightColor[light], option: idea.option_type, idea, checks, stale, liveOk: light === "GREEN", headline, plain };
}
