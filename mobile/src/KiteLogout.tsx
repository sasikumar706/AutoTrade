import { useFocusEffect } from "@react-navigation/native";
import React, { useCallback, useState } from "react";
import { Alert, Text, View } from "react-native";
import { api, type AuthStatus } from "./api";
import { Btn, C, s } from "./ui";

/** Bottom-of-screen Kite session line with a "Log out of Kite" button (shown only when logged in). */
export default function KiteLogout({ onChange }: { onChange?: () => void }) {
  const [st, setSt] = useState<AuthStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => { api.authStatus().then(setSt).catch(() => setSt(null)); }, []);
  useFocusEffect(useCallback(() => { load(); }, [load]));
  if (!st?.logged_in || !st.session) return null;
  function logout() {
    Alert.alert("Log out of Kite?", "Live prices stop and Auto Trade can't place orders until you log in again. Open positions stay open at Zerodha.", [
      { text: "Cancel", style: "cancel" },
      { text: "Log out", style: "destructive", onPress: async () => {
        setBusy(true);
        try { await api.logout(); load(); onChange?.(); } catch (e: any) { Alert.alert("Logout", e.message); } finally { setBusy(false); }
      } },
    ]);
  }
  return (
    <View style={{ marginTop: 8, paddingTop: 12, borderTopWidth: 1, borderTopColor: C.border, gap: 6 }}>
      <Text style={[s.muted, { textAlign: "center" }]}>
        Kite: {st.session.user_name} ({st.session.user_id}) · session until {st.session.expires.slice(11, 16)} IST
      </Text>
      <Btn label="Log out of Kite" kind="ghost" onPress={logout} loading={busy} />
    </View>
  );
}

/** Confirm, then run a clear action and show how many records were removed. */
export function confirmClear(title: string, message: string, run: () => Promise<number>, after?: () => void) {
  Alert.alert(title, message, [
    { text: "Cancel", style: "cancel" },
    { text: "Clear", style: "destructive", onPress: async () => {
      try { const n = await run(); Alert.alert("Cleared", `${n} record(s) removed.`); after?.(); }
      catch (e: any) { Alert.alert("Clear failed", e.message); }
    } },
  ]);
}
