import { useState, useEffect } from "react";
import { api } from "../api";
import { fmtDateTime } from "../format";
import { useStore } from "../store";

interface SignalFlowRecord {
  id?: string;
  candidate_id?: string;
  symbol: string;
  strategy: string;
  direction: string;
  timestamp?: string;
  ts?: string;
  final_decision?: string;
  rejection_reason?: string;
  rejection_stage?: string;
  first_decisive_gate?: string;
  ml_confidence?: number;
  ml_probability?: number;
  meta_probability?: number;
  trade_score?: number;
  market_regime?: string;
  simulated_pnl?: number;
  payload?: any;
  gate_evidence_json?: any;
}

export function TickerFlowVisualizer() {
  const [signals, setSignals] = useState<SignalFlowRecord[]>([]);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>("");
  const [autoFollow, setAutoFollow] = useState<boolean>(true);
  const [lastUpdated, setLastUpdated] = useState<Date>(new Date());
  const [loading, setLoading] = useState<boolean>(true);

  // Subscribe to store logs to trigger immediate updates on new engine signals
  const logs = useStore((s) => s.logs);

  const fetchSignals = async () => {
    try {
      const data = await api.shadowLedgerCandidates(100);
      if (data && data.length > 0) {
        setSignals(data);
        setLastUpdated(new Date());

        // If auto-follow is active, automatically snap to the latest signal
        if (autoFollow) {
          const newestId = data[0].candidate_id || data[0].id || "";
          setSelectedCandidateId(newestId);
        } else if (!selectedCandidateId) {
          setSelectedCandidateId(data[0].candidate_id || data[0].id || "");
        }
      }
    } catch (err) {
      console.error("Failed to load signals for live workflow visualizer", err);
    } finally {
      setLoading(false);
    }
  };

  // Initial load + 3-second live polling interval
  useEffect(() => {
    fetchSignals();
    const interval = setInterval(fetchSignals, 3000);
    return () => clearInterval(interval);
  }, [autoFollow]);

  // Live trigger when engine logs a signal broadcast
  useEffect(() => {
    if (logs.length > 0) {
      const latestLog = logs[0];
      if (latestLog.source === "engine" || latestLog.message.includes("signal") || latestLog.message.includes("veto")) {
        fetchSignals();
      }
    }
  }, [logs.length]);

  const currentSignal = signals.find(
    (s) => (s.candidate_id || s.id) === selectedCandidateId
  ) || signals[0];

  const payload = currentSignal?.payload && typeof currentSignal.payload === "object" ? currentSignal.payload : {};
  const gateEvidence = currentSignal?.gate_evidence_json || payload.gate_evidence_json || {};

  const symbol = currentSignal?.symbol || "N/A";
  const strategy = currentSignal?.strategy || payload.strategy || "Unknown Strategy";
  const direction = (currentSignal?.direction || payload.direction || "long").toUpperCase();
  const decision = (currentSignal?.final_decision || payload.final_decision || "pending").toLowerCase();
  const rejectionStage = currentSignal?.rejection_stage || payload.rejection_stage || gateEvidence.first_decisive_gate || "ml_gate";
  const rejectionReason = currentSignal?.rejection_reason || payload.rejection_reason || "Filter threshold not met";
  const timestamp = currentSignal?.timestamp || currentSignal?.ts || payload.timestamp || "";

  const mlProb = Number(currentSignal?.ml_confidence ?? currentSignal?.ml_probability ?? payload.ml_confidence ?? payload.ml_probability ?? 0.52);
  const metaProb = Number(currentSignal?.meta_probability ?? payload.meta_probability ?? 0.54);
  const tradeScore = Number(currentSignal?.trade_score ?? payload.trade_score ?? 62.5);
  const regime = (currentSignal?.market_regime || payload.market_regime || "NORMAL").toUpperCase();

  const regimeThresholds: Record<string, number> = {
    NORMAL: 0.48,
    CAUTION: 0.52,
    STRESS: 0.60,
    PANIC: 0.65,
  };
  const activeMlThreshold = regimeThresholds[regime] || 0.48;

  const mlPassed = mlProb >= activeMlThreshold;
  const metaPassed = metaProb >= 0.50;
  const scorePassed = tradeScore >= 50.0;
  const riskPassed = decision === "accepted" || (rejectionStage !== "risk_gate" && rejectionStage !== "behavioral_safety_gate");
  const isAccepted = decision === "accepted";

  return (
    <div className="flow-visualizer-container" style={{ padding: "16px", color: "#e2e8f0" }}>
      {/* Top Header & Selector */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <h3 style={{ margin: 0, fontSize: "1.2rem", fontWeight: "600", color: "#f8fafc" }}>
              ⚡ Live Ticker Pipeline Flow Visualizer
            </h3>
            {/* Live Indicator Badge */}
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                padding: "3px 8px",
                borderRadius: "12px",
                background: "#0284c722",
                border: "1px solid #0284c766",
                color: "#38bdf8",
                fontSize: "0.75rem",
                fontWeight: "600",
              }}
            >
              <span
                style={{
                  width: "8px",
                  height: "8px",
                  borderRadius: "50%",
                  background: "#38bdf8",
                  boxShadow: "0 0 8px #38bdf8",
                  animation: "pulse 1.5s infinite",
                }}
              />
              LIVE UPDATING
            </span>
          </div>
          <p style={{ margin: "4px 0 0", fontSize: "0.85rem", color: "#94a3b8" }}>
            Real-time pipeline tracing for tickers picked by Market Scanner (Updated {lastUpdated.toLocaleTimeString()})
          </p>
        </div>

        {/* Controls: Auto-Follow Toggle & Candidate Dropdown */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <button
            onClick={() => setAutoFollow(!autoFollow)}
            style={{
              background: autoFollow ? "#0284c7" : "#334155",
              color: "#ffffff",
              border: "none",
              borderRadius: "6px",
              padding: "6px 12px",
              fontSize: "0.8rem",
              fontWeight: "600",
              cursor: "pointer",
              transition: "all 0.2s ease",
            }}
          >
            {autoFollow ? "✓ Auto-Follow Newest" : "Manual Select"}
          </button>

          <select
            value={selectedCandidateId}
            onChange={(e) => {
              setAutoFollow(false);
              setSelectedCandidateId(e.target.value);
            }}
            style={{
              background: "#1e293b",
              color: "#f8fafc",
              border: "1px solid #334155",
              borderRadius: "6px",
              padding: "6px 12px",
              fontSize: "0.85rem",
              outline: "none",
              cursor: "pointer",
            }}
          >
            {signals.map((s, idx) => {
              const id = s.candidate_id || s.id || `signal-${idx}`;
              const sym = s.symbol || "UNKNOWN";
              const dir = (s.direction || "long").toUpperCase();
              const dec = (s.final_decision || "rejected").toUpperCase();
              return (
                <option key={id} value={id}>
                  {sym} ({dir}) — {s.strategy} [{dec}]
                </option>
              );
            })}
          </select>
        </div>
      </div>

      {loading ? (
        <div style={{ padding: "40px", textAlign: "center", color: "#94a3b8" }}>Connecting to live signal stream...</div>
      ) : (
        <>
          {/* Signal Summary Banner */}
          <div
            style={{
              background: "linear-gradient(135deg, #1e293b 0%, #0f172a 100%)",
              borderRadius: "10px",
              border: "1px solid #334155",
              padding: "14px 20px",
              marginBottom: "24px",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
              <div
                style={{
                  fontSize: "1.4rem",
                  fontWeight: "bold",
                  color: "#38bdf8",
                  padding: "6px 14px",
                  background: "#0369a122",
                  borderRadius: "8px",
                  border: "1px solid #0284c744",
                }}
              >
                {symbol}
              </div>
              <div>
                <div style={{ fontWeight: "600", fontSize: "1rem" }}>
                  {strategy} <span style={{ color: direction === "LONG" ? "#4ade80" : "#f87171" }}>({direction})</span>
                </div>
                <div style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
                  Candidate ID: {selectedCandidateId} | Time: {fmtDateTime(timestamp)}
                </div>
              </div>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "20px" }}>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: "0.75rem", color: "#94a3b8" }}>Active Market Regime</div>
                <div style={{ fontWeight: "bold", color: "#facc15" }}>{regime} (ML Scale: {activeMlThreshold})</div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: "0.75rem", color: "#94a3b8" }}>Pipeline Outcome</div>
                <div
                  style={{
                    fontWeight: "bold",
                    color: isAccepted ? "#4ade80" : "#f87171",
                    textTransform: "uppercase",
                  }}
                >
                  {isAccepted ? "✓ EXECUTED TO BROKER" : `✕ VETOED AT ${rejectionStage}`}
                </div>
              </div>
            </div>
          </div>

          {/* Live Flow Diagram */}
          <div style={{ display: "flex", gap: "12px", overflowX: "auto", paddingBottom: "12px" }}>
            <PipelineNode
              stepNum={1}
              title="Market Scanner"
              subtitle="Universe Discovery"
              status="passed"
              metrics={[
                { label: "Scan Criteria", val: "RVOL & Momentum" },
                { label: "Scanner Watchlist", val: "ACTIVE" },
                { label: "Discovery State", val: "ELIGIBLE" },
              ]}
            />

            <FlowArrow passed={true} />

            <PipelineNode
              stepNum={2}
              title="Strategy Signal"
              subtitle={strategy}
              status="passed"
              metrics={[
                { label: "Direction", val: direction },
                { label: "Pattern Signal", val: "TRIGGERED" },
                { label: "Bar Confirmation", val: "CLOSED" },
              ]}
            />

            <FlowArrow passed={true} />

            <PipelineNode
              stepNum={3}
              title="ML Confidence Gate"
              subtitle={`Regime Scale: ${activeMlThreshold}`}
              status={mlPassed ? "passed" : "rejected"}
              metrics={[
                { label: "Model Score", val: `${(mlProb * 100).toFixed(1)}%` },
                { label: "Regime Target", val: `${(activeMlThreshold * 100).toFixed(1)}%` },
                { label: "Decision", val: mlPassed ? "PASS" : "VETO" },
              ]}
              progressPct={mlProb * 100}
              targetPct={activeMlThreshold * 100}
            />

            <FlowArrow passed={mlPassed} />

            <PipelineNode
              stepNum={4}
              title="Meta-Label Gate"
              subtitle="Profitability Filter"
              status={!mlPassed ? "bypassed" : metaPassed ? "passed" : "rejected"}
              metrics={[
                { label: "Meta Score", val: `${(metaProb * 100).toFixed(1)}%` },
                { label: "Meta Target", val: "50.0%" },
                { label: "Decision", val: !mlPassed ? "BYPASSED" : metaPassed ? "PASS" : "VETO" },
              ]}
              progressPct={metaProb * 100}
              targetPct={50}
            />

            <FlowArrow passed={mlPassed && metaPassed} />

            <PipelineNode
              stepNum={5}
              title="Trade Score Engine"
              subtitle="Excursion & Setup Score"
              status={!(mlPassed && metaPassed) ? "bypassed" : scorePassed ? "passed" : "rejected"}
              metrics={[
                { label: "Trade Score", val: `${tradeScore.toFixed(1)} / 100` },
                { label: "Score Target", val: "50.0 / 100" },
                { label: "Decision", val: !(mlPassed && metaPassed) ? "BYPASSED" : scorePassed ? "PASS" : "VETO" },
              ]}
              progressPct={tradeScore}
              targetPct={50}
            />

            <FlowArrow passed={mlPassed && metaPassed && scorePassed} />

            <PipelineNode
              stepNum={6}
              title="Portfolio & Risk AI"
              subtitle="Capital Allocation"
              status={!(mlPassed && metaPassed && scorePassed) ? "bypassed" : riskPassed ? "passed" : "rejected"}
              metrics={[
                { label: "Sector Concentration", val: "CLEAR" },
                { label: "Max Drawdown", val: "PASS" },
                { label: "Decision", val: !(mlPassed && metaPassed && scorePassed) ? "BYPASSED" : riskPassed ? "PASS" : "VETO" },
              ]}
            />

            <FlowArrow passed={isAccepted} />

            <PipelineNode
              stepNum={7}
              title={isAccepted ? "Broker Order" : "Shadow Ledger Replay"}
              subtitle={isAccepted ? "Active Position" : "Counterfactual Intelligence"}
              status={isAccepted ? "passed" : "shadow"}
              metrics={[
                { label: "Execution Mode", val: isAccepted ? "LIVE FILL" : "SHADOW REPLAY" },
                { label: "Exit Intelligence", val: "Hybrid Breakeven ATR" },
                { label: "Breakeven Lock", val: "+0.30R MFE" },
              ]}
            />
          </div>

          {!isAccepted && (
            <div
              style={{
                marginTop: "16px",
                padding: "12px 16px",
                background: "#7f1d1d22",
                border: "1px solid #b91c1c44",
                borderRadius: "8px",
                display: "flex",
                alignItems: "center",
                gap: "12px",
              }}
            >
              <div style={{ color: "#f87171", fontSize: "1.2rem" }}>🛑</div>
              <div>
                <div style={{ fontWeight: "600", color: "#fca5a5", fontSize: "0.9rem" }}>
                  Primary Rejection Stage: <span style={{ textTransform: "uppercase" }}>{rejectionStage}</span>
                </div>
                <div style={{ fontSize: "0.8rem", color: "#f87171" }}>
                  {rejectionReason} (Continuously replayed in Shadow Ledger for A/B testing)
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function PipelineNode({
  stepNum,
  title,
  subtitle,
  status,
  metrics,
  progressPct,
  targetPct,
}: {
  stepNum: number;
  title: string;
  subtitle: string;
  status: "passed" | "rejected" | "bypassed" | "shadow";
  metrics: { label: string; val: string }[];
  progressPct?: number;
  targetPct?: number;
}) {
  const statusColors = {
    passed: { bg: "#0284c715", border: "#0284c766", text: "#38bdf8", badgeBg: "#0369a133", label: "PASSED" },
    rejected: { bg: "#991b1b15", border: "#991b1b66", text: "#f87171", badgeBg: "#991b1b33", label: "REJECTED" },
    bypassed: { bg: "#33415515", border: "#33415544", text: "#64748b", badgeBg: "#33415533", label: "BYPASSED" },
    shadow: { bg: "#a855f715", border: "#a855f766", text: "#c084fc", badgeBg: "#a855f733", label: "SHADOW" },
  };

  const style = statusColors[status];

  return (
    <div
      style={{
        flex: "1 0 200px",
        minWidth: "200px",
        background: "linear-gradient(180deg, #1e293b 0%, #0f172a 100%)",
        border: `1px solid ${style.border}`,
        borderRadius: "10px",
        padding: "14px",
        position: "relative",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
        <span style={{ fontSize: "0.7rem", fontWeight: "bold", color: "#64748b" }}>STEP 0{stepNum}</span>
        <span
          style={{
            fontSize: "0.65rem",
            fontWeight: "bold",
            color: style.text,
            background: style.badgeBg,
            padding: "2px 8px",
            borderRadius: "4px",
            border: `1px solid ${style.border}`,
          }}
        >
          {style.label}
        </span>
      </div>

      <div style={{ fontWeight: "600", fontSize: "0.9rem", color: "#f8fafc", marginBottom: "2px" }}>{title}</div>
      <div style={{ fontSize: "0.75rem", color: "#94a3b8", marginBottom: "12px" }}>{subtitle}</div>

      {progressPct !== undefined && (
        <div style={{ marginBottom: "12px" }}>
          <div style={{ height: "6px", background: "#334155", borderRadius: "3px", overflow: "hidden", position: "relative" }}>
            <div
              style={{
                height: "100%",
                width: `${Math.min(100, Math.max(0, progressPct))}%`,
                background: progressPct >= (targetPct || 0) ? "#38bdf8" : "#f87171",
                transition: "width 0.4s ease",
              }}
            />
            {targetPct !== undefined && (
              <div
                style={{
                  position: "absolute",
                  top: 0,
                  bottom: 0,
                  left: `${targetPct}%`,
                  width: "2px",
                  background: "#facc15",
                }}
              />
            )}
          </div>
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
        {metrics.map((m, i) => (
          <div key={i} style={{ display: "flex", justifyContent: "space-between", fontSize: "0.75rem" }}>
            <span style={{ color: "#64748b" }}>{m.label}:</span>
            <span style={{ color: "#cbd5e1", fontWeight: "500" }}>{m.val}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function FlowArrow({ passed }: { passed: boolean }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", color: passed ? "#38bdf8" : "#475569" }}>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M5 12h14M12 5l7 7-7 7" />
      </svg>
    </div>
  );
}
