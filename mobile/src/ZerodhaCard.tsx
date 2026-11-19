import { useFocusEffect } from "@react-navigation/native";
import * as Linking from "expo-linking";
import * as WebBrowser from "expo-web-browser";
import React, { useCallback, useState } from "react";
import { Alert, Text, View } from "react-native";
import { api, getSettings, type AuthStatus } from "./api";
import { Badge, C, Card, ErrorText, s } from "./ui";
import { IconBtn } from "./ui";

const REDIRECT = "kitealgo://login";

/** Zerodha connection: status, daily login (on the phone server, copied to the PC) and log out. Lives in Settings. */
export default function ZerodhaCard() {
  const [st, setSt] = useState<AuthStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [hasPc, setHasPc] = useState(false);

  const load = useCallback(async () => {
    const c = await getSettings();
    setHasPc(!!c.pcUrl);
    try { setSt(await api.authStatusOn("phone")); setErr(null); } catch (e: any) { setErr(e.message); setSt(null); }
  }, []);
  useFocusEffect(useCallback(() => { load(); }, [load]));

  async function login() {
    setBusy("login");
    try {
      // Kite redirects to http://127.0.0.1:8000/auth/callback, i.e. the PHONE server
      const { url } = await api.loginUrlPhone();
      const res = await WebBrowser.openAuthSessionAsync(url, REDIRECT);
      if (res.type === "success") {
        const q = Linking.parse(res.url).queryParams ?? {};
        if (q.status !== "ok") Alert.alert("Login failed", String(q.message ?? "Unknown error"));
        else if (hasPc) {
          try { await api.syncSessionToPc(); }
          catch (e: any) { Alert.alert("Logged in on the phone", `Could not copy the login to the PC server: ${e.message}`); }
        }
      }
      await load();
    } catch (e: any) { Alert.alert("Login error", e.message); } finally { setBusy(null); }
  }

  function logout() {
    Alert.alert("Log out of Kite?", "Live prices stop and Auto Trade can't place orders until you log in again. Open positions stay open at Zerodha.", [
      { text: "Cancel", style: "cancel" },
      { text: "Log out", style: "destructive", onPress: async () => {
        setBusy("logout");
        try { await api.logout(); await load(); } catch (e: any) { Alert.alert("Logout", e.message); } finally { setBusy(null); }
      } },
    ]);
  }

  return (
    <Card title="Zerodha" right={st ? <Badge text={st.logged_in ? "CONNECTED" : "LOGGED OUT"} color={st.logged_in ? C.green : C.amber} /> : null}>
      <ErrorText msg={err} />
      {err && !st ? <Text style={s.muted}>Start the server in Termux (termux_start.sh), then come back here.</Text> : null}
      {st?.logged_in && st.session ? (
        <>
          <Text style={s.text}>{st.session.user_name} ({st.session.user_id}) · until {st.session.expires.slice(11, 16)} IST</Text>
          <Text style={s.muted}>
            Data stream: {st.ticker.started ? (st.ticker.connected ? "connected" : "reconnecting…") : "idle"}
            {st.ticker.last_error ? ` · ${st.ticker.last_error}` : ""}
          </Text>
        </>
      ) : st ? (
        <Text style={s.muted}>Password and 2FA go only to Zerodha. Kite needs a fresh login every morning.</Text>
      ) : null}
      <View style={{ flexDirection: "row", gap: 10 }}>
        {st && !st.logged_in && <IconBtn icon="🔑" label="Login with Kite" onPress={login} loading={busy === "login"} kind="primary" />}
        {st?.logged_in && <IconBtn icon="⎋" label="Log out" onPress={logout} loading={busy === "logout"} kind="danger" />}
        <IconBtn icon="↻" label="Refresh" onPress={load} />
      </View>
    </Card>
  );
}
