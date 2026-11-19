import * as SecureStore from "expo-secure-store";
import React from "react";
import { Pressable, ScrollView, Text, View } from "react-native";
import { api } from "./api";
import { C } from "./ui";

const KEY = "kitealgo_last_crash";

/** Send a crash/error report to the phone server (kept in data/app_errors.log) so it can be fixed. */
async function report(kind: string, message: string, stack?: string) {
  const entry = { kind, message: String(message).slice(0, 400), stack: String(stack ?? "").slice(0, 1200), at: new Date().toISOString() };
  try {
    await api.clientLog(entry);
  } catch {
    try { await SecureStore.setItemAsync(KEY, JSON.stringify(entry).slice(0, 1900)); } catch { /* ignore */ }
  }
}

/** Errors outside the screen code (timers, promises) must not close the app: log them and keep running. */
export function installGlobalErrorHandler() {
  const g: any = global as any;
  const prev = g.ErrorUtils?.getGlobalHandler?.();
  g.ErrorUtils?.setGlobalHandler?.((e: any, isFatal?: boolean) => {
    report(isFatal ? "fatal" : "error", e?.message ?? String(e), e?.stack);
    if (isFatal && prev) prev(e, isFatal);
  });
  // a crash saved while the server was unreachable is sent on the next start
  SecureStore.getItemAsync(KEY).then(async (v) => {
    if (!v) return;
    try { await api.clientLog({ ...JSON.parse(v), kind: "earlier-crash" }); await SecureStore.deleteItemAsync(KEY); } catch { /* later */ }
  }).catch(() => {});
}

/** A screen error shows this card instead of closing the whole app. */
export class CrashGuard extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: any) { report("screen", error.message, `${error.stack ?? ""}\n${info?.componentStack ?? ""}`); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <View style={{ flex: 1, backgroundColor: C.bg, padding: 24, paddingTop: 60, gap: 12 }}>
        <Text style={{ color: C.text, fontSize: 20, fontWeight: "800" }}>This screen hit a problem</Text>
        <Text style={{ color: C.dim }}>The app did not close and Auto Trade on the server keeps running. The error was saved for fixing.</Text>
        <ScrollView style={{ maxHeight: 200 }}><Text style={{ color: C.red, fontSize: 12 }}>{this.state.error.message}</Text></ScrollView>
        <Pressable onPress={() => this.setState({ error: null })}
          style={{ backgroundColor: C.blue, padding: 14, borderRadius: 12, alignItems: "center" }}>
          <Text style={{ color: "#fff", fontWeight: "700" }}>Go back to the app</Text>
        </Pressable>
      </View>
    );
  }
}
