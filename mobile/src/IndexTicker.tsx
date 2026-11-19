import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useState } from "react";
import { Pressable, Text, View } from "react-native";
import { api, type IndexKey, type IndexMove } from "./api";
import { C } from "./ui";

const NAMES: Record<IndexKey, string> = { NIFTY: "NIFTY 50", BANKNIFTY: "BANK NIFTY", SENSEX: "SENSEX" };
export const moveColor = (d?: IndexMove["direction"]) => (d === "UP" ? C.green : d === "DOWN" ? C.red : C.dim);
export const moveWords = (m?: IndexMove | null) =>
  !m || m.change === null ? "—" : m.direction === "UP" ? "going up" : m.direction === "DOWN" ? "going down" : "flat";
const fmt = (n: number) => n.toLocaleString("en-IN", { maximumFractionDigits: 2 });

/** Today's index moves, refreshed every 5 s while the screen is open. */
export function useIndexMoves(enabled = true) {
  const [moves, setMoves] = useState<Record<IndexKey, IndexMove> | null>(null);
  useFocusEffect(useCallback(() => {
    if (!enabled) return;
    let alive = true;
    const load = () => api.indices().then((m) => { if (alive) setMoves(m); }).catch(() => {});
    load();
    const t = setInterval(load, 5000);
    return () => { alive = false; clearInterval(t); };
  }, [enabled]));
  return moves;
}

/** Three tiles: green ▲ when the index is up today, red ▼ when down, grey when flat. */
export function IndexTicker({ moves, selected, onPress }: {
  moves: Record<IndexKey, IndexMove> | null; selected?: IndexKey | null; onPress?: (k: IndexKey) => void;
}) {
  return (
    <View style={{ flexDirection: "row", gap: 8 }}>
      {(Object.keys(NAMES) as IndexKey[]).map((k) => {
        const m = moves?.[k];
        const col = moveColor(m?.direction);
        const has = m && m.change !== null;
        return (
          <Pressable key={k} onPress={() => onPress?.(k)}
            style={{ flex: 1, padding: 10, borderRadius: 12, backgroundColor: has ? col + "1F" : C.card,
              borderWidth: selected === k ? 2 : 1, borderColor: selected === k ? C.blue : has ? col + "66" : C.border, gap: 2 }}>
            <Text style={{ color: C.dim, fontSize: 11, fontWeight: "700" }}>{NAMES[k]}</Text>
            <Text style={{ color: C.text, fontSize: 15, fontWeight: "800" }}>{m?.last ? fmt(m.last) : "—"}</Text>
            {has ? (
              <>
                <Text style={{ color: col, fontWeight: "800", fontSize: 13 }}>
                  {m!.direction === "UP" ? "▲" : m!.direction === "DOWN" ? "▼" : "•"} {m!.change! > 0 ? "+" : ""}{fmt(m!.change!)} pts
                </Text>
                <Text style={{ color: col, fontSize: 11 }}>{m!.change_pct! > 0 ? "+" : ""}{m!.change_pct}% · {moveWords(m)}</Text>
              </>
            ) : <Text style={{ color: C.dim, fontSize: 11 }}>log in to see</Text>}
          </Pressable>
        );
      })}
    </View>
  );
}
