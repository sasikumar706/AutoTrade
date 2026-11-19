import { useFocusEffect } from "@react-navigation/native";
import { useCallback } from "react";
import { AppState } from "react-native";

/**
 * Refresh while the screen is visible — one request at a time (the next starts only after the last finished),
 * and paused while the app is in the background. Prevents piles of overlapping requests when the phone
 * server is busy (e.g. during the AI test), which can slow the app down or get it closed by Android.
 */
export function usePollOnFocus(load: () => Promise<unknown> | unknown, ms: number) {
  useFocusEffect(useCallback(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (stop) return;
      if (AppState.currentState === "active") {
        try { await load(); } catch { /* screens show their own errors */ }
      }
      if (!stop) timer = setTimeout(tick, ms);
    };
    tick();
    return () => { stop = true; if (timer) clearTimeout(timer); };
  }, [load, ms]));
}
