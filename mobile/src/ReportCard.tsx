import * as WebBrowser from "expo-web-browser";
import React, { useState } from "react";
import { Linking, Text } from "react-native";
import { reportLinks } from "./api";
import { Btn, Chips, ErrorText, Row, Section, s } from "./ui";

/** Trading history report: open a readable report, or download every trade as a spreadsheet (CSV). */
export default function ReportCard({ source = "all", mode = "all" }: { source?: "all" | "auto" | "manual"; mode?: "all" | "paper" | "live" }) {
  const [days, setDays] = useState<"7" | "30" | "90" | "365">("30");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const go = async (kind: "html" | "csv") => {
    setBusy(kind); setErr(null);
    try {
      const l = await reportLinks(Number(days), mode, source);
      if (kind === "html") await WebBrowser.openBrowserAsync(l.html);
      else await Linking.openURL(l.csv);
    } catch (e: any) { setErr(e.message); } finally { setBusy(null); }
  };
  return (
    <Section title="📄 Trading report" summary="Every trade: why, when, fill price, stop/target, exit, charges · open as PDF or Excel">
      <Chips options={["7", "30", "90", "365"] as const} value={days} onChange={setDays}
        labels={{ "7": "7 days", "30": "30 days", "90": "90 days", "365": "1 year" }} />
      <Row>
        <Btn label="Open report" onPress={() => go("html")} loading={busy === "html"} />
        <Btn label="Download Excel (CSV)" kind="ghost" onPress={() => go("csv")} loading={busy === "csv"} />
      </Row>
      <ErrorText msg={err} />
      <Text style={s.muted}>Opens in your browser — use ⋮ → Share or Print to save it as PDF or send it.</Text>
    </Section>
  );
}
