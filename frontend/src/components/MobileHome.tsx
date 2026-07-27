import { fmtMoney, fmtPct, fmtSigned, pnlClass } from "../format";
import { useStore } from "../store";

export function MobileHome({ onOpenActivity, onOpenChart }: { onOpenActivity: () => void; onOpenChart: () => void }) {
  const account = useStore((s) => s.account);
  const positions = useStore((s) => s.positions);
  const status = useStore((s) => s.status);
  const insights = useStore((s) => s.insights);
  const selectSymbol = useStore((s) => s.selectSymbol);
  const equity = account?.equity ?? 0;
  const dayPnl = account?.day_pnl ?? 0;
  const dayPnlPct = account?.day_pnl_pct ?? 0;
  const exposure = positions.reduce((sum, p) => sum + Math.abs(p.market_value ?? 0), 0);

  return <div className="mobile-page mobile-home">
    <section className="mobile-hero-card">
      <div className="mobile-eyebrow">Portfolio value</div>
      <div className="mobile-equity">{fmtMoney(equity)}</div>
      <div className={`mobile-day-pnl ${pnlClass(dayPnl)}`}>{fmtSigned(dayPnl)} <span>{fmtPct(dayPnlPct)}</span> today</div>
      <div className="mobile-hero-meta">
        <div><span>Buying power</span><strong>{fmtMoney(account?.buying_power ?? 0)}</strong></div>
        <div><span>Exposure</span><strong>{fmtMoney(exposure)}</strong></div>
      </div>
    </section>

    <section className="mobile-status-strip">
      <div><span className={`mobile-status-dot ${status?.state === "running" ? "live" : ""}`} /><div><b>{status?.state === "running" ? "Engine live" : "Engine idle"}</b><small>{status?.mode ?? "demo"} mode</small></div></div>
      <div><b>{status?.market?.is_open ? "Open" : "Closed"}</b><small>US market</small></div>
      <div><b>{positions.length}</b><small>Positions</small></div>
    </section>

    <div className="mobile-section-head"><div><span>Open positions</span><small>Live portfolio</small></div><button onClick={onOpenActivity}>View all</button></div>
    <section className="mobile-card-list">
      {positions.length === 0 && <div className="mobile-empty">No open positions</div>}
      {positions.slice(0, 4).map((p) => <button className="mobile-position-card" key={p.symbol} onClick={() => { selectSymbol(p.symbol); onOpenChart(); }}>
        <div className="mobile-asset-mark">{p.symbol.slice(0, 2)}</div>
        <div className="mobile-position-main"><b>{p.symbol}</b><span>{p.side.toUpperCase()} · {Math.abs(p.qty)} shares</span></div>
        <div className="mobile-position-value"><b>{fmtMoney(p.market_value)}</b><span className={pnlClass(p.unrealized_pnl)}>{fmtSigned(p.unrealized_pnl)} · {fmtPct(p.unrealized_pnl_pct)}</span></div>
      </button>)}
    </section>

    <div className="mobile-section-head"><div><span>Intelligence</span><small>Latest bot activity</small></div></div>
    <section className="mobile-intelligence-card">
      {insights.length === 0 ? <div className="mobile-empty">Insights appear when the engine runs</div> : insights.slice(0, 3).map((item) => <div className="mobile-intel-row" key={item.id}><span className={`mobile-intel-dot ${item.severity}`} /><div><b>{item.title}</b><p>{item.detail}</p></div></div>)}
    </section>
  </div>;
}
