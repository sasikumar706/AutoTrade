import * as SecureStore from "expo-secure-store";
import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import { Alert, Pressable, Text } from "react-native";
import { ApiError, api, setRoutingMode, targetFor, type AppSettings, type IndexKey } from "./api";
import { C } from "./ui";

/** Settings decide two things: money (Paper or Live) and Auto or Manual. Manual trading always works.
 *  In Auto the server runs the app's own Autopilot (intraday) and Swing strategies — nothing to create or enable. */
export type AppMode = "paper" | "live";
export const LIVE_CONFIRM = "I UNDERSTAND REAL MONEY";

export const MODE_INFO: Record<AppMode, { label: string; icon: string; color: string }> = {
  paper: { label: "Paper", icon: "📝", color: C.green },
  live: { label: "Live", icon: "💹", color: C.red },
};

/** An order to pre-fill on the Paper/Live option ticket (e.g. from the AI finder). */
export type TicketPrefill = { index: IndexKey; optionType: "CE" | "PE"; nonce: number; note?: string };

const KEY = "kitealgo_mode";
const KEY_AUTO = "kitealgo_auto";
const route = (money: AppMode, auto: boolean) => (money === "live" ? "live" : auto ? "auto" : "paper");

type Ctx = {
  mode: AppMode; auto: boolean; app: AppSettings | null;
  /** Save a change (money, auto, which strategies, capital). Asks before real money. Returns null if cancelled. */
  apply: (upd: Partial<AppSettings>) => Promise<AppSettings | null>;
  setMode: (m: AppMode) => void;
  refresh: () => Promise<void>;
};
const ModeCtx = createContext<Ctx>({ mode: "paper", auto: false, app: null, apply: async () => null, setMode: () => {}, refresh: async () => {} });

const askLive = () => new Promise<boolean>((res) => Alert.alert("Auto trade with REAL money?",
  "The app will place real Zerodha orders by itself (intraday Autopilot and/or Swing), sized from your Kite funds. Stop any time in Settings.",
  [{ text: "Cancel", style: "cancel", onPress: () => res(false) }, { text: "Yes, real money", style: "destructive", onPress: () => res(true) }],
  { cancelable: true, onDismiss: () => res(false) }));

export function ModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<AppMode>("paper");
  const [auto, setAuto] = useState(false);
  const [app, setApp] = useState<AppSettings | null>(null);
  const cur = useRef({ mode: "paper" as AppMode, auto: false });

  const adopt = (a: AppSettings) => {
    cur.current = { mode: a.money, auto: a.auto };
    setRoutingMode(route(a.money, a.auto));
    setModeState(a.money); setAuto(a.auto); setApp(a);
    SecureStore.setItemAsync(KEY, a.money).catch(() => {});
    SecureStore.setItemAsync(KEY_AUTO, a.auto ? "1" : "0").catch(() => {});
  };

  const refresh = async () => {
    try { adopt(await api.appSettings()); } catch { /* server not reachable yet: keep the last known choice */ }
  };

  useEffect(() => {
    (async () => {
      const [m, a] = await Promise.all([SecureStore.getItemAsync(KEY).catch(() => null), SecureStore.getItemAsync(KEY_AUTO).catch(() => null)]);
      const money: AppMode = m === "live" ? "live" : "paper"; // older "auto"/"backtest" choices fall back to paper
      const au = a === "1" || m === "auto";
      cur.current = { mode: money, auto: au };
      setRoutingMode(route(money, au)); setModeState(money); setAuto(au);
      await refresh();
    })();
  }, []);

  const apply = async (upd: Partial<AppSettings>) => {
    const was = { money: cur.current.mode, auto: cur.current.auto };
    const next = { money: (upd.money ?? was.money) as AppMode, auto: upd.auto ?? was.auto };
    const runs = (upd.intraday ?? app?.intraday ?? true) || (upd.swing ?? app?.swing ?? true);
    const liveAuto = next.money === "live" && next.auto && runs;
    if (liveAuto && !(was.money === "live" && was.auto) && !(await askLive())) return null;
    const oldT = await targetFor(route(was.money, was.auto));
    const newT = await targetFor(route(next.money, next.auto));
    if (oldT !== newT && was.auto) await api.saveAppSettings({ auto: false }, oldT).catch(() => null); // stop it on the other server
    // show the choice at once; put it back if the server refuses
    setModeState(next.money); setAuto(next.auto);
    let res: AppSettings;
    try {
      res = await api.saveAppSettings({ ...upd, money: next.money, auto: next.auto, confirm_live: liveAuto ? LIVE_CONFIRM : "" }, newT);
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 404) {
        if (!next.auto) { // older server: Paper/Live for manual trading still switches (it is only the app's choice)
          const local = { ...(app ?? {}), money: next.money, auto: false } as AppSettings;
          adopt(local);
          return local;
        }
        setModeState(was.money); setAuto(was.auto);
        throw new Error(`The ${newT === "pc" ? "PC" : "phone"} server is an older version without Auto mode. Update it in Termux:\n` +
          "cd ~/kite-algo-app && git pull && bash backend/deploy/termux_start.sh");
      }
      setModeState(was.money); setAuto(was.auto);
      throw e;
    }
    adopt(res);
    return res;
  };

  /** Used by screens that jump to a Paper/Live ticket (manual trading). */
  const setMode = (m: AppMode) => {
    if (m === cur.current.mode) return;
    apply({ money: m }).catch((e) => Alert.alert("Mode", e.message));
  };

  return <ModeCtx.Provider value={{ mode, auto, app, apply, setMode, refresh }}>{children}</ModeCtx.Provider>;
}

export const useMode = () => useContext(ModeCtx);

/** Header chip: "📝 Paper · Manual" / "💹 Live · ⚡ Auto". Tap → Settings. */
export function ModeChip({ onPress }: { onPress: () => void }) {
  const { mode, auto } = useMode();
  const info = MODE_INFO[mode];
  return (
    <Pressable onPress={onPress} hitSlop={8}
      style={{ flexDirection: "row", alignItems: "center", gap: 4, paddingHorizontal: 10, paddingVertical: 5, borderRadius: 14,
        backgroundColor: info.color + "1F", borderWidth: 1, borderColor: info.color + "88" }}>
      <Text style={{ color: info.color, fontWeight: "800", fontSize: 13 }}>{info.icon} {info.label}</Text>
      <Text style={{ color: auto ? C.amber : C.dim, fontWeight: "800", fontSize: 13 }}>· {auto ? "⚡ Auto" : "Manual"}</Text>
    </Pressable>
  );
}
