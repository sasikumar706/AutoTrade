import React from "react";
import { Text, View } from "react-native";
import type { WhyItem } from "./api";
import { C, s } from "./ui";

const GROUPS: { key: WhyItem["group"]; title: string }[] = [
  { key: "strategy", title: "Strategy" },
  { key: "market", title: "Market today" },
  { key: "indicators", title: "Indicator checks" },
  { key: "contract", title: "Contract chosen" },
  { key: "risk", title: "Risk checks" },
];

/** Plain-language reasons for a signal: ✓ agreed / ✗ did not, grouped. */
export function WhyList({ why }: { why?: WhyItem[] | null }) {
  if (!why?.length) return <Text style={s.muted}>No details recorded for this signal.</Text>;
  return (
    <View style={{ gap: 8 }}>
      {GROUPS.map((g) => {
        const items = why.filter((w) => w.group === g.key);
        if (!items.length) return null;
        const passed = items.filter((w) => w.ok).length;
        return (
          <View key={g.key} style={{ gap: 3 }}>
            <Text style={[s.label, { marginTop: 0 }]}>
              {g.title}{g.key === "indicators" ? `  ·  ${passed}/${items.length} agree` : ""}
            </Text>
            {items.map((w, i) => (
              <Text key={i} style={{ color: w.ok ? C.green : C.red, fontSize: 13, lineHeight: 19 }}>
                {w.ok ? "✓" : "✗"}  <Text style={{ color: C.text }}>{w.label}</Text>
              </Text>
            ))}
          </View>
        );
      })}
    </View>
  );
}
