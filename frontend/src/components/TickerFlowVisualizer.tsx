import { useState, useEffect } from "react";
import { api } from "../api";
import { fmtTime } from "../format";
import { useStore } from "../store";

interface LiveTickerFlowItem {
  id: string;
  symbol: string;
  strategy: string;
  direction: string;
  timestamp: string;
  tsMs: number;
  final_decision: string;
  rejection_stage: string;
  rejection_reason: string;
  ml_prob: number;
  meta_prob: number;
  trade_score: number;
  market_regime: string;
  ml_threshold: number;
  step_reached: number; // 1 to 7
}

const STAGES = [
  { id: 1, key: "scanner", title: "1. Market Scanner", sub: "Universe Discovery" },
  { id: 2, key: "strategy", title: "2. Strategy Trigger", sub: "Pattern Generation" },
  { id: 3, key: "ml_gate", title: "3. ML Gate", sub: "Regime-Adaptive Scale" },
  { id: 4, key: "meta_label", title: "4. Meta-Label", sub: "Profitability Filter" },
  { id: 5, key: "trade_score", title: "5. Trade Score", sub: "Setup Quality" },
  { id: 6, key: "risk_gate", title: "6. Portfolio & Risk", sub: "Capital Allocation" },
  { id: 7, key: "execution", title: "7. Execution / Replay", sub: "Broker / Shadow" },
];

export function TickerFlowVisualizer() {
  const [stream, setStream] = useState<LiveTickerFlowItem[]>([]);
  const [lastTickTime, setLastTickTime] = useState<Date>(new Date());
  const logs = useStore((s) => s.logs);

  const fetchLiveStream = async () => {
    try {
      const data = await api.shadowLedgerCandidates(60);
      if (data && Array.isArray(data)) {
        const parsed: LiveTickerFlowItem[] = data.map((s, idx) => {
          const payload = s.payload && typeof s.payload === "object" ? s.payload : {};
          const gateEv = s.gate_evidence_json || payload.gate_evidence_json || {};

          const decision = (s.final_decision || payload.final_decision || "rejected").toLowerCase();
          const stage = s.rejection_stage || payload.rejection_stage || gateEv.first_decisive_gate || "ml_gate";
          const regime = (s.market_regime || payload.market_regime || "NORMAL").toUpperCase();
          const regimeThresholds: Record<string, number> = { NORMAL: 0.48, CAUTION: 0.52, STRESS: 0.60, PANIC: 0.65 };
          const mlThresh = regimeThresholds[regime] || 0.48;

          const mlP = Number(s.ml_confidence ?? s.ml_probability ?? payload.ml_confidence ?? payload.ml_probability ?? 0.52);
          const metaP = Number(s.meta_probability ?? payload.meta_probability ?? 0.54);
          const tScore = Number(s.trade_score ?? payload.trade_score ?? 62.5);

          // Calculate how far the ticker progressed through the 7 stages
          let step = 7;
          if (decision !== "accepted") {
            if (stage === "ml_gate") step = 3;
            else if (stage === "meta_label_gate") step = 4;
            else if (stage === "trade_score_gate") step = 5;
            else if (stage === "portfolio_gate" || stage === "risk_gate" || stage === "behavioral_safety_gate") step = 6;
            else step = 3;
          }

          const rawTs = s.timestamp || s.ts || payload.timestamp || "";
          const tsMs = rawTs ? new Date(rawTs).getTime() : Date.now() - idx * 1000;

          return {
            id: s.candidate_id || s.id || `flow-${idx}`,
            symbol: s.symbol || "UNKNOWN",
            strategy: s.strategy || payload.strategy || "Strategy",
            direction: (s.direction || payload.direction || "long").toUpperCase(),
            timestamp: rawTs,
            tsMs: isNaN(tsMs) ? Date.now() - idx * 1000 : tsMs,
            final_decision: decision,
            rejection_stage: stage,
            rejection_reason: s.rejection_reason || payload.rejection_reason || "Threshold not met",
            ml_prob: mlP,
            meta_prob: metaP,
            trade_score: tScore,
            market_regime: regime,
            ml_threshold: mlThresh,
            step_reached: step,
          };
        });

        // Sort stream chronologically descending (newest at top)
        parsed.sort((a, b) => b.tsMs - a.tsMs);
        setStream(parsed);
        setLastTickTime(new Date());
      }
    } catch (err) {
      console.error("Failed to load live flow stream", err);
    }
  };

  // Poll live candidate stream every 2 seconds
  useEffect(() => {
    fetchLiveStream();
    const timer = setInterval(fetchLiveStream, 2000);
    return () => clearInterval(timer);
  }, []);

  // Instant trigger when engine emits new log or tick
  useEffect(() => {
    if (logs.length > 0) {
      fetchLiveStream();
    }
  }, [logs.length]);

  const passedCount = stream.filter((x) => x.final_decision === "accepted").length;
  const passRate = stream.length > 0 ? ((passedCount / stream.length) * 100).toFixed(1) : "0.0";

  return (
    <div style={{ padding: "16px", color: "#e2e8f0", background: "#0b1329", borderRadius: "8px" }}>
      {/* Live Stream Status Bar */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "6px",
              padding: "4px 10px",
              borderRadius: "16px",
              background: "#0284c722",
              border: "1px solid #0284c766",
              color: "#38bdf8",
              fontSize: "0.8rem",
              fontWeight: "bold",
            }}
          >
            <span
              style={{
                width: "8px",
                height: "8px",
                borderRadius: "50%",
                background: "#38bdf8",
                boxShadow: "0 0 10px #38bdf8",
                animation: "pulse 1.2s infinite",
              }}
            />
            LIVE STREAMING PIPELINE
          </span>
          <span style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
            Updated: {lastTickTime.toLocaleTimeString()}
          </span>
        </div>

        {/* Live Summary Metrics */}
        <div style={{ display: "flex", gap: "20px", fontSize: "0.85rem" }}>
          <div>
            <span style={{ color: "#64748b" }}>Recent Candidates: </span>
            <span style={{ fontWeight: "bold", color: "#f8fafc" }}>{stream.length}</span>
          </div>
          <div>
            <span style={{ color: "#64748b" }}>Pass Rate: </span>
            <span style={{ fontWeight: "bold", color: "#4ade80" }}>{passRate}%</span>
          </div>
          <div>
            <span style={{ color: "#64748b" }}>Veto Rate: </span>
            <span style={{ fontWeight: "bold", color: "#f87171" }}>{(100 - Number(passRate)).toFixed(1)}%</span>
          </div>
        </div>
      </div>

      {/* 7-Stage Matrix Live Stream */}
      <div style={{ display: "flex", gap: "10px", overflowX: "auto", minHeight: "420px", paddingBottom: "10px" }}>
        {STAGES.map((stage) => {
          // Tickers currently active or terminated at this stage
          const stageTickers = stream.filter((item) => {
            if (stage.id < item.step_reached) return true; // Passed through this stage
            if (stage.id === item.step_reached) return true; // Stopped or active at this stage
            return false;
          });

          return (
            <div
              key={stage.id}
              style={{
                flex: "1 0 180px",
                minWidth: "180px",
                background: "#111c35",
                border: "1px solid #1e293b",
                borderRadius: "8px",
                display: "flex",
                flexDirection: "column",
              }}
            >
              {/* Stage Header */}
              <div
                style={{
                  padding: "10px 12px",
                  background: "#1e293b",
                  borderBottom: "1px solid #334155",
                  borderRadius: "8px 8px 0 0",
                }}
              >
                <div style={{ fontWeight: "bold", fontSize: "0.85rem", color: "#f8fafc" }}>{stage.title}</div>
                <div style={{ fontSize: "0.7rem", color: "#94a3b8" }}>{stage.sub}</div>
              </div>

              {/* Ticker Stream Column */}
              <div style={{ padding: "8px", display: "flex", flexDirection: "column", gap: "8px", flex: 1, overflowY: "auto" }}>
                {stageTickers.length === 0 ? (
                  <div style={{ fontSize: "0.75rem", color: "#475569", textAlign: "center", padding: "20px 0" }}>
                    No active tickers
                  </div>
                ) : (
                  stageTickers.slice(0, 15).map((item) => {
                    const isTerminatedHere = item.step_reached === stage.id && item.final_decision !== "accepted";
                    const isPassedHere = stage.id < item.step_reached || (stage.id === 7 && item.final_decision === "accepted");

                    let cardColor = "#0284c7"; // Blue passed
                    let borderColor = "#0284c744";
                    let bg = "#0369a115";

                    if (isTerminatedHere) {
                      cardColor = "#f87171"; // Red veto
                      borderColor = "#ef444466";
                      bg = "#7f1d1d25";
                    } else if (stage.id === 7 && item.final_decision === "accepted") {
                      cardColor = "#4ade80"; // Green execute
                      borderColor = "#22c55e66";
                      bg = "#15803d25";
                    }

                    return (
                      <div
                        key={`${item.id}-${stage.id}`}
                        style={{
                          background: bg,
                          border: `1px solid ${borderColor}`,
                          borderRadius: "6px",
                          padding: "8px 10px",
                          transition: "all 0.3s ease",
                        }}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <span style={{ fontWeight: "bold", fontSize: "0.9rem", color: "#f8fafc" }}>{item.symbol}</span>
                          <span style={{ fontSize: "0.65rem", color: item.direction === "LONG" ? "#4ade80" : "#f87171", fontWeight: "bold" }}>
                            {item.direction}
                          </span>
                        </div>

                        <div style={{ fontSize: "0.7rem", color: "#94a3b8", margin: "2px 0 4px" }}>
                          {item.strategy}
                        </div>

                        {/* Stage Specific Telemetry Pill */}
                        {stage.id === 3 && (
                          <div style={{ fontSize: "0.68rem", color: item.ml_prob >= item.ml_threshold ? "#38bdf8" : "#f87171" }}>
                            ML: {(item.ml_prob * 100).toFixed(1)}% (Thresh: {(item.ml_threshold * 100).toFixed(0)}%)
                          </div>
                        )}
                        {stage.id === 4 && (
                          <div style={{ fontSize: "0.68rem", color: item.meta_prob >= 0.5 ? "#38bdf8" : "#f87171" }}>
                            Meta: {(item.meta_prob * 100).toFixed(1)}%
                          </div>
                        )}
                        {stage.id === 5 && (
                          <div style={{ fontSize: "0.68rem", color: item.trade_score >= 50 ? "#38bdf8" : "#f87171" }}>
                            Score: {item.trade_score.toFixed(1)} / 100
                          </div>
                        )}
                        {stage.id === 7 && (
                          <div style={{ fontSize: "0.68rem", color: item.final_decision === "accepted" ? "#4ade80" : "#c084fc" }}>
                            {item.final_decision === "accepted" ? "✓ BROKER FILL" : "SHADOW A/B REPLAY"}
                          </div>
                        )}

                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "4px" }}>
                          <span style={{ fontSize: "0.65rem", color: "#64748b" }}>
                            {fmtTime(item.timestamp)}
                          </span>
                          <span
                            style={{
                              fontSize: "0.6rem",
                              fontWeight: "bold",
                              color: cardColor,
                              textTransform: "uppercase",
                            }}
                          >
                            {isTerminatedHere ? `VETO: ${item.rejection_stage}` : isPassedHere ? "PASS →" : "HOLD"}
                          </span>
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Real-time Ticker Activity Event Log Feed */}
      <div style={{ marginTop: "16px", background: "#0f172a", border: "1px solid #1e293b", borderRadius: "8px", padding: "12px" }}>
        <div style={{ fontSize: "0.8rem", fontWeight: "bold", color: "#94a3b8", marginBottom: "8px" }}>
          📡 REAL-TIME TICKER PIPELINE EVENT STREAM
        </div>
        <div style={{ maxHeight: "120px", overflowY: "auto", display: "flex", flexDirection: "column", gap: "4px" }}>
          {stream.slice(0, 10).map((item) => (
            <div key={`log-${item.id}`} style={{ fontSize: "0.75rem", fontFamily: "monospace", display: "flex", gap: "10px" }}>
              <span style={{ color: "#64748b" }}>{fmtTime(item.timestamp)}</span>
              <span style={{ fontWeight: "bold", color: "#38bdf8" }}>{item.symbol}</span>
              <span style={{ color: "#94a3b8" }}>{item.strategy} ({item.direction})</span>
              <span style={{ color: item.final_decision === "accepted" ? "#4ade80" : "#f87171" }}>
                {item.final_decision === "accepted"
                  ? "→ EXECUTED TO BROKER"
                  : `→ VETOED at ${item.rejection_stage} (${item.rejection_reason})`}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
