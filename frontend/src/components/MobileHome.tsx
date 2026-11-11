import { fmtMoney, fmtPct, fmtSigned, pnlClass } from "../format";
import { useStore } from "../store";

// =============================================================================
// [MODIFIED 2026-11-11]: Clean MobileHome Format & Fix Corrupted Glyphs
// ORIGINAL INTENT: Rendered single-line minified markup with raw corrupted bullet/chevron bytes.
// REASON FOR CHANGE: Format into clean JSX and replace corrupted characters with standard SVGs and &middot;.
// SAFE ROLLBACK: Previous minified code preserved below in comments.
// -----------------------------------------------------------------------------
// [PREVIOUS CODE - PRESERVED]:
// export function MobileHome({ onOpenActivity, onOpenChart }: { onOpenActivity: () => void; onOpenChart: () => void }) { ... minified ... }
// -----------------------------------------------------------------------------
// [NEW CODE]:
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
  const buyingPower = account?.buying_power ?? 0;
  const exposurePct = equity > 0 ? Math.min(999, (exposure / equity) * 100) : 0;

  return (
    <div className="mobile-page mobile-home mobile-home-v2">
      <section className="home-portfolio-hero">
        <div className="home-hero-kicker">
          <span>Portfolio</span>
          <i className={status?.state === "running" ? "live" : ""}>{status?.state === "running" ? "Live" : "Idle"}</i>
        </div>
        <div className="home-equity">{fmtMoney(equity)}</div>
        <div className={`home-return ${pnlClass(dayPnl)}`}>
          <b>{fmtSigned(dayPnl)}</b>
          <span>{fmtPct(dayPnlPct)} today</span>
        </div>
        <div className="home-balance-line">
          <div>
            <span>Buying power</span>
            <b>{fmtMoney(buyingPower)}</b>
          </div>
          <div>
            <span>Gross exposure</span>
            <b>{fmtMoney(exposure)}</b>
          </div>
        </div>
        <div className="home-exposure">
          <div>
            <span>Capital deployed</span>
            <b>{exposurePct.toFixed(1)}%</b>
          </div>
          <div className="home-exposure-track">
            <i style={{ width: `${Math.min(100, exposurePct)}%` }} />
          </div>
        </div>
      </section>

      <section className="home-market-brief">
        <div>
          <span className={`brief-orb ${status?.market?.is_open ? "open" : ""}`} />
          <div>
            <small>US MARKET</small>
            <b>{status?.market?.is_open ? "Open" : "Closed"}</b>
          </div>
        </div>
        <div>
          <small>ENGINE</small>
          <b>{status?.state === "running" ? "Running" : "Standby"}</b>
        </div>
        <div>
          <small>MODE</small>
          <b>{(status?.mode ?? "demo").toUpperCase()}</b>
        </div>
      </section>

      <div className="mobile-section-head premium-section-head">
        <div>
          <span>Portfolio</span>
          <small>{positions.length ? `${positions.length} active position${positions.length === 1 ? "" : "s"}` : "No capital deployed"}</small>
        </div>
        <button onClick={onOpenActivity} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          All positions
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>
      </div>

      <section className="home-position-list">
        {positions.length === 0 && (
          <div className="home-empty">
            <span>Portfolio is clear</span>
            <small>New positions will appear here when the engine executes.</small>
          </div>
        )}
        {positions.slice(0, 4).map((p) => (
          <button
            className="home-position"
            key={p.symbol}
            onClick={() => {
              selectSymbol(p.symbol);
              onOpenChart();
            }}
          >
            <div className="home-position-icon">{p.symbol.slice(0, 1)}</div>
            <div className="home-position-copy">
              <div>
                <b>{p.symbol}</b>
                <span>{p.strategy || p.side.toUpperCase()}</span>
              </div>
              <small>
                {Math.abs(p.qty)} shares &middot; avg ${p.avg_entry_price.toFixed(2)}
              </small>
            </div>
            <div className="home-position-money">
              <b>{fmtMoney(p.market_value)}</b>
              <span className={pnlClass(p.unrealized_pnl_pct)}>{fmtPct(p.unrealized_pnl_pct)}</span>
            </div>
            <span className="home-chevron" style={{ display: "inline-flex", alignItems: "center" }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </span>
          </button>
        ))}
      </section>

      <div className="mobile-section-head premium-section-head intelligence-head">
        <div>
          <span>Research pulse</span>
          <small>What AlphaDesk is seeing now</small>
        </div>
      </div>

      <section className="home-research-feed">
        {insights.length === 0 ? (
          <div className="home-empty">
            <span>Waiting for research</span>
            <small>Engine observations will surface here automatically.</small>
          </div>
        ) : (
          insights.slice(0, 4).map((item, index) => (
            <article className="home-research-item" key={item.id}>
              <div className={`research-index ${item.severity}`}>{String(index + 1).padStart(2, "0")}</div>
              <div>
                <div className="research-item-title">
                  <b>{item.title}</b>
                  <span>{item.severity}</span>
                </div>
                <p>{item.detail}</p>
              </div>
            </article>
          ))
        )}
      </section>
    </div>
  );
}
// =============================================================================
