import React, { useState } from "react";
import { Text, View } from "react-native";
import { inr, pct } from "../api";
import { Badge, C, Card, LineChart, Row, Screen, Stat, pnlColor, s, Hint } from "../ui";

export default function OptionsResultScreen({ route }: any) {
  const res = route.params.result;
  const [all, setAll] = useState(false);
  const m = res.metrics;
  const cfg = res.config;
  const trades = all ? [...res.trades].reverse() : res.trades.slice(-40).reverse();
  return (
    <Screen>
      <Card>
        <Text style={{ color: C.text, fontSize: 18, fontWeight: "800" }}>
          {cfg.index} options · {cfg.side === "BUY" ? "BUY (long)" : "SELL (short)"} · {res.interval}
        </Text>
        <Text style={s.muted}>
          {cfg.strategy} · {cfg.option_rule} · {cfg.strike_method}{["ITM", "OTM"].includes(cfg.strike_method) ? ` ${cfg.strike_offset}` : ""} · {cfg.lots} lot(s) × {cfg.lot_size} ·
          entries {cfg.entry_start}–{cfg.entry_end}, exit {cfg.exit_time} · {m.premium_source === "real" ? "real premiums" : "estimated premiums"}
        </Text>
        <Text style={{ color: pnlColor(m.net_pnl), fontSize: 28, fontWeight: "800" }}>
          {inr(m.net_pnl)} <Text style={{ fontSize: 16 }}>({pct(m.total_return_pct)})</Text>
        </Text>
        <Text style={s.muted}>{m.start.slice(0, 10)} → {m.end.slice(0, 10)} · after brokerage, STT, exchange charges, GST and slippage</Text>
      </Card>

      <Card title="Equity curve">
        <LineChart values={res.equity_curve.map((p: any) => p.equity)} baseline={m.initial_capital} color={m.net_pnl >= 0 ? C.green : C.red} />
        <Text style={s.label}>Drawdown %</Text>
        <LineChart values={res.drawdown_curve.map((p: any) => p.dd)} height={70} color={C.red} />
      </Card>

      <Card title="Performance">
        <Row gap={8}>
          <Stat label="Total trades" value={String(m.trades)} />
          <Stat label="Winning" value={String(m.winning_trades)} color={C.green} />
          <Stat label="Losing" value={String(m.losing_trades)} color={C.red} />
          <Stat label="Win rate" value={`${m.win_rate_pct}%`} />
          <Stat label="Gross profit" value={inr(m.gross_profit)} color={C.green} />
          <Stat label="Gross loss" value={inr(m.gross_loss)} color={C.red} />
          <Stat label="Profit factor" value={m.profit_factor?.toString() ?? "—"} />
          <Stat label="Max drawdown" value={`${m.max_drawdown_pct}%`} color={C.red} />
          <Stat label="Avg win" value={inr(m.avg_win)} color={C.green} />
          <Stat label="Avg loss" value={inr(m.avg_loss)} color={C.red} />
          <Stat label="Realised R:R" value={m.avg_rr_realised ? `1 : ${m.avg_rr_realised}` : "—"} />
          <Stat label="Planned R:R" value={m.planned_rr ? `1 : ${m.planned_rr}` : "—"} />
          <Stat label="Charges" value={inr(m.total_charges)} color={C.amber} />
          <Stat label="Signals skipped" value={String(m.signals_skipped)} />
        </Row>
        <Text style={s.label}>How trades ended</Text>
        <View style={s.chips}>
          {Object.entries(m.exits as Record<string, number>).map(([k, v]) => (
            <Badge key={k} text={`${k.replace(/_/g, " ")}: ${v}`} color={k === "target" ? C.green : k.includes("stop") ? C.red : C.blue} />
          ))}
        </View>
        <Hint>
          Win rate and risk:reward are different things: a 1:2.1 plan only needs ~33% winners to break even before costs.
          {m.premium_source !== "real" ? " Estimated premiums ignore IV changes — confirm with real-premium mode and paper trading." : ""}
        </Hint>
      </Card>

      <Card title={`Trades (${res.trades.length})`} right={res.trades.length > 40 ? <Text style={{ color: C.blue }} onPress={() => setAll(!all)}>{all ? "Recent" : "All"}</Text> : null}>
        {trades.length === 0 && <Text style={s.muted}>No trades. Try a longer period, another strategy, or check skipped signals below.</Text>}
        {trades.map((t: any, i: number) => (
          <View key={i} style={{ paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border, gap: 2 }}>
            <View style={s.row}>
              <Badge text={t.action} color={t.side === "LONG" ? C.blue : C.amber} />
              <Text style={[s.text, { flex: 1 }]} numberOfLines={1}>{t.contract}</Text>
              <Text style={{ color: pnlColor(t.net_pnl), fontWeight: "800" }}>{inr(t.net_pnl, 0)}</Text>
            </View>
            <Text style={s.muted}>
              {t.entry_time.slice(0, 16).replace("T", " ")} @ {t.entry_price} → {t.exit_time.slice(11, 16)} @ {t.exit_price} · {t.exit_reason.replace(/_/g, " ")}
            </Text>
            <Text style={s.muted}>SL {t.stop_loss} · target {t.target ?? "—"} · {t.qty} qty · {t.r_multiple !== null ? `${t.r_multiple}R` : ""} · charges {inr(t.charges)}</Text>
          </View>
        ))}
      </Card>

      {res.skipped?.length > 0 && (
        <Card title="Skipped signals (latest)">
          {res.skipped.slice(-12).reverse().map((x: any, i: number) => (
            <Text key={i} style={s.muted}>{x.time.slice(0, 16).replace("T", " ")} — {x.reason}</Text>
          ))}
        </Card>
      )}
    </Screen>
  );
}
