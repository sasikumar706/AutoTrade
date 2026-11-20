import React from "react";
import { Screen } from "../ui";
import { DayHistory } from "./TradeTicket";

/** Earlier days' P&L: one line per day, tap a day for its trades. */
export default function PnlHistoryScreen({ navigation, route }: any) {
  const mode = route?.params?.mode === "live" ? "live" : "paper";
  return (
    <Screen>
      <DayHistory mode={mode} navigation={navigation} />
    </Screen>
  );
}
