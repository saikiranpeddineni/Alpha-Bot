import { useState } from "react";
import { api } from "../api";
import { fmtDateTime, fmtMoney, fmtPct, fmtQty, fmtSigned, fmtTime, pnlClass } from "../format";
import { useStore } from "../store";
import { BottomTabs } from "./BottomTabs";

type ActivityTab = "positions" | "orders" | "trades" | "research";

export function MobileActivity() {
  const [tab, setTab] = useState<ActivityTab>("positions");
  const positions = useStore((s) => s.positions);
  const orders = useStore((s) => s.orders);
  const trades = useStore((s) => s.trades);
  return <div className="mobile-page mobile-activity">
    <div className="mobile-page-title"><span>Activity</span><small>Execution, portfolio and research</small></div>
    <div className="mobile-segmented">
      <button className={tab === "positions" ? "active" : ""} onClick={() => setTab("positions")}>Positions <i>{positions.length}</i></button>
      <button className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>Orders <i>{orders.length}</i></button>
      <button className={tab === "trades" ? "active" : ""} onClick={() => setTab("trades")}>Trades <i>{trades.length}</i></button>
      <button className={tab === "research" ? "active" : ""} onClick={() => setTab("research")}>Research</button>
    </div>
    {tab === "positions" && <MobilePositions />}
    {tab === "orders" && <MobileOrders />}
    {tab === "trades" && <MobileTrades />}
    {tab === "research" && <div className="mobile-research-shell"><BottomTabs /></div>}
  </div>;
}

function MobilePositions() {
  const positions = useStore((s) => s.positions);
  const selectSymbol = useStore((s) => s.selectSymbol);
  const showToast = useStore((s) => s.showToast);
  if (!positions.length) return <MobileEmpty text="No open positions" />;
  return <div className="mobile-activity-list">{positions.map((p) => <article className="mobile-activity-card" key={p.symbol} onClick={() => selectSymbol(p.symbol)}>
    <div className="mobile-activity-top"><div className="mobile-symbol"><span>{p.symbol.slice(0,2)}</span><div><b>{p.symbol}</b><small>{p.strategy || "Strategy"}</small></div></div><div className="mobile-activity-pnl"><b className={pnlClass(p.unrealized_pnl)}>{fmtSigned(p.unrealized_pnl)}</b><small className={pnlClass(p.unrealized_pnl_pct)}>{fmtPct(p.unrealized_pnl_pct)}</small></div></div>
    <div className="mobile-metric-row"><Metric label="Side" value={p.side.toUpperCase()} /><Metric label="Qty" value={fmtQty(Math.abs(p.qty))} /><Metric label="Value" value={fmtMoney(p.market_value)} /></div>
    <div className="mobile-price-row"><Metric label="Entry" value={`$${p.avg_entry_price.toFixed(2)}`} /><Metric label="Last" value={`$${p.market_price.toFixed(2)}`} /><Metric label="Stop" value={p.stop_loss != null ? `$${p.stop_loss.toFixed(2)}` : "—"} /><Metric label="Target" value={p.take_profit != null ? `$${p.take_profit.toFixed(2)}` : "—"} /></div>
    <button className="mobile-card-action danger" onClick={(e) => { e.stopPropagation(); api.closePosition(p.symbol).then(() => showToast("info", `${p.symbol} close submitted`)).catch((err) => showToast("error", (err as Error).message)); }}>Close position</button>
  </article>)}</div>;
}

function MobileOrders() {
  const orders = useStore((s) => s.orders);
  if (!orders.length) return <MobileEmpty text="No orders this session" />;
  return <div className="mobile-activity-list">{orders.map((o) => <article className="mobile-activity-card" key={o.id}>
    <div className="mobile-activity-top"><div className="mobile-symbol"><span>{o.symbol.slice(0,2)}</span><div><b>{o.symbol}</b><small>{fmtTime(o.submitted_at)} · {o.order_type}</small></div></div><div className="mobile-order-state"><b className={o.side === "buy" ? "pos" : "neg"}>{o.side.toUpperCase()}</b><small>{o.status}</small></div></div>
    <div className="mobile-metric-row"><Metric label="Quantity" value={fmtQty(o.qty)} /><Metric label="Filled" value={fmtQty(o.filled_qty)} /><Metric label="Avg fill" value={o.filled_avg_price != null ? `$${o.filled_avg_price.toFixed(2)}` : "—"} /></div>
    {(o.strategy || o.reason) && <div className="mobile-card-note"><b>{o.strategy || "Order"}</b><span>{o.reason || "No execution note"}</span></div>}
  </article>)}</div>;
}

function MobileTrades() {
  const trades = useStore((s) => s.trades);
  if (!trades.length) return <MobileEmpty text="No closed trades yet" />;
  return <div className="mobile-activity-list">{trades.map((t) => <article className="mobile-activity-card" key={t.id}>
    <div className="mobile-activity-top"><div className="mobile-symbol"><span>{t.symbol.slice(0,2)}</span><div><b>{t.symbol}</b><small>{fmtDateTime(t.exit_at)}</small></div></div><div className="mobile-activity-pnl"><b className={pnlClass(t.pnl)}>{fmtSigned(t.pnl)}</b><small className={pnlClass(t.pnl_pct)}>{fmtPct(t.pnl_pct)}</small></div></div>
    <div className="mobile-metric-row"><Metric label="Side" value={t.side.toUpperCase()} /><Metric label="Qty" value={fmtQty(t.qty)} /><Metric label="R multiple" value={t.r_multiple != null ? `${t.r_multiple.toFixed(1)}R` : "—"} /></div>
    <div className="mobile-price-row"><Metric label="Entry" value={`$${t.entry_price.toFixed(2)}`} /><Metric label="Exit" value={`$${t.exit_price.toFixed(2)}`} /></div>
    <div className="mobile-card-note"><b>{t.strategy}</b><span>{t.exit_reason || "Trade closed"}</span></div>
  </article>)}</div>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div><span>{label}</span><b>{value}</b></div>; }
function MobileEmpty({ text }: { text: string }) { return <div className="mobile-empty-card"><span>—</span><b>{text}</b><small>Live data will appear here automatically.</small></div>; }
