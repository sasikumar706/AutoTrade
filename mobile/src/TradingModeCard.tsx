import React, { useEffect, useState } from "react";
import { Text, View } from "react-native";
import { Btn, C, Card, Chips, ErrorText, Field, Toggle, s } from "./ui";
import { MODE_INFO, useMode } from "./mode";

/** Settings: money (Paper / Live) and Auto or Manual. In Auto the app runs its own strategies; manual orders always work. */
export default function TradingModeCard() {
  const { mode, auto, app, apply, refresh } = useMode();
  const [capital, setCapital] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { refresh(); }, []);
  // while the server is starting the strategies, check every 3 s
  useEffect(() => {
    if (!app?.starting) return;
    const t = setInterval(() => { refresh(); }, 3000);
    return () => clearInterval(t);
  }, [app?.starting]);
  useEffect(() => { if (app) setCapital(String(Math.round(app.capital))); }, [app?.capital]);

  const go = async (key: string, upd: Record<string, any>) => {
    setBusy(key); setErr(null);
    try { await apply(upd); } catch (e: any) { setErr(e.message); } finally { setBusy(null); }
  };
  const errs = app?.errors ? Object.entries(app.errors) : [];

  return (
    <Card title="⚙️ Trading mode">
      <Text style={s.label}>Money</Text>
      <Chips options={["paper", "live"] as const} value={mode} onChange={(m) => go("money", { money: m })}
        labels={{ paper: `${MODE_INFO.paper.icon} Paper (practice)`, live: `${MODE_INFO.live.icon} Live (real money)` }} />
      <Text style={s.label}>Trading</Text>
      <Chips options={["manual", "auto"] as const} value={auto ? "auto" : "manual"} onChange={(v) => go("auto", { auto: v === "auto" })}
        labels={{ manual: "✋ Manual", auto: "⚡ Auto" }} />
      {busy ? <Text style={{ color: C.blue, fontWeight: "700" }}>Saving…</Text> : null}
      {app?.starting ? <Text style={{ color: C.amber, fontWeight: "700" }}>Starting strategies… (up to a minute)</Text> : null}
      <Text style={s.muted}>
        {auto ? `The app trades ${mode === "live" ? "real" : "paper"} money by itself. Your own orders still work.` : "You place every order. Nothing trades by itself."}
      </Text>

      {auto && app && (
        <View style={{ gap: 8, marginTop: 4 }}>
          <Toggle label="Intraday Autopilot · out by 15:10" value={app.intraday}
            onChange={(v) => go("intraday", { intraday: v })} />
          <Toggle label={`Swing · ${app.swing_index} monthly CE`} value={app.swing}
            onChange={(v) => go("swing", { swing: v })} />
          {app.swing && (
            <Chips options={["BANKNIFTY", "NIFTY"] as const} value={app.swing_index} onChange={(x) => go("ix", { swing_index: x })}
              labels={{ BANKNIFTY: "BANKNIFTY (best tested)", NIFTY: "NIFTY" }} />
          )}
          {mode === "paper" ? (
            <>
              <Field label="Paper capital ₹" value={capital} onChangeText={setCapital} numeric hint="follows your P&L" />
              <Btn label="Save capital" kind="ghost" loading={busy === "cap"} onPress={() => go("cap", { capital: Number(capital) || 0 })} />
            </>
          ) : (
            <Text style={s.muted}>Sized from your Kite funds{app.live_trading_enabled ? "" : " · ⚠ server real-money switch is OFF"}</Text>
          )}
          <Text style={{ color: app.intraday_on || app.swing_on ? C.green : C.dim, fontWeight: "700" }}>
            {[app.intraday ? `Intraday ${app.intraday_on ? "running ✓" : "stopped"}` : null,
              app.swing ? `Swing ${app.swing_on ? "running ✓" : "stopped"}` : null].filter(Boolean).join(" · ") || "Nothing selected to run"}
          </Text>
          {app.needs_resume && (
            <Btn label={mode === "live" ? "Resume Auto (real money)" : "Resume Auto"} kind={mode === "live" ? "warn" : "primary"}
              loading={busy === "resume"} onPress={() => go("resume", { auto: true })} />
          )}
          {errs.map(([k, v]) => <Text key={k} style={{ color: C.amber }}>⚠ {k}: {v}</Text>)}
        </View>
      )}
      <ErrorText msg={err} />
    </Card>
  );
}
