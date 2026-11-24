import * as SecureStore from "expo-secure-store";
import React, { createContext, useContext, useEffect, useState } from "react";
import { Pressable, Text } from "react-native";

/**
 * Simple / Detailed view.
 *  Simple  — only what you need to act: status, today's money, the open trade, the next trade, buttons.
 *  Detailed — everything: proofs, tuning tables, logs, hints, execution stats.
 * Saved on the phone; tap the header chip (or Settings → View) to switch.
 */
export type ViewMode = "simple" | "detailed";
const KEY = "kitealgo_view";

type Ctx = { view: ViewMode; detailed: boolean; setView: (v: ViewMode) => void };
const ViewCtx = createContext<Ctx>({ view: "simple", detailed: false, setView: () => {} });

export function ViewProvider({ children }: { children: React.ReactNode }) {
  const [view, setV] = useState<ViewMode>("simple");
  useEffect(() => {
    SecureStore.getItemAsync(KEY).then((v) => { if (v === "detailed" || v === "simple") setV(v); }).catch(() => {});
  }, []);
  const setView = (v: ViewMode) => {
    setV(v);
    SecureStore.setItemAsync(KEY, v).catch(() => {});
  };
  return <ViewCtx.Provider value={{ view, detailed: view === "detailed", setView }}>{children}</ViewCtx.Provider>;
}

export const useView = () => useContext(ViewCtx);

/** Renders its children only in Detailed view. */
export function Detail({ children }: { children: React.ReactNode }) {
  return useView().detailed ? <>{children}</> : null;
}

/** Renders its children only in Simple view. */
export function SimpleOnly({ children }: { children: React.ReactNode }) {
  return useView().detailed ? null : <>{children}</>;
}

/** Header chip: "◧ Simple" / "▦ Detailed" — tap to switch. */
export function ViewChip() {
  const { detailed, setView } = useView();
  const col = detailed ? "#7C3AED" : "#0F766E";
  return (
    <Pressable onPress={() => setView(detailed ? "simple" : "detailed")} hitSlop={8}
      style={{ paddingHorizontal: 9, paddingVertical: 5, borderRadius: 14, backgroundColor: col + "1A", borderWidth: 1, borderColor: col + "77" }}>
      <Text style={{ color: col, fontWeight: "800", fontSize: 12 }}>{detailed ? "▦ Detailed" : "◧ Simple"}</Text>
    </Pressable>
  );
}
