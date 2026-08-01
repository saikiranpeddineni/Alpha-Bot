import { useEffect, useMemo, useState } from "react";
import { useStore } from "../store";

type Tab = "flow" | "timeline" | "trades" | "manifest";
type ReplayEvent = { timestamp?: string; event_type?: string; symbol?: string; details?: Record<string, unknown> };

interface ReplayModalProps { onClose: () => void; }

const STAGES = [
  ["SCANNER_CANDIDATE", "Scanner", "Universe → candidates"],
  ["STRATEGY_SIGNAL", "Strategy", "Production strategy signal"],
  ["ML_DECISION", "AI Gates", "ML → meta-label → score"],
  ["SPAWN_DECISION", "Spawn", "Shadow opportunity-cost"],
  ["RISK_DECISION", "Risk", "Portfolio + risk approval"],
  ["ORDER_SUBMITTED", "Broker", "Order submitted"],
  ["FILL_EXECUTED", "Fill", "Deterministic execution"],
  ["TRADE_CLOSED", "P&L", "Closed trade outcome"],
] as const;

function shortHash(value?: string) { return value ? `${value.slice(0, 10)}…${value.slice(-8)}` : "—"; }
function fmtTs(value?: string) { if (!value) return "—"; const d = new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleString(); }
function eventTone(type?: string) {
  if (type === "FILL_EXECUTED" || type === "TRADE_CLOSED") return "good";
  if (type === "RISK_DECISION" || type === "SPAWN_DECISION") return "warn";
  return "info";
}

export function ReplayModal({ onClose }: ReplayModalProps) {
  const [startDate, setStartDate] = useState("2026-07-24");
  const [endDate, setEndDate] = useState("2026-07-24");
  const [fillModel, setFillModel] = useState("ohlc_touch");
  const [slippageBps, setSlippageBps] = useState("0");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replayData, setReplayData] = useState<any>(null);
  const [activeTab, setActiveTab] = useState<Tab>("flow");
  const [eventFilter, setEventFilter] = useState("ALL");
  const [query, setQuery] = useState("");
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([]);
  const [history, setHistory] = useState<any[]>([]);
  const watchlist = useStore((s) => s.status?.watchlist) || [];

  useEffect(() => {
    fetch("/api/replay").then(r => r.json()).then(setHistory).catch(console.error);
  }, []);

  async function refreshHistory() {
    fetch("/api/replay").then(r => r.json()).then(setHistory).catch(console.error);
  }

  async function handleRunReplay() {
    if (!startDate || !endDate || startDate > endDate) { setError("Choose a valid replay date range."); return; }
    setLoading(true); setError(null); setReplayData(null); setActiveTab("flow");
    try {
      const res = await fetch("/api/replay/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        start_date: startDate, end_date: endDate, fill_model: fillModel, slippage_bps: Number(slippageBps) || 0, async_mode: false,
        symbols: selectedSymbols.length > 0 ? selectedSymbols : undefined,
      }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || data?.message || `Replay failed (${res.status})`);
      setReplayData(data);
      refreshHistory();
    } catch (err) { setError(err instanceof Error ? err.message : "Replay execution failed"); }
    finally { setLoading(false); }
  }

  async function handleLoadReplay(id: string) {
    setLoading(true); setError(null); setReplayData(null); setActiveTab("flow");
    try {
      const sessionRes = await fetch(`/api/replay/${id}`);
      if (!sessionRes.ok) throw new Error("Failed to load session details");
      const session = await sessionRes.json();
      
      const ledgerRes = await fetch(`/api/replay/${id}/ledger`);
      if (!ledgerRes.ok) throw new Error("Failed to load ledger data");
      const ledger = await ledgerRes.json();
      
      const manifestRes = await fetch(`/api/replay/${id}/manifest`);
      const manifest = await manifestRes.json().catch(() => ({}));
      
      setReplayData({ session: session.summary || session, ledger, manifest });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Load failed");
    } finally {
      setLoading(false);
    }
  }

  const ledger = replayData?.ledger || {};
  const timeline: ReplayEvent[] = ledger.timeline || [];
  const trades: ReplayEvent[] = ledger.trades || [];
  const manifest = replayData?.manifest || {};
  const session = replayData?.session || {};
  const stageCounts = useMemo(() => timeline.reduce((acc: Record<string, number>, e) => { if (e.event_type) acc[e.event_type] = (acc[e.event_type] || 0) + 1; return acc; }, {}), [timeline]);
  const filtered = useMemo(() => timeline.filter((e) => (eventFilter === "ALL" || e.event_type === eventFilter) && (!query || `${e.symbol || ""} ${e.event_type || ""} ${JSON.stringify(e.details || {})}`.toLowerCase().includes(query.toLowerCase()))), [timeline, eventFilter, query]);

  return <div className="replay-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <section className="replay-cockpit" role="dialog" aria-modal="true" aria-label="Production Replay Command Center">
      <header className="replay-head">
        <div className="replay-title-wrap"><div className="replay-mark">R</div><div><div className="replay-eyebrow">ALPHADESK / FORENSICS</div><h2>Production Replay Command Center</h2><p>Run the real production decision path against immutable historical market data.</p></div></div>
        <div className="replay-head-actions">{session.replay_id && <span className="replay-id mono">{session.replay_id}</span>}<button className="replay-close" onClick={onClose} aria-label="Close replay">×</button></div>
      </header>

      <div className="replay-layout">
        <aside className="replay-sidebar">
          <div className="replay-sidebar-title">Replay History</div>
          <div className="replay-sidebar-list">
            {history.length === 0 ? <div className="replay-no-rows">No past replays.</div> : null}
            {history.map((h, i) => (
              <div className="replay-history-item" key={h.replay_id || i} onClick={() => handleLoadReplay(h.replay_id)}>
                <strong>{h.start_date} to {h.end_date}</strong>
                <small className="mono">{h.replay_id}</small>
                {h.status === "COMPLETED" && h.summary?.final_equity && (
                  <small style={{ color: "var(--green)", marginTop: "4px" }}>
                    Final Equity: ${Number(h.summary.final_equity).toLocaleString(undefined, { maximumFractionDigits: 2 })}
                  </small>
                )}
                {h.status === "FAILED" && <small style={{ color: "var(--red)" }}>Failed</small>}
              </div>
            ))}
          </div>
        </aside>

        <main className="replay-main">
          <div className="replay-controls">
            <label><span>From</span><input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} /></label>
            <label><span>To</span><input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} /></label>
            <label><span>Fill model</span><select value={fillModel} onChange={(e) => setFillModel(e.target.value)}><option value="ohlc_touch">OHLC touch</option><option value="next_open">Next-bar open</option><option value="immediate">Immediate</option></select></label>
            <label><span>Slippage (bps)</span><input className="num" type="number" min="0" max="100" step="0.1" value={slippageBps} onChange={(e) => setSlippageBps(e.target.value)} /></label>
            <label><span>Symbols (All if none)</span>
              <select multiple size={3} value={selectedSymbols} onChange={(e) => {
                const vals = Array.from(e.target.selectedOptions, option => option.value);
                setSelectedSymbols(vals);
              }}>
                {watchlist.map(sym => <option key={sym} value={sym}>{sym}</option>)}
              </select>
            </label>
            <div className="replay-run-wrap"><button className="btn primary replay-run" onClick={handleRunReplay} disabled={loading}>{loading ? <><span className="replay-spinner" /> Replaying production…</> : "▶ Run new replay"}</button></div>
          </div>

          {error && <div className="replay-alert"><strong>Error</strong><span>{error}</span></div>}

          <div className="replay-body">
            {loading && <div className="replay-running"><div className="replay-running-line"><span className="replay-live-dot" /><span>Production path executing</span></div><div className="replay-running-flow">Scanner <i /> Strategy <i /> ML + Meta <i /> Spawn <i /> Risk <i /> Broker <i /> Fill <i /> Exit</div><p>Fast single-pass deterministic execution. No live broker orders are sent.</p></div>}
            {!loading && !replayData && !error && <div className="replay-empty"><div className="replay-empty-orbit"><span>R</span></div><h3>Replay a production session</h3><p>Inspect exactly how a historical market stream moves through scanner, strategies, AI gates, Spawn, risk, orders, fills and exits.</p><div className="replay-trust-row"><span>◆ Real TradingEngine._tick</span><span>◆ Strict as-of data</span><span>◆ Isolated DB + models</span></div></div>}

            {!loading && replayData && <>
              <div className="replay-kpis">
                <div className="replay-kpi"><span className="k">Production ticks</span><strong className="num">{session.production_ticks ?? 0}</strong><small>TradingEngine._tick calls</small></div>
                <div className="replay-kpi"><span className="k">Candidates</span><strong className="num">{session.total_candidates ?? ledger.candidates?.length ?? 0}</strong><small>Scanner observations</small></div>
                <div className="replay-kpi"><span className="k">Orders / fills</span><strong className="num">{session.total_orders ?? 0} / {session.total_fills ?? 0}</strong><small>Execution lifecycle</small></div>
                <div className="replay-kpi"><span className="k">Trades</span><strong className="num">{session.total_trades ?? trades.length}</strong><small>Closed outcomes</small></div>
                <div className="replay-kpi"><span className="k">Final equity</span><strong className="num">{session.final_equity != null ? `$${Number(session.final_equity).toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—"}</strong><small>{shortHash(session.state_hash)}</small></div>
              </div>

              <nav className="replay-tabs">{([ ["flow","Decision flow"], ["timeline",`Timeline ${timeline.length}`], ["trades",`Trades ${trades.length}`], ["manifest","Manifest"] ] as [Tab,string][]).map(([id,label]) => <button key={id} className={activeTab === id ? "active" : ""} onClick={() => setActiveTab(id)}>{label}</button>)}</nav>

              {activeTab === "flow" && <div className="replay-flow-grid">{STAGES.map(([type, name, sub], index) => <div className="replay-stage-wrap" key={type}><div className={`replay-stage ${stageCounts[type] ? "active" : "quiet"}`}><div className="replay-stage-top"><span className="replay-stage-index">{String(index + 1).padStart(2,"0")}</span><span className="replay-stage-count num">{stageCounts[type] || 0}</span></div><strong>{name}</strong><small>{sub}</small><div className="replay-stage-meter"><span style={{ width: stageCounts[type] ? "100%" : "0%" }} /></div></div>{index < STAGES.length - 1 && <span className="replay-stage-arrow">→</span>}</div>)}</div>}

              {activeTab === "timeline" && <div className="replay-panel"><div className="replay-toolbar"><select value={eventFilter} onChange={(e) => setEventFilter(e.target.value)}><option value="ALL">All events</option>{STAGES.map(([type,name]) => <option key={type} value={type}>{name}</option>)}</select><input type="text" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter symbol, gate, reason…" /><span className="muted num">{filtered.length} events</span></div><div className="replay-event-list">{filtered.length ? filtered.map((evt, i) => <div className="replay-event" key={`${evt.timestamp}-${i}`}><div className="replay-event-time mono">{fmtTs(evt.timestamp)}</div><span className={`replay-event-type ${eventTone(evt.event_type)}`}>{evt.event_type || "EVENT"}</span><strong className="replay-event-symbol">{evt.symbol || "—"}</strong><code>{JSON.stringify(evt.details || {})}</code></div>) : <div className="replay-no-rows">No events match this filter.</div>}</div></div>}

              {activeTab === "trades" && <div className="replay-panel replay-table-wrap"><table className="grid replay-table"><thead><tr><th>Exit time</th><th>Symbol</th><th>Strategy</th><th>Side</th><th>Reason</th><th className="r">P&L</th><th className="r">R</th></tr></thead><tbody>{trades.length ? trades.map((tr, i) => { const d:any=tr.details||{}; const pnl=Number(d.pnl ?? d.realized_pnl); return <tr key={i}><td className="mono">{fmtTs(tr.timestamp)}</td><td><b>{tr.symbol}</b></td><td>{d.strategy || "—"}</td><td>{d.side || d.direction || "—"}</td><td>{d.exit_reason || d.reason || "—"}</td><td className={`r num ${Number.isFinite(pnl) ? pnl >= 0 ? "pos" : "neg" : ""}`}>{Number.isFinite(pnl) ? `$${pnl.toFixed(2)}` : "—"}</td><td className="r num">{d.r_multiple != null ? Number(d.r_multiple).toFixed(2) : "—"}</td></tr>; }) : <tr><td colSpan={7}><div className="replay-no-rows">No closed trades in this replay.</div></td></tr>}</tbody></table></div>}

              {activeTab === "manifest" && <div className="replay-manifest"><div className="replay-manifest-cards"><div><span>Engine commit</span><code>{manifest.engine_commit || "—"}</code></div><div><span>Decision path</span><strong>{manifest.decision_path || "—"}</strong></div><div><span>Lookahead</span><strong>{manifest.lookahead_policy || "—"}</strong></div><div><span>Model policy</span><strong>{manifest.model_policy || "—"}</strong></div><div><span>Fill model</span><strong>{manifest.fill_model || fillModel}</strong></div><div><span>Market data hash</span><code>{shortHash(manifest.data_hash)}</code></div></div><details><summary>Raw reproducibility manifest</summary><pre>{JSON.stringify(manifest, null, 2)}</pre></details></div>}
            </>}
          </div>
        </main>
      </div>
    </section>
  </div>;
}
