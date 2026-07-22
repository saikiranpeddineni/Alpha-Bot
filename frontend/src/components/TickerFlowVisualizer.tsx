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
  rejection_stage: string | null;
  rejection_reason: string | null;
  ml_prob: number | null;
  meta_prob: number | null;
  trade_score: number | null;
  market_regime: string | null;
  ml_threshold: number | null;
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

          const decision = s.final_decision ? s.final_decision.toLowerCase() : (payload.final_decision ? payload.final_decision.toLowerCase() : "pending");
          const stage = s.rejection_stage || payload.rejection_stage || gateEv.first_decisive_gate || null;
          const regime = s.market_regime || payload.market_regime || gateEv.market_regime || null;

          // Extract actual applied threshold from backend evidence (never hardcode fallback maps)
          const rawThresh = s.ml_threshold ?? payload.ml_threshold ?? gateEv.applied_ml_threshold ?? gateEv.ml_threshold ?? null;
          const mlThresh = rawThresh !== null ? Number(rawThresh) : null;

          // Extract probabilities cleanly without synthetic fallbacks
          const rawMlP = s.ml_confidence ?? s.ml_probability ?? payload.ml_confidence ?? payload.ml_probability ?? gateEv.ml_probability ?? null;
          const mlP = rawMlP !== null ? Number(rawMlP) : null;

          const rawMetaP = s.meta_probability ?? payload.meta_probability ?? gateEv.meta_probability ?? null;
          const metaP = rawMetaP !== null ? Number(rawMetaP) : null;

          const rawTScore = s.trade_score ?? payload.trade_score ?? gateEv.trade_score ?? null;
          const tScore = rawTScore !== null ? Number(rawTScore) : null;

          // Calculate step reached based strictly on decision & stage
          let step = 7;
          if (decision === "rejected") {
            if (stage === "ml_gate") step = 3;
            else if (stage === "meta_label_gate") step = 4;
            else if (stage === "trade_score_gate") step = 5;
            else if (stage === "portfolio_gate" || stage === "risk_gate" || stage === "behavioral_safety_gate") step = 6;
            else step = 3;
          } else if (decision === "pending") {
            step = 2;
          }

          const rawTs = s.timestamp || s.ts || payload.timestamp || "";
          const tsMs = rawTs ? new Date(rawTs).getTime() : Date.now() - idx * 1000;

          return {
            id: s.candidate_id || s.id || `flow-${idx}`,
            symbol: s.symbol || "N/A",
            strategy: s.strategy || payload.strategy || "Unknown Strategy",
            direction: (s.direction || payload.direction || "N/A").toUpperCase(),
            timestamp: rawTs,
            tsMs: isNaN(tsMs) ? Date.now() - idx * 1000 : tsMs,
            final_decision: decision,
            rejection_stage: stage,
            rejection_reason: s.rejection_reason || payload.rejection_reason || null,
            ml_prob: mlP,
            meta_prob: metaP,
            trade_score: tScore,
            market_regime: regime ? regime.toUpperCase() : null,
            ml_threshold: mlThresh,
            step_reached: step,
          };
        });

        parsed.sort((a, b) => b.tsMs - a.tsMs);
        setStream(parsed);
        setLastTickTime(new Date());
      }
    } catch (err) {
      console.error("Failed to load live flow stream", err);
    }
  };

  useEffect(() => {
    fetchLiveStream();
    const timer = setInterval(fetchLiveStream, 2000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (logs.length > 0) {
      fetchLiveStream();
    }
  }, [logs.length]);

  const passedCount = stream.filter((x) => x.final_decision === "accepted" || x.final_decision === "pipeline_approved").length;
  const rejectedCount = stream.filter((x) => x.final_decision === "rejected").length;
  const passRate = stream.length > 0 ? ((passedCount / stream.length) * 100).toFixed(1) : "N/A";

  return (
    <div style={{ padding: "16px", color: "#e2e8f0", background: "#0b1329", borderRadius: "8px" }}>
      {/* Status Bar */}
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

        <div style={{ display: "flex", gap: "20px", fontSize: "0.85rem" }}>
          <div>
            <span style={{ color: "#64748b" }}>Candidates Sampled: </span>
            <span style={{ fontWeight: "bold", color: "#f8fafc" }}>{stream.length}</span>
          </div>
          <div>
            <span style={{ color: "#64748b" }}>Accepted: </span>
            <span style={{ fontWeight: "bold", color: "#4ade80" }}>{passedCount} ({passRate}%)</span>
          </div>
          <div>
            <span style={{ color: "#64748b" }}>Vetoed: </span>
            <span style={{ fontWeight: "bold", color: "#f87171" }}>{rejectedCount}</span>
          </div>
        </div>
      </div>

      {/* 7-Stage Matrix */}
      <div style={{ display: "flex", gap: "10px", overflowX: "auto", minHeight: "420px", paddingBottom: "10px" }}>
        {STAGES.map((stage) => {
          const stageTickers = stream.filter((item) => {
            if (stage.id < item.step_reached) return true;
            if (stage.id === item.step_reached) return true;
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
                    const isAccepted = item.final_decision === "accepted" || item.final_decision === "pipeline_approved";
                    const isTerminatedHere = item.step_reached === stage.id && item.final_decision === "rejected";
                    const isPassedHere = stage.id < item.step_reached || (stage.id === 7 && isAccepted);

                    let cardColor = "#0284c7";
                    let borderColor = "#0284c744";
                    let bg = "#0369a115";

                    if (isTerminatedHere) {
                      cardColor = "#f87171";
                      borderColor = "#ef444466";
                      bg = "#7f1d1d25";
                    } else if (stage.id === 7 && isAccepted) {
                      cardColor = "#4ade80";
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

                        {/* Telemetry Pills — Display exact backend values or N/A */}
                        {stage.id === 3 && (
                          <div style={{ fontSize: "0.68rem", color: item.ml_prob !== null && item.ml_threshold !== null && item.ml_prob >= item.ml_threshold ? "#38bdf8" : "#f87171" }}>
                            ML: {item.ml_prob !== null ? `${(item.ml_prob * 100).toFixed(1)}%` : "N/A"}
                            {item.ml_threshold !== null ? ` (Thresh: ${(item.ml_threshold * 100).toFixed(0)}%)` : " (Thresh: N/A)"}
                          </div>
                        )}
                        {stage.id === 4 && (
                          <div style={{ fontSize: "0.68rem", color: item.meta_prob !== null && item.meta_prob >= 0.5 ? "#38bdf8" : "#f87171" }}>
                            Meta: {item.meta_prob !== null ? `${(item.meta_prob * 100).toFixed(1)}%` : "N/A"}
                          </div>
                        )}
                        {stage.id === 5 && (
                          <div style={{ fontSize: "0.68rem", color: item.trade_score !== null && item.trade_score >= 50 ? "#38bdf8" : "#f87171" }}>
                            Score: {item.trade_score !== null ? `${item.trade_score.toFixed(1)} / 100` : "N/A"}
                          </div>
                        )}
                        {stage.id === 7 && (
                          <div style={{ fontSize: "0.68rem", color: isAccepted ? "#4ade80" : "#c084fc" }}>
                            {isAccepted ? "✓ BROKER FILL" : "SHADOW A/B REPLAY"}
                          </div>
                        )}

                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "4px" }}>
                          <span style={{ fontSize: "0.65rem", color: "#64748b" }}>
                            {item.timestamp ? fmtTime(item.timestamp) : "N/A"}
                          </span>
                          <span
                            style={{
                              fontSize: "0.6rem",
                              fontWeight: "bold",
                              color: cardColor,
                              textTransform: "uppercase",
                            }}
                          >
                            {isTerminatedHere ? `VETO: ${item.rejection_stage || "gate"}` : isPassedHere ? "PASS →" : "PENDING"}
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

      {/* Real-time Ticker Activity Log */}
      <div style={{ marginTop: "16px", background: "#0f172a", border: "1px solid #1e293b", borderRadius: "8px", padding: "12px" }}>
        <div style={{ fontSize: "0.8rem", fontWeight: "bold", color: "#94a3b8", marginBottom: "8px" }}>
          📡 REAL-TIME TICKER PIPELINE EVIDENCE LOG
        </div>
        <div style={{ maxHeight: "120px", overflowY: "auto", display: "flex", flexDirection: "column", gap: "4px" }}>
          {stream.slice(0, 10).map((item) => {
            const isAccepted = item.final_decision === "accepted" || item.final_decision === "pipeline_approved";
            return (
              <div key={`log-${item.id}`} style={{ fontSize: "0.75rem", fontFamily: "monospace", display: "flex", gap: "10px" }}>
                <span style={{ color: "#64748b" }}>{item.timestamp ? fmtTime(item.timestamp) : "--:--:--"}</span>
                <span style={{ fontWeight: "bold", color: "#38bdf8" }}>{item.symbol}</span>
                <span style={{ color: "#94a3b8" }}>{item.strategy} ({item.direction})</span>
                <span style={{ color: isAccepted ? "#4ade80" : item.final_decision === "rejected" ? "#f87171" : "#facc15" }}>
                  {isAccepted
                    ? "→ EXECUTED TO BROKER"
                    : item.final_decision === "rejected"
                    ? `→ VETOED at ${item.rejection_stage || "UNKNOWN_GATE"} (${item.rejection_reason || "Threshold not met"})`
                    : "→ EVALUATING IN PIPELINE"}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
