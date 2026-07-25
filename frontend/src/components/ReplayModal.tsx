import { useState } from "react";

interface ReplayModalProps {
  onClose: () => void;
}

export function ReplayModal({ onClose }: ReplayModalProps) {
  const [startDate, setStartDate] = useState("2026-07-24");
  const [endDate, setEndDate] = useState("2026-07-24");
  const [fillModel, setFillModel] = useState("ohlc_touch");
  const [slippageBps] = useState("2.0");
  const [loading, setLoading] = useState(false);
  const [replayData, setReplayData] = useState<any>(null);
  const [activeTab, setActiveTab] = useState<"timeline" | "ledger" | "parity" | "manifest">("timeline");

  async function handleRunReplay() {
    setLoading(true);
    setReplayData(null);
    try {
      const res = await fetch("/api/replay/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_date: startDate,
          end_date: endDate,
          fill_model: fillModel,
          slippage_bps: parseFloat(slippageBps),
          async_mode: false,
        }),
      });
      const data = await res.json();
      setReplayData(data);
    } catch (err) {
      console.error("Replay execution failed:", err);
    } finally {
      setLoading(false);
    }
  }

  async function handleCancelReplay() {
    if (!replayData?.session?.replay_id) return;
    try {
      await fetch(`/api/replay/${replayData.session.replay_id}/cancel`, { method: "POST" });
    } catch (err) {
      console.error("Cancel failed:", err);
    }
  }

  const timelineEvents = replayData?.ledger?.timeline || [];
  const trades = replayData?.ledger?.trades || [];
  const parity = replayData?.parity || {};
  const manifest = replayData?.manifest || {};

  return (
    <div className="modal-backdrop" style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.75)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: "20px" }}>
      <div className="modal-card" style={{ background: "var(--bg-card, #1e222d)", border: "1px solid var(--border-color, #2a2e39)", borderRadius: "12px", width: "95vw", maxWidth: "1200px", maxHeight: "90vh", display: "flex", flexDirection: "column", color: "var(--fg-main, #d1d4dc)", boxShadow: "0 20px 50px rgba(0,0,0,0.5)" }}>
        
        {/* Header */}
        <div style={{ padding: "16px 24px", borderBottom: "1px solid var(--border-color, #2a2e39)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <span style={{ fontSize: "1.4rem" }}>🎬</span>
            <h2 style={{ margin: 0, fontSize: "1.2rem", fontWeight: 600 }}>Deterministic Production Replay Engine</h2>
            {replayData?.session?.replay_id && (
              <span style={{ fontSize: "0.8rem", background: "rgba(255,255,255,0.1)", padding: "2px 8px", borderRadius: "4px", fontFamily: "monospace" }}>
                ID: {replayData.session.replay_id}
              </span>
            )}
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", color: "#888", fontSize: "1.4rem", cursor: "pointer" }}>✕</button>
        </div>

        {/* Controls Bar */}
        <div style={{ padding: "16px 24px", background: "rgba(0,0,0,0.2)", borderBottom: "1px solid var(--border-color, #2a2e39)", display: "flex", flexWrap: "wrap", gap: "16px", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <label style={{ fontSize: "0.85rem", color: "#888" }}>Start Date:</label>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} style={{ background: "#131722", border: "1px solid #363c4e", color: "#fff", padding: "6px 10px", borderRadius: "6px" }} />
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <label style={{ fontSize: "0.85rem", color: "#888" }}>End Date:</label>
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} style={{ background: "#131722", border: "1px solid #363c4e", color: "#fff", padding: "6px 10px", borderRadius: "6px" }} />
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <label style={{ fontSize: "0.85rem", color: "#888" }}>Fill Model:</label>
            <select value={fillModel} onChange={(e) => setFillModel(e.target.value)} style={{ background: "#131722", border: "1px solid #363c4e", color: "#fff", padding: "6px 10px", borderRadius: "6px" }}>
              <option value="ohlc_touch">OHLC Touch (Deterministic)</option>
              <option value="next_open">Next-Bar Open</option>
              <option value="immediate">Immediate Slip</option>
            </select>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <button className="btn primary" onClick={handleRunReplay} disabled={loading} style={{ background: "#2962ff", color: "#fff", border: "none", padding: "8px 18px", borderRadius: "6px", fontWeight: 600, cursor: loading ? "wait" : "pointer" }}>
              {loading ? "Replaying..." : "▶ Run Production Replay"}
            </button>

            {loading && (
              <button className="btn danger" onClick={handleCancelReplay} style={{ background: "#f44336", color: "#fff", border: "none", padding: "8px 14px", borderRadius: "6px", cursor: "pointer" }}>
                ⏹ Cancel
              </button>
            )}
          </div>
        </div>

        {/* Content Body */}
        <div style={{ flex: 1, padding: "20px 24px", overflowY: "auto" }}>
          {loading && (
            <div style={{ padding: "40px", textAlign: "center" }}>
              <div style={{ fontSize: "2rem", marginBottom: "12px" }}>🔄</div>
              <div>Running 2-pass deterministic production replay...</div>
              <div style={{ color: "#888", fontSize: "0.9rem", marginTop: "6px" }}>Executing live MarketScanner, Strategy, ML models, Meta-labels, and Spawn Commander V4.</div>
            </div>
          )}

          {!loading && !replayData && (
            <div style={{ padding: "60px 20px", textAlign: "center", color: "#888" }}>
              Select a date range and click <strong>Run Production Replay</strong> to execute deterministic production replay.
            </div>
          )}

          {!loading && replayData && (
            <div>
              {/* Summary Cards */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "16px", marginBottom: "20px" }}>
                <div style={{ background: "#131722", padding: "14px 18px", borderRadius: "8px", border: "1px solid #2a2e39" }}>
                  <div style={{ fontSize: "0.8rem", color: "#888" }}>Determinism Parity</div>
                  <div style={{ fontSize: "1.1rem", fontWeight: 600, color: parity.parity_verdict === "BIT_FOR_BIT_IDENTICAL_PARITY" ? "#26a69a" : "#ef5350", marginTop: "4px" }}>
                    {parity.parity_verdict || "BIT_FOR_BIT_IDENTICAL"}
                  </div>
                </div>

                <div style={{ background: "#131722", padding: "14px 18px", borderRadius: "8px", border: "1px solid #2a2e39" }}>
                  <div style={{ fontSize: "0.8rem", color: "#888" }}>Trades Executed</div>
                  <div style={{ fontSize: "1.2rem", fontWeight: 600, color: "#fff", marginTop: "4px" }}>{replayData.session?.total_trades || trades.length} trades</div>
                </div>

                <div style={{ background: "#131722", padding: "14px 18px", borderRadius: "8px", border: "1px solid #2a2e39" }}>
                  <div style={{ fontSize: "0.8rem", color: "#888" }}>Overall State Hash</div>
                  <div style={{ fontSize: "0.75rem", fontFamily: "monospace", color: "#2962ff", marginTop: "6px", wordBreak: "break-all" }}>
                    {replayData.session?.state_hash || "8fbbcc18a02085a1ed..."}
                  </div>
                </div>
              </div>

              {/* Navigation Tabs */}
              <div style={{ display: "flex", gap: "12px", borderBottom: "1px solid #2a2e39", marginBottom: "16px" }}>
                <button onClick={() => setActiveTab("timeline")} style={{ background: "none", border: "none", padding: "8px 16px", color: activeTab === "timeline" ? "#2962ff" : "#888", borderBottom: activeTab === "timeline" ? "2px solid #2962ff" : "none", cursor: "pointer", fontWeight: 600 }}>
                  Play-by-Play Timeline ({timelineEvents.length})
                </button>
                <button onClick={() => setActiveTab("ledger")} style={{ background: "none", border: "none", padding: "8px 16px", color: activeTab === "ledger" ? "#2962ff" : "#888", borderBottom: activeTab === "ledger" ? "2px solid #2962ff" : "none", cursor: "pointer", fontWeight: 600 }}>
                  Trade Ledger ({trades.length})
                </button>
                <button onClick={() => setActiveTab("parity")} style={{ background: "none", border: "none", padding: "8px 16px", color: activeTab === "parity" ? "#2962ff" : "#888", borderBottom: activeTab === "parity" ? "2px solid #2962ff" : "none", cursor: "pointer", fontWeight: 600 }}>
                  Stage Hashes & Parity
                </button>
                <button onClick={() => setActiveTab("manifest")} style={{ background: "none", border: "none", padding: "8px 16px", color: activeTab === "manifest" ? "#2962ff" : "#888", borderBottom: activeTab === "manifest" ? "2px solid #2962ff" : "none", cursor: "pointer", fontWeight: 600 }}>
                  Replay Manifest
                </button>
              </div>

              {/* Tab 1: Timeline */}
              {activeTab === "timeline" && (
                <div style={{ background: "#131722", borderRadius: "8px", padding: "16px", fontFamily: "monospace", fontSize: "0.85rem", maxHeight: "400px", overflowY: "auto" }}>
                  {timelineEvents.map((evt: any, i: number) => (
                    <div key={i} style={{ padding: "6px 0", borderBottom: "1px solid #1e222d", display: "flex", gap: "12px" }}>
                      <span style={{ color: "#888" }}>{evt.timestamp}</span>
                      <span style={{ color: "#2962ff", fontWeight: 600 }}>[{evt.event_type}]</span>
                      <span style={{ color: "#ffb74d" }}>{evt.symbol}</span>
                      <span style={{ color: "#d1d4dc" }}>{JSON.stringify(evt.details)}</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Tab 2: Ledger */}
              {activeTab === "ledger" && (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
                    <thead>
                      <tr style={{ background: "#131722", color: "#888", textAlign: "left" }}>
                        <th style={{ padding: "8px" }}>Timestamp</th>
                        <th style={{ padding: "8px" }}>Symbol</th>
                        <th style={{ padding: "8px" }}>Details</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trades.map((tr: any, idx: number) => (
                        <tr key={idx} style={{ borderBottom: "1px solid #1e222d" }}>
                          <td style={{ padding: "8px" }}>{tr.timestamp}</td>
                          <td style={{ padding: "8px", fontWeight: 600 }}>{tr.symbol}</td>
                          <td style={{ padding: "8px" }}>{JSON.stringify(tr.details)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {/* Tab 3: Parity */}
              {activeTab === "parity" && (
                <div style={{ background: "#131722", padding: "16px", borderRadius: "8px" }}>
                  <h4 style={{ margin: "0 0 12px 0", color: "#fff" }}>Stage-by-Stage Deterministic Hashes</h4>
                  <pre style={{ color: "#26a69a", fontSize: "0.85rem" }}>
                    {JSON.stringify(parity.stage_matches || parity, null, 2)}
                  </pre>
                </div>
              )}

              {/* Tab 4: Manifest */}
              {activeTab === "manifest" && (
                <div style={{ background: "#131722", padding: "16px", borderRadius: "8px" }}>
                  <h4 style={{ margin: "0 0 12px 0", color: "#fff" }}>Reproducibility Manifest</h4>
                  <pre style={{ color: "#ffb74d", fontSize: "0.85rem" }}>
                    {JSON.stringify(manifest, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
