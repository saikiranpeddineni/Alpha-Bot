import { useState, useEffect } from "react";
import { api } from "../api";
import { fmtDateTime, fmtMoney, fmtPct, fmtQty, fmtSigned, fmtTime, pnlClass } from "../format";
import { useStore } from "../store";
import { ShadowAnalyticsResponse, GateAlphaResponse, GateAlphaStats, AltExitStats } from "../types";

import { TickerFlowVisualizer } from "./TickerFlowVisualizer";

type Tab = "positions" | "orders" | "trades" | "logs" | "shadow" | "flow";

export function BottomTabs() {
  const [tab, setTab] = useState<Tab>("positions");
  const positions = useStore((s) => s.positions);
  const trades = useStore((s) => s.trades);
  const logs = useStore((s) => s.logs);

  return (
    <div className="panel">
      <div className="tabs">
        <button className={tab === "positions" ? "active" : ""} onClick={() => setTab("positions")}>
          Positions {positions.length > 0 && `(${positions.length})`}
        </button>
        <button className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>
          Orders
        </button>
        <button className={tab === "trades" ? "active" : ""} onClick={() => setTab("trades")}>
          Trades {trades.length > 0 && `(${trades.length})`}
        </button>
        <button className={tab === "flow" ? "active" : ""} onClick={() => setTab("flow")}>
          ⚡ Pipeline Flow
        </button>
        <button className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}>
          Logs
        </button>
        <button className={tab === "shadow" ? "active" : ""} onClick={() => setTab("shadow")}>
          Shadow Analytics
        </button>
        <span className="spacer" />
        {tab === "positions" && positions.length > 0 && <FlattenButton />}
      </div>
      <div className="panel-body">
        {tab === "positions" && <PositionsTable />}
        {tab === "orders" && <OrdersTable />}
        {tab === "trades" && <TradesTable />}
        {tab === "flow" && <TickerFlowVisualizer />}
        {tab === "shadow" && <ShadowAnalyticsTab />}
        {tab === "logs" && (
          <div>
            {logs.length === 0 && <div className="empty-state">No log entries yet</div>}
            {logs.map((l, i) => (
              <div className="log-line" key={`${l.ts}-${i}`}>
                <span className="faint num">{fmtTime(l.ts)}</span>
                <span className="src">{l.source}</span>
                <span className={`msg lvl-${l.level}`}>{l.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function FlattenButton() {
  const showToast = useStore((s) => s.showToast);
  return (
    <div className="tab-actions">
      <button
        className="btn sm danger"
        onClick={() =>
          api.flatten().then(
            () => showToast("info", "All positions closed"),
            (e) => showToast("error", (e as Error).message),
          )
        }
      >
        Flatten all
      </button>
    </div>
  );
}

function PositionsTable() {
  const positions = useStore((s) => s.positions);
  const selectSymbol = useStore((s) => s.selectSymbol);

  if (positions.length === 0) {
    return <div className="empty-state">No open positions</div>;
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th>
          <th className="r">Last</th><th className="r">Stop</th><th className="r">Target</th>
          <th className="r">Value</th><th className="r">Unrlzd P&L</th><th>Strategy</th><th></th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => (
          <tr key={p.symbol} onClick={() => selectSymbol(p.symbol)} style={{ cursor: "pointer" }}>
            <td><b>{p.symbol}</b></td>
            <td><span className={`side-tag ${p.side}`}>{p.side}</span></td>
            <td className="r num">{fmtQty(Math.abs(p.qty))}</td>
            <td className="r num">{p.avg_entry_price.toFixed(2)}</td>
            <td className="r num">{p.market_price.toFixed(2)}</td>
            <td className="r num faint">{p.stop_loss?.toFixed(2) ?? "—"}</td>
            <td className="r num faint">{p.take_profit?.toFixed(2) ?? "—"}</td>
            <td className="r num">{fmtMoney(p.market_value)}</td>
            <td className={`r num ${pnlClass(p.unrealized_pnl)}`}>
              {fmtSigned(p.unrealized_pnl)} ({fmtPct(p.unrealized_pnl_pct)})
            </td>
            <td className="muted">{p.strategy}</td>
            <td className="r">
              <button
                className="btn sm"
                onClick={(e) => {
                  e.stopPropagation();
                  api.closePosition(p.symbol).catch((err) =>
                    useStore.getState().showToast("error", (err as Error).message),
                  );
                }}
              >
                Close
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
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
    if (!sym || !Number.isFinite(q) || q <= 0) {
      showToast("error", "Enter a symbol and a positive quantity");
      return;
    }
    try {
      const order = await api.submitOrder({ symbol: sym, side, qty: q });
      showToast(
        order.status === "rejected" ? "error" : "info",
        `Order ${order.status}: ${side.toUpperCase()} ${q} ${sym}`,
      );
    } catch (e) {
      showToast("error", (e as Error).message);
    }
  }

  return (
    <div>
      <div className="order-form">
        <div className="field">
          <label>Symbol</label>
          <input
            type="text"
            placeholder={useStore.getState().selectedSymbol}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          />
        </div>
        <div className="field">
          <label>Side</label>
          <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </div>
        <div className="field">
          <label>Qty</label>
          <input type="number" className="num" min={1} value={qty} onChange={(e) => setQty(e.target.value)} />
        </div>
        <button className={`btn ${side === "buy" ? "success" : "danger"}`} onClick={place} disabled={!running}>
          {running ? `${side === "buy" ? "Buy" : "Sell"} market` : "Engine stopped"}
        </button>
      </div>
      {orders.length === 0 ? (
        <div className="empty-state">No orders this session</div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Time</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th>Type</th>
              <th className="r">Fill</th><th>Status</th><th>Strategy</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td className="num faint">{fmtTime(o.submitted_at)}</td>
                <td><b>{o.symbol}</b></td>
                <td><span className={`side-tag ${o.side}`}>{o.side}</span></td>
                <td className="r num">{fmtQty(o.qty)}</td>
                <td className="muted">{o.order_type}</td>
                <td className="r num">{o.filled_avg_price?.toFixed(2) ?? "—"}</td>
                <td><span className={`status-pill ${o.status}`}>{o.status}</span></td>
                <td className="muted">{o.strategy}</td>
                <td className="muted" style={{ maxWidth: 340, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {o.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function TradesTable() {
  const trades = useStore((s) => s.trades);
  if (trades.length === 0) {
    return <div className="empty-state">No closed trades yet</div>;
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Closed</th><th>Symbol</th><th>Side</th><th className="r">Qty</th>
          <th className="r">Entry</th><th className="r">Exit</th><th className="r">P&L</th>
          <th className="r">P&L %</th><th className="r">R</th><th>Strategy</th><th>Exit reason</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td className="num faint">{fmtDateTime(t.exit_at)}</td>
            <td><b>{t.symbol}</b></td>
            <td><span className={`side-tag ${t.side}`}>{t.side}</span></td>
            <td className="r num">{fmtQty(t.qty)}</td>
            <td className="r num">{t.entry_price.toFixed(2)}</td>
            <td className="r num">{t.exit_price.toFixed(2)}</td>
            <td className={`r num ${pnlClass(t.pnl)}`}>{fmtSigned(t.pnl)}</td>
            <td className={`r num ${pnlClass(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
            <td className={`r num ${pnlClass(t.r_multiple)}`}>{t.r_multiple?.toFixed(1) ?? "—"}</td>
            <td className="muted">{t.strategy}</td>
            <td className="muted">{t.exit_reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ShadowAnalyticsTab() {
  const [analytics, setAnalytics] = useState<ShadowAnalyticsResponse | null>(null);
  const [gateAlpha, setGateAlpha] = useState<GateAlphaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.shadowLedgerAnalytics(), api.shadowLedgerGateAlpha()])
      .then(([a, g]) => {
        setAnalytics(a);
        setGateAlpha(g);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message ?? "Failed to fetch shadow analytics");
        setLoading(false);
      });
  }, []);

  if (loading) {
    return <div className="empty-state">Loading shadow analytics data...</div>;
  }

  if (error) {
    return <div className="empty-state error" style={{ color: "var(--danger)" }}>Error: {error}</div>;
  }

  if (!analytics || !gateAlpha) {
    return <div className="empty-state">No shadow analytics data available</div>;
  }

  const s = analytics.summary;
  const netAlpha = (s.avoided_loss_r ?? 0) - (s.missed_profit_r ?? 0);

  return (
    <div className="shadow-analytics" style={{ display: "flex", flexDirection: "column", gap: "20px", padding: "10px 0" }}>
      
      {analytics.evidence_cohort && (
        <div style={{ fontSize: "12px", color: "var(--faint)", background: "var(--panel-2)", padding: "8px 12px", borderRadius: "6px", border: "1px solid var(--border)" }}>
          📊 Evidence Cohort: <b>{analytics.evidence_cohort.canonical_r_signals}</b> canonical R outcomes from <b>{analytics.evidence_cohort.replayed_signals}</b> replayed signals ({analytics.evidence_cohort.total_signals} total evaluated)
        </div>
      )}

      {/* 1. KPI Cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "12px" }}>
        <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "15px", borderRadius: "8px", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: "12px", color: "var(--faint)", textTransform: "uppercase" }}>Canonical Rejected Signals</div>
          <div style={{ fontSize: "24px", fontWeight: "bold", margin: "5px 0" }} className="num">{s.canonical_rejected_signals ?? (s.avoided_loss_count + s.missed_profit_count)}</div>
          <div style={{ fontSize: "11px", color: "var(--faint)" }}>From {s.valid_r_count ?? s.replayed_signals} canonical R outcomes ({s.replayed_signals} replayed)</div>
        </div>
        <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "15px", borderRadius: "8px", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: "12px", color: "var(--faint)", textTransform: "uppercase" }}>Avoided Losses R</div>
          <div style={{ fontSize: "24px", fontWeight: "bold", margin: "5px 0", color: "var(--success)" }} className="num">+{s.avoided_loss_r?.toFixed(2) ?? "0.00"}R</div>
          <div style={{ fontSize: "11px", color: "var(--faint)" }}>From {s.avoided_loss_count} correct rejections</div>
        </div>
        <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "15px", borderRadius: "8px", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: "12px", color: "var(--faint)", textTransform: "uppercase" }}>Missed Winners R</div>
          <div style={{ fontSize: "24px", fontWeight: "bold", margin: "5px 0", color: "var(--danger)" }} className="num">-{s.missed_profit_r?.toFixed(2) ?? "0.00"}R</div>
          <div style={{ fontSize: "11px", color: "var(--faint)" }}>From {s.missed_profit_count} false rejections</div>
        </div>
        <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "15px", borderRadius: "8px", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: "12px", color: "var(--faint)", textTransform: "uppercase" }}>Net Gate Alpha R</div>
          <div style={{ fontSize: "24px", fontWeight: "bold", margin: "5px 0" }} className={`num ${pnlClass(netAlpha)}`}>
            {netAlpha >= 0 ? "+" : ""}{netAlpha.toFixed(2)}R
          </div>
          <div style={{ fontSize: "11px", color: "var(--faint)" }}>Avoided Loss - Missed Winner</div>
        </div>
      </div>

      {/* 2. Veto Attribution Table */}
      <div>
        <h4 style={{ margin: "0 0 10px", color: "var(--accent)" }}>Gate Alpha & Veto Attribution</h4>
        <table className="grid">
          <thead>
            <tr>
              <th>Gate</th>
              <th className="r">Total Vetoes</th>
              <th className="r">Unique Vetoes</th>
              <th className="r">Unique Alpha (R)</th>
              <th className="r">Shared Alpha (R)</th>
              <th className="r">Marginal Alpha (R)</th>
              <th className="r">Avoided Losses</th>
              <th className="r">Missed Winners</th>
            </tr>
          </thead>
          <tbody>
            {(Object.entries(gateAlpha) as [string, GateAlphaStats][]).map(([gateName, info]) => {
              const friendlyName = gateName === "ml_gate" ? "ML Alpha Gate" : gateName === "meta_label_gate" ? "Meta-Label Gate" : "Trade Score Gate";
              return (
                <tr key={gateName}>
                  <td><b>{friendlyName}</b></td>
                  <td className="r num">{info.total_vetoes}</td>
                  <td className="r num">{info.unique_vetoes}</td>
                  <td className={`r num ${pnlClass(info.unique_veto_alpha)}`}>
                    {info.unique_veto_alpha >= 0 ? "+" : ""}{info.unique_veto_alpha.toFixed(2)}R
                  </td>
                  <td className={`r num ${pnlClass(info.shared_veto_alpha)}`}>
                    {info.shared_veto_alpha >= 0 ? "+" : ""}{info.shared_veto_alpha.toFixed(2)}R
                  </td>
                  <td className={`r num ${pnlClass(info.marginal_alpha)}`}>
                    {info.marginal_alpha >= 0 ? "+" : ""}{info.marginal_alpha.toFixed(2)}R
                  </td>
                  <td className="r num text-success">{info.avoided_losses}</td>
                  <td className="r num text-danger">{info.missed_profits}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* 3. Exit Telemetry Efficiency */}
      <div>
        <h4 style={{ margin: "0 0 10px", color: "var(--accent)" }}>Exit Capture Efficiency (Realized Trades)</h4>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "12px" }}>
          <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "12px", borderRadius: "6px", border: "1px solid var(--border)" }}>
            <div style={{ fontSize: "11px", color: "var(--faint)", textTransform: "uppercase" }}>Winner Capture Ratio</div>
            <div style={{ fontSize: "20px", fontWeight: "bold", margin: "4px 0", color: "var(--accent)" }} className="num">
              {(s.avg_winner_capture_ratio * 100).toFixed(1)}%
            </div>
            <div style={{ fontSize: "10px", color: "var(--faint)" }}>Realized R / MFE R</div>
          </div>
          <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "12px", borderRadius: "6px", border: "1px solid var(--border)" }}>
            <div style={{ fontSize: "11px", color: "var(--faint)", textTransform: "uppercase" }}>Average Give-Back R</div>
            <div style={{ fontSize: "20px", fontWeight: "bold", margin: "4px 0", color: "var(--warning)" }} className="num">
              {s.avg_give_back_r.toFixed(2)}R
            </div>
            <div style={{ fontSize: "10px", color: "var(--faint)" }}>MFE R - Realized R</div>
          </div>
          <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "12px", borderRadius: "6px", border: "1px solid var(--border)" }}>
            <div style={{ fontSize: "11px", color: "var(--faint)", textTransform: "uppercase" }}>Average MFE R</div>
            <div style={{ fontSize: "20px", fontWeight: "bold", margin: "4px 0" }} className="num">
              {s.avg_mfe_r != null ? `${s.avg_mfe_r.toFixed(2)}R` : "—"}
            </div>
            <div style={{ fontSize: "10px", color: "var(--faint)" }}>
              {s.mfe_coverage_pct != null ? `${s.mfe_coverage_pct}% coverage (${s.mfe_valid_count}/${s.valid_r_count ?? s.replayed_signals})` : "Max Favorable Excursion"}
            </div>
          </div>
          <div className="card-kpi" style={{ background: "var(--panel-2)", padding: "12px", borderRadius: "6px", border: "1px solid var(--border)" }}>
            <div style={{ fontSize: "11px", color: "var(--faint)", textTransform: "uppercase" }}>MFE-Before-Loss R</div>
            <div style={{ fontSize: "20px", fontWeight: "bold", margin: "4px 0", color: "var(--danger)" }} className="num">
              {s.avg_mfe_before_loss_r.toFixed(2)}R
            </div>
            <div style={{ fontSize: "10px", color: "var(--faint)" }}>Peak R of losing trades</div>
          </div>
        </div>
      </div>

      {/* 4. Alternative Exits Comparison */}
      <div>
        <h4 style={{ margin: "0 0 10px", color: "var(--accent)" }}>Alternative Exit Policy Evaluation</h4>
        <table className="grid">
          <thead>
            <tr>
              <th>Policy Name</th>
              <th className="r">Simulated Trades</th>
              <th className="r">Win Rate %</th>
              <th className="r">Avg Return %</th>
              <th className="r">Native Realized R</th>
              <th className="r">Common Realized R</th>
              <th className="r">Common-R Coverage</th>
              <th className="r">Avg MFE R</th>
              <th className="r">Avg MAE R</th>
              <th className="r">Avg Bars Held</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {analytics.alternative_exits_comparison?.map((p: AltExitStats) => {
              const isProduction = p.policy_id === "exit_production";
              const friendlyName = p.policy_id === "exit_production"
                ? "Champion Production Exit"
                : p.policy_id === "exit_bracket_tight"
                ? "Tight Bracket [Shadow]"
                : p.policy_id === "exit_bracket_wide"
                ? "Wide Bracket [Shadow]"
                : "ATR Trailing [Shadow]";
              
              return (
                <tr key={p.policy_id} style={{ background: isProduction ? "rgba(var(--accent-rgb), 0.08)" : undefined }}>
                  <td>
                    <b>{friendlyName}</b>
                  </td>
                  <td className="r num">{p.count}</td>
                  <td className="r num">{p.win_rate_pct}%</td>
                  <td className={`r num ${pnlClass(p.avg_simulated_return_pct)}`}>
                    {fmtSigned(p.avg_simulated_return_pct)}%
                  </td>
                  <td className={`r num ${pnlClass(p.avg_realized_r)}`}>
                    {p.avg_realized_r >= 0 ? "+" : ""}{p.avg_realized_r.toFixed(2)}R
                  </td>
                  <td className={`r num ${p.avg_common_realized_r != null ? pnlClass(p.avg_common_realized_r) : "faint"}`}>
                    {p.avg_common_realized_r != null ? `${p.avg_common_realized_r >= 0 ? "+" : ""}${p.avg_common_realized_r.toFixed(2)}R` : "—"}
                  </td>
                  <td className="r num faint">
                    {p.common_r_coverage_pct != null ? `${p.common_r_coverage_pct}% (${p.common_r_count ?? 0}/${p.count})` : "—"}
                  </td>
                  <td className="r num">{p.avg_mfe_r != null ? `${p.avg_mfe_r.toFixed(2)}R` : "—"}</td>
                  <td className="r num faint">{p.avg_mae_r != null ? `${p.avg_mae_r.toFixed(2)}R` : "—"}</td>
                  <td className="r num faint">{p.avg_bars_held}m</td>
                  <td>
                    {isProduction ? (
                      <span className="badge success" style={{ textTransform: "uppercase", fontSize: "10px" }}>Champion</span>
                    ) : (
                      <span className="badge" style={{ background: "var(--border)", color: "var(--faint)", textTransform: "uppercase", fontSize: "9px" }}>Shadow / Research</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

    </div>
  );
}
