import React, { useEffect, useState } from "react";
import { Text } from "react-native";
import { api, getSettings, savePcSettings, saveSettings, type Target } from "../api";
import { Btn, C, Card, Chips, ErrorText, Field, Screen, s, Hint } from "../ui";
import ZerodhaCard from "../ZerodhaCard";
import TradingModeCard from "../TradingModeCard";

/** Market-moving days (RBI, US Fed, Budget + your own) — Auto Trade skips or goes careful on them. */
function EventsCard() {
  const [ev, setEv] = useState<{ upcoming: { day: string; event: string }[]; mine: { day: string; event: string }[] } | null>(null);
  const [day, setDay] = useState("");
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.marketEvents().then(setEv).catch((e) => setErr(e.message)); }, []);
  const save = async (mine: { day: string; event: string }[]) => {
    try { setEv(await api.saveEvents(mine)); setErr(null); } catch (e: any) { setErr(e.message); }
  };
  return (
    <Card title="📅 Event days">
      {ev?.upcoming.map((e) => {
        const own = ev.mine.some((m) => m.day === e.day);
        return (
          <Text key={e.day} style={s.text} onPress={own ? () => save(ev.mine.filter((m) => m.day !== e.day)) : undefined}>
            {e.day} · {e.event}{own ? "  ✕" : ""}
          </Text>
        );
      })}
      <Field label="Add a day (YYYY-MM-DD)" value={day} onChangeText={setDay} placeholder="2026-12-15" />
      <Field label="What happens" value={name} onChangeText={setName} placeholder="e.g. Election results" />
      <Btn label="Add event day" kind="ghost" onPress={() => {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(day.trim())) return setErr("Date must be YYYY-MM-DD");
        save([...(ev?.mine ?? []), { day: day.trim(), event: name.trim() || "Event" }]); setDay(""); setName("");
      }} />
      <ErrorText msg={err} />
      <Hint>RBI policy, US Fed and Budget days are built in. Tap ✕ on your own days to remove them. Each Auto Trade chooses to skip these days or trade 1 lot.</Hint>
    </Card>
  );
}

/** Phone server (Termux, backtest + paper) and PC server (static IP, live orders). */
export default function SettingsScreen() {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [pcUrl, setPcUrl] = useState("");
  const [pcToken, setPcToken] = useState("");
  const [autoOn, setAutoOn] = useState<Target>("phone");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pcMsg, setPcMsg] = useState<string | null>(null);
  const [pcErr, setPcErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [ntfy, setNtfy] = useState<{ ok: boolean; t: string } | null>(null);

  useEffect(() => {
    getSettings().then((c) => { setUrl(c.url); setToken(c.token); setPcUrl(c.pcUrl); setPcToken(c.pcToken); setAutoOn(c.autoOn); });
  }, []);

  async function savePhone() {
    setBusy("phone"); setMsg(null); setErr(null);
    try {
      if (!/^https?:\/\//.test(url.trim())) throw new Error("URL must start with http:// or https:// (phone server: http://127.0.0.1:8000)");
      await saveSettings(url, token);
      const h = await api.healthOn("phone");
      await api.authStatusOn("phone"); // checks the app token too
      setMsg(`Connected ✓ server time ${h.time.slice(11, 19)}`);
    } catch (e: any) { setErr(e.message); } finally { setBusy(null); }
  }

  async function savePc() {
    setBusy("pc"); setPcMsg(null); setPcErr(null);
    try {
      if (pcUrl.trim() && !/^https?:\/\//.test(pcUrl.trim())) throw new Error("URL must start with http:// or https://, e.g. http://192.168.1.20:8000");
      await savePcSettings(pcUrl, pcToken, autoOn);
      if (!pcUrl.trim()) { setPcMsg("PC server removed — everything runs on the phone."); return; }
      const info = await api.serverInfo("pc");
      let line = `Connected ✓ ${info.host} (${info.platform}) · real-money orders ${info.live_trading_enabled ? "ENABLED" : "OFF"} · public IP ${info.public_ip ?? "unknown"}`;
      try {
        const [ph, pc] = await Promise.all([api.authStatusOn("phone").catch(() => null), api.authStatusOn("pc")]);
        if (!pc.logged_in && ph?.logged_in) { await api.syncSessionToPc(); line += " · Kite login copied from phone ✓"; }
        else if (pc.logged_in) line += " · Kite logged in ✓";
      } catch (e: any) { line += ` · Kite login not copied (${e.message})`; }
      setPcMsg(line);
    } catch (e: any) { setPcErr(e.message); } finally { setBusy(null); }
  }

  return (
    <Screen>
      <TradingModeCard />
      <ZerodhaCard />
      <Card title="📱 Phone server — backtest & paper">
        <Field label="Server URL" value={url} onChangeText={setUrl} placeholder="http://127.0.0.1:8000" />
        <Field label="App token" value={token} onChangeText={setToken} secure placeholder="APP_TOKEN from the phone's backend/.env" />
        <Btn label="Save & test phone server" onPress={savePhone} loading={busy === "phone"} />
        {msg ? <Text style={{ color: C.green }}>{msg}</Text> : null}
        <ErrorText msg={err} />
      </Card>

      <Card title="🖥 PC server — LIVE orders (static IP)">
        <Field label="PC server URL" value={pcUrl} onChangeText={setPcUrl} placeholder="http://192.168.1.20:8000" hint="leave empty if you have no PC server" />
        <Field label="PC app token" value={pcToken} onChangeText={setPcToken} secure placeholder="APP_TOKEN from the PC's backend\.env" />
        <Text style={s.label}>Run Auto Trade on</Text>
        <Chips options={["phone", "pc"] as const} value={autoOn} onChange={setAutoOn}
          labels={{ phone: "📱 Phone (paper)", pc: "🖥 PC (live or paper)" }} />
        <Btn label="Save & test PC server" onPress={savePc} loading={busy === "pc"} />
        {pcMsg ? <Text style={{ color: C.green }}>{pcMsg}</Text> : null}
        <ErrorText msg={pcErr} />
      </Card>

      <Card title="🔔 Phone alerts (orders, exits, problems)">
        <Hint>
          The phone server shows normal Android notifications itself — no extra account. One-time setup:{"\n"}
          1. Install the "Termux:API" app from the same place you got Termux (F-Droid or GitHub), open it once.{"\n"}
          2. In Termux run:  pkg install termux-api   then restart the server.{"\n"}
          3. Tap the button below and allow notifications if Android asks.{"\n"}
          (For a cloud/PC server later, alerts go through the free ntfy app instead — NTFY_TOPIC in backend/.env.)
        </Hint>
        <Btn label="Send test alert" kind="ghost" loading={busy === "ntfy"} onPress={async () => {
          setBusy("ntfy"); setNtfy(null);
          try { const r = await api.notifyTest(); setNtfy({ ok: true, t: `Sent ✓ via ${(r as any).channels?.join(" + ") ?? "phone"} — check your notifications` }); }
          catch (e: any) { setNtfy({ ok: false, t: e.message }); } finally { setBusy(null); }
        }} />
        {ntfy ? <Text style={{ color: ntfy.ok ? C.green : C.red }}>{ntfy.t}</Text> : null}
      </Card>

      <EventsCard />

      <Card title="Which server does what">
        <Hint>
          • Paper (Manual) → phone server.{"\n"}
          • Live mode → PC server (Zerodha accepts orders only from your whitelisted static IP).{"\n"}
          • Paper + Auto → the server chosen above.{"\n"}
          • Log in to Kite once each morning (on the phone); the app copies that login to the PC automatically. Both servers
          must use the same Kite API key.{"\n"}
          • Phone and PC must be on the same Wi-Fi, or use a private network app such as Tailscale to reach the PC from anywhere.
        </Hint>
      </Card>
    </Screen>
  );
}
