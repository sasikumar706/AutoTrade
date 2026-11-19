import React, { useEffect, useState } from "react";
import { ActivityIndicator, Text, View } from "react-native";
import { api, inr, pct, type BacktestResult, type Trade } from "../api";
import { Badge, C, Card, ErrorText, LineChart, Row, Screen, Stat, pnlColor, s, Hint } from "../ui";

export function TradeRow({ t }: { t: Trade }) {
  return (
    <View style={{ paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border, gap: 2 }}>
      <View style={s.row}>
        <Badge text={t.side} color={t.side === "LONG" ? C.green : C.red} />
        <Text style={[s.text, { flex: 1 }]}>{t.qty} @ {t.entry_price} → {t.exit_price}</Text>
        <Text style={{ color: pnlColor(t.net_pnl), fontWeight: "700" }}>{inr(t.net_pnl, 2)}</Text>
      </View>
      <Text style={s.muted}>
        {t.entry_time.replace("T", " ").slice(0, 16)} → {t.exit_time.replace("T", " ").slice(5, 16)} · {t.exit_reason.replace(/_/g, " ")} · charges {inr(t.charges, 2)}
      </Text>
    </View>
  );
}

export default function BacktestResultScreen({ route }: any) {
  const [res, setRes] = useState<BacktestResult | null>(route.params?.result ?? null);
  const [err, setErr] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    if (!res && route.params?.id) api.getBacktest(route.params.id).then(setRes).catch((e) => setErr(e.message));
  }, [route.params?.id]);

  if (!res) return <Screen>{err ? <ErrorText msg={err} /> : <ActivityIndicator color={C.blue} />}</Screen>;
  const m = res.metrics;
  const trades = showAll ? res.trades : res.trades.slice(-30).reverse();
  const cfg = res.config as Record<string, any>;

  return (
    <Screen>
      <Card>
        <Text style={{ color: C.text, fontSize: 18, fontWeight: "800" }}>
          {res.instrument.exchange}:{res.instrument.tradingsymbol} · {res.interval}
        </Text>
        <Text style={s.muted}>
          {String(cfg.strategy)} {JSON.stringify(cfg.params)} · {String(cfg.product)} · {m.start.slice(0, 10)} → {m.end.slice(0, 10)}
        </Text>
        <Text style={{ color: pnlColor(m.net_pnl), fontSize: 28, fontWeight: "800" }}>
          {inr(m.net_pnl)} <Text style={{ fontSize: 16 }}>({pct(m.total_return_pct)})</Text>
        </Text>
        <Text style={s.muted}>Buy & hold over same period: {pct(m.buy_and_hold_pct)}</Text>
      </Card>

      <Card title="Equity curve">
        <LineChart values={res.equity_curve.map((p) => p.equity)} baseline={m.initial_capital} color={m.net_pnl >= 0 ? C.green : C.red} />
        <Text style={s.label}>Drawdown %</Text>
        <LineChart values={res.drawdown_curve.map((p) => p.dd)} height={80} color={C.red} />
      </Card>

      <Card title="Performance">
        <Row gap={8}>
          <Stat label="Trades" value={String(m.trades)} />
          <Stat label="Win rate" value={`${m.win_rate_pct}%`} />
          <Stat label="Profit factor" value={m.profit_factor?.toString() ?? "—"} />
          <Stat label="Max drawdown" value={`${m.max_drawdown_pct}%`} color={C.red} />
          <Stat label="Sharpe" value={m.sharpe?.toString() ?? "—"} />
          <Stat label="CAGR" value={m.cagr_pct === null ? "—" : `${m.cagr_pct}%`} />
          <Stat label="Avg win" value={inr(m.avg_win)} color={C.green} />
          <Stat label="Avg loss" value={inr(m.avg_loss)} color={C.red} />
          <Stat label="Expectancy/trade" value={inr(m.expectancy)} color={pnlColor(m.expectancy)} />
          <Stat label="Gross P&L" value={inr(m.gross_pnl)} color={pnlColor(m.gross_pnl)} />
          <Stat label="Charges paid" value={inr(m.total_charges)} color={C.amber} />
          <Stat label="Max losing streak" value={String(m.max_consecutive_losses)} />
        </Row>
        <Hint>
          Fills at next candle open with slippage; stop assumed before target when both touch in a candle; Zerodha charges included.
          Past results don't guarantee future returns — paper-test before going live.
        </Hint>
      </Card>

      <Card title={`Trades (${res.trades.length})`} right={
        res.trades.length > 30 ? <Text style={{ color: C.blue }} onPress={() => setShowAll(!showAll)}>{showAll ? "Recent" : "Show all"}</Text> : null}>
        {trades.length === 0 ? <Text style={s.muted}>No trades — try a different period or parameters.</Text> : trades.map((t, i) => <TradeRow key={i} t={t} />)}
      </Card>
    </Screen>
  );
}
