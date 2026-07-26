import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtDateTime, fmtMoney, fmtPct, fmtQty, fmtSigned, fmtTime, pnlClass } from "../format";
import { useStore } from "../store";
import type { AltExitStats, GateAlphaResponse, GateAlphaStats, ShadowAnalyticsResponse } from "../types";
import { TickerFlowVisualizer } from "./TickerFlowVisualizer";

type Tab = "positions" | "orders" | "trades" | "flow" | "logs" | "shadow";

export function BottomTabs() {
  const [tab, setTab] = useState<Tab>("positions");
  const positions = useStore((s) => s.positions);
  const trades = useStore((s) => s.trades);
  const logs = useStore((s) => s.logs);

  return (
    <div className="panel">
      <div className="tabs">
        <button className={tab === "positions" ? "active" : ""} onClick={() => setTab("positions")}>Positions {positions.length > 0 && `(${positions.length})`}</button>
        <button className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>Orders</button>
        <button className={tab === "trades" ? "active" : ""} onClick={() => setTab("trades")}>Trades {trades.length > 0 && `(${trades.length})`}</button>
        <button className={tab === "flow" ? "active" : ""} onClick={() => setTab("flow")}>Pipeline Flow</button>
        <button className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}>Logs</button>
        <button className={tab === "shadow" ? "active" : ""} onClick={() => setTab("shadow")}>Shadow Analytics</button>
        <span className="spacer" />
        {tab === "positions" && positions.length > 0 && <FlattenButton />}
      </div>
      <div className="panel-body">
        {tab === "positions" && <PositionsTable />}
        {tab === "orders" && <OrdersTable />}
        {tab === "trades" && <TradesTable />}
        {tab === "flow" && <TickerFlowVisualizer />}
        {tab === "shadow" && <ShadowAnalyticsTab />}
        {tab === "logs" && <div>{logs.length === 0 && <div className="empty-state">No log entries yet</div>}{logs.map((l, i) => <div className="log-line" key={`${l.ts}-${i}`}><span className="faint num">{fmtTime(l.ts)}</span><span className="src">{l.source}</span><span className={`msg lvl-${l.level}`}>{l.message}</span></div>)}</div>}
      </div>
    </div>
  );
}

function FlattenButton() {
  const showToast = useStore((s) => s.showToast);
  return <div className="tab-actions"><button className="btn sm danger" onClick={() => api.flatten().then(() => showToast("info", "All positions closed"), (e) => showToast("error", (e as Error).message))}>Flatten all</button></div>;
}

function PositionsTable() {
  const positions = useStore((s) => s.positions);
  const selectSymbol = useStore((s) => s.selectSymbol);
  if (positions.length === 0) return <div className="empty-state">No open positions</div>;
  return <table className="grid"><thead><tr><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Last</th><th className="r">Stop</th><th className="r">Target</th><th className="r">Value</th><th className="r">Unrlzd P&L</th><th>Strategy</th><th></th></tr></thead><tbody>{positions.map((p) => <tr key={p.symbol} onClick={() => selectSymbol(p.symbol)} style={{ cursor: "pointer" }}><td><b>{p.symbol}</b></td><td><span className={`side-tag ${p.side}`}>{p.side}</span></td><td className="r num">{fmtQty(Math.abs(p.qty))}</td><td className="r num">{p.avg_entry_price.toFixed(2)}</td><td className="r num">{p.market_price.toFixed(2)}</td><td className="r num faint">{p.stop_loss?.toFixed(2) ?? "—"}</td><td className="r num faint">{p.take_profit?.toFixed(2) ?? "—"}</td><td className="r num">{fmtMoney(p.market_value)}</td><td className={`r num ${pnlClass(p.unrealized_pnl)}`}>{fmtSigned(p.unrealized_pnl)} ({fmtPct(p.unrealized_pnl_pct)})</td><td className="muted">{p.strategy}</td><td className="r"><button className="btn sm" onClick={(e) => { e.stopPropagation(); api.closePosition(p.symbol).catch((err) => useStore.getState().showToast("error", (err as Error).message)); }}>Close</button></td></tr>)}</tbody></table>;
}

function OrdersTable() {
  const orders = useStore((s) => s.orders);
  const status = useStore((s) => s.status);
  const showToast = useStore((s) => s.showToast);
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [qty, setQty] = useState("10");
  const running = status?.state === "running";
  async function place() {
    const sym = (symbol || useStore.getState().selectedSymbol).trim().toUpperCase();
    const q = Number(qty);
    if (!sym || !Number.isFinite(q) || q <= 0) return showToast("error", "Enter a symbol and a positive quantity");
    try { const order = await api.submitOrder({ symbol: sym, side, qty: q }); showToast(order.status === "rejected" ? "error" : "info", `Order ${order.status}: ${side.toUpperCase()} ${q} ${sym}`); } catch (e) { showToast("error", (e as Error).message); }
  }
  return <div><div className="order-form"><div className="field"><label>Symbol</label><input type="text" placeholder={useStore.getState().selectedSymbol} value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} /></div><div className="field"><label>Side</label><select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}><option value="buy">Buy</option><option value="sell">Sell</option></select></div><div className="field"><label>Qty</label><input type="number" className="num" min={1} value={qty} onChange={(e) => setQty(e.target.value)} /></div><button className={`btn ${side === "buy" ? "success" : "danger"}`} onClick={place} disabled={!running}>{running ? `${side === "buy" ? "Buy" : "Sell"} market` : "Engine stopped"}</button></div>{orders.length === 0 ? <div className="empty-state">No orders this session</div> : <table className="grid"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th>Type</th><th className="r">Fill</th><th>Status</th><th>Strategy</th><th>Reason</th></tr></thead><tbody>{orders.map((o) => <tr key={o.id}><td className="num faint">{fmtTime(o.submitted_at)}</td><td><b>{o.symbol}</b></td><td><span className={`side-tag ${o.side}`}>{o.side}</span></td><td className="r num">{fmtQty(o.qty)}</td><td className="muted">{o.order_type}</td><td className="r num">{o.filled_avg_price?.toFixed(2) ?? "—"}</td><td><span className={`status-pill ${o.status}`}>{o.status}</span></td><td className="muted">{o.strategy}</td><td className="muted" style={{ maxWidth: 340, overflow: "hidden", textOverflow: "ellipsis" }}>{o.reason}</td></tr>)}</tbody></table>}</div>;
}

function TradesTable() {
  const trades = useStore((s) => s.trades);
  if (trades.length === 0) return <div className="empty-state">No closed trades yet</div>;
  return <table className="grid"><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Exit</th><th className="r">P&L</th><th className="r">P&L %</th><th className="r">R</th><th>Strategy</th><th>Exit reason</th></tr></thead><tbody>{trades.map((t) => <tr key={t.id}><td className="num faint">{fmtDateTime(t.exit_at)}</td><td><b>{t.symbol}</b></td><td><span className={`side-tag ${t.side}`}>{t.side}</span></td><td className="r num">{fmtQty(t.qty)}</td><td className="r num">{t.entry_price.toFixed(2)}</td><td className="r num">{t.exit_price.toFixed(2)}</td><td className={`r num ${pnlClass(t.pnl)}`}>{fmtSigned(t.pnl)}</td><td className={`r num ${pnlClass(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td><td className={`r num ${pnlClass(t.r_multiple)}`}>{t.r_multiple?.toFixed(1) ?? "—"}</td><td className="muted">{t.strategy}</td><td className="muted">{t.exit_reason}</td></tr>)}</tbody></table>;
}

export function ShadowAnalyticsTab() {
  const [analytics, setAnalytics] = useState<ShadowAnalyticsResponse | null>(null);
  const [gateAlpha, setGateAlpha] = useState<GateAlphaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { Promise.all([api.shadowLedgerAnalytics(), api.shadowLedgerGateAlpha()]).then(([a, g]) => { setAnalytics(a); setGateAlpha(g); setLoading(false); }).catch((err) => { setError(err.message ?? "Failed to fetch shadow analytics"); setLoading(false); }); }, []);
  if (loading) return <div className="empty-state">Loading shadow analytics data...</div>;
  if (error) return <div className="empty-state" style={{ color: "var(--danger)" }}>Error: {error}</div>;
  if (!analytics || !gateAlpha) return <div className="empty-state">No shadow analytics data available</div>;
  const s = analytics.summary;
  const netAlpha = (s.avoided_loss_r ?? 0) - (s.missed_profit_r ?? 0);
  const cards = [
    ["Canonical rejected", s.canonical_rejected_signals ?? (s.avoided_loss_count + s.missed_profit_count), `${s.valid_r_count ?? s.replayed_signals} canonical R outcomes`, ""],
    ["Avoided losses", `+${s.avoided_loss_r?.toFixed(2) ?? "0.00"}R`, `${s.avoided_loss_count} correct rejections`, "pos"],
    ["Missed winners", `-${s.missed_profit_r?.toFixed(2) ?? "0.00"}R`, `${s.missed_profit_count} false rejections`, "neg"],
    ["Net gate alpha", `${netAlpha >= 0 ? "+" : ""}${netAlpha.toFixed(2)}R`, "Avoided loss − missed winner", pnlClass(netAlpha)],
  ];
  return <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
    {analytics.evidence_cohort && <div className="muted" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 10, fontSize: 11 }}>Evidence cohort · <b>{analytics.evidence_cohort.canonical_r_signals}</b> canonical R · <b>{analytics.evidence_cohort.replayed_signals}</b> replayed · {analytics.evidence_cohort.total_signals} evaluated</div>}
    <div className="metric-grid">{cards.map(([label, value, hint, cls]) => <div className="metric-card" key={String(label)}><div className="k">{label}</div><div className={`v num ${cls}`}>{value}</div><div className="faint" style={{ fontSize: 10 }}>{hint}</div></div>)}</div>
    <div><div style={{ marginBottom: 8 }}><b>Gate alpha & veto attribution</b><div className="muted" style={{ fontSize: 11 }}>Where production gates add or lose R on rejected candidates.</div></div><div style={{ overflowX: "auto" }}><table className="grid"><thead><tr><th>Gate</th><th className="r">Vetoes</th><th className="r">Unique</th><th className="r">Unique α</th><th className="r">Shared α</th><th className="r">Marginal α</th><th className="r">Avoided</th><th className="r">Missed</th></tr></thead><tbody>{(Object.entries(gateAlpha) as [string, GateAlphaStats][]).map(([name, info]) => <tr key={name}><td><b>{name === "ml_gate" ? "ML Gate" : name === "meta_label_gate" ? "Meta-Label Gate" : "Trade Score Gate"}</b></td><td className="r num">{info.total_vetoes}</td><td className="r num">{info.unique_vetoes}</td><td className={`r num ${pnlClass(info.unique_veto_alpha)}`}>{fmtSigned(info.unique_veto_alpha)}R</td><td className={`r num ${pnlClass(info.shared_veto_alpha)}`}>{fmtSigned(info.shared_veto_alpha)}R</td><td className={`r num ${pnlClass(info.marginal_alpha)}`}>{fmtSigned(info.marginal_alpha)}R</td><td className="r num pos">{info.avoided_losses}</td><td className="r num neg">{info.missed_profits}</td></tr>)}</tbody></table></div></div>
    <div><div style={{ marginBottom: 8 }}><b>Exit capture</b><div className="muted" style={{ fontSize: 11 }}>Live exit efficiency from realized trades.</div></div><div className="metric-grid"><div className="metric-card"><div className="k">Winner capture</div><div className="v num">{(s.avg_winner_capture_ratio * 100).toFixed(1)}%</div><div className="faint" style={{ fontSize: 10 }}>Realized R / MFE R</div></div><div className="metric-card"><div className="k">Average give-back</div><div className="v num">{s.avg_give_back_r.toFixed(2)}R</div><div className="faint" style={{ fontSize: 10 }}>MFE R − realized R</div></div><div className="metric-card"><div className="k">Average MFE</div><div className="v num">{s.avg_mfe_r != null ? `${s.avg_mfe_r.toFixed(2)}R` : "—"}</div><div className="faint" style={{ fontSize: 10 }}>{s.mfe_coverage_pct != null ? `${s.mfe_coverage_pct}% coverage` : "Max favorable excursion"}</div></div><div className="metric-card"><div className="k">MFE before loss</div><div className="v num">{s.avg_mfe_before_loss_r.toFixed(2)}R</div><div className="faint" style={{ fontSize: 10 }}>Peak R of losing trades</div></div></div></div>
    <div><div style={{ marginBottom: 8 }}><b>Alternative exit policies</b><div className="muted" style={{ fontSize: 11 }}>Champion versus shadow exit challengers. Shadow rows have no execution authority.</div></div><div style={{ overflowX: "auto" }}><table className="grid"><thead><tr><th>Policy</th><th className="r">Trades</th><th className="r">Win %</th><th className="r">Avg return</th><th className="r">Native R</th><th className="r">Common R</th><th className="r">Coverage</th><th className="r">MFE</th><th className="r">MAE</th><th className="r">Held</th><th>Status</th></tr></thead><tbody>{analytics.alternative_exits_comparison?.map((p: AltExitStats) => { const champion = p.policy_id === "exit_production"; const name = champion ? "Production exit" : p.policy_id === "exit_bracket_tight" ? "Tight bracket" : p.policy_id === "exit_bracket_wide" ? "Wide bracket" : "ATR trailing"; return <tr key={p.policy_id}><td><b>{name}</b></td><td className="r num">{p.count}</td><td className="r num">{p.win_rate_pct}%</td><td className={`r num ${pnlClass(p.avg_simulated_return_pct)}`}>{fmtSigned(p.avg_simulated_return_pct)}%</td><td className={`r num ${pnlClass(p.avg_realized_r)}`}>{fmtSigned(p.avg_realized_r)}R</td><td className={`r num ${p.avg_common_realized_r != null ? pnlClass(p.avg_common_realized_r) : "faint"}`}>{p.avg_common_realized_r != null ? `${fmtSigned(p.avg_common_realized_r)}R` : "—"}</td><td className="r num faint">{p.common_r_coverage_pct != null ? `${p.common_r_coverage_pct}%` : "—"}</td><td className="r num">{p.avg_mfe_r != null ? `${p.avg_mfe_r.toFixed(2)}R` : "—"}</td><td className="r num faint">{p.avg_mae_r != null ? `${p.avg_mae_r.toFixed(2)}R` : "—"}</td><td className="r num faint">{p.avg_bars_held}m</td><td>{champion ? <span className="badge success">Champion</span> : <span className="badge">Shadow</span>}</td></tr>; })}</tbody></table></div></div>
  </div>;
}
