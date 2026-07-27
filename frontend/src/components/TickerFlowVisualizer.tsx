import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtTime } from "../format";
import { useStore } from "../store";

interface LiveTickerFlowItem {
  id: string;
  symbol: string;
  strategy: string;
  direction: string;
  timestamp: string;
  tsMs: number | null;
  final_decision: string;
  rejection_stage: string | null;
  rejection_reason: string | null;
  ml_prob: number | null;
  meta_prob: number | null;
  trade_score: number | null;
  ml_threshold: number | null;
  step_reached: number | null;
}

const STAGES = [
  { id: 2, key: "strategy", title: "Strategy" },
  { id: 3, key: "ml_gate", title: "ML Prob" },
  { id: 4, key: "meta_label", title: "Meta Label" },
  { id: 5, key: "trade_score", title: "Score" },
  { id: 6, key: "portfolio_risk", title: "Portfolio / Risk" },
  { id: 7, key: "execution", title: "Broker" },
];

const accepted = (decision: string) => decision === "accepted" || decision === "pipeline_approved";

function stageValue(item: LiveTickerFlowItem, stageId: number) {
  if (stageId === 3 && item.ml_prob !== null) return item.ml_prob.toFixed(2);
  if (stageId === 4 && item.meta_prob !== null) return item.meta_prob.toFixed(2);
  if (stageId === 5 && item.trade_score !== null) return item.trade_score.toFixed(0);
  return null;
}

export function TickerFlowVisualizer() {
  const [stream, setStream] = useState<LiveTickerFlowItem[]>([]);
  const [lastTickTime, setLastTickTime] = useState<Date>(new Date());
  const logs = useStore((s) => s.logs);

  const fetchLiveStream = async () => {
    try {
      const data = await api.shadowLedgerCandidates(60);
      if (!Array.isArray(data)) return;
      const parsed: LiveTickerFlowItem[] = data.map((s, idx) => {
        const payload = s.payload && typeof s.payload === "object" ? s.payload : {};
        const gateEv = s.gate_evidence_json || payload.gate_evidence_json || {};
        const decision = (s.final_decision || payload.final_decision || "pending").toLowerCase();
        const stage = s.rejection_stage || payload.rejection_stage || gateEv.first_decisive_gate || null;
        const num = (v: unknown) => v === null || v === undefined || v === "" ? null : Number(v);
        let step: number | null = 7;
        if (decision === "rejected") {
          if (stage === "ml_gate") step = 3;
          else if (stage === "meta_label_gate") step = 4;
          else if (stage === "trade_score_gate") step = 5;
          else if (["portfolio_gate", "risk_gate", "behavioral_safety_gate"].includes(stage)) step = 6;
          else step = null;
        } else if (decision === "pending") step = 2;
        const rawTs = s.timestamp || s.ts || payload.timestamp || "";
        const parsedTs = rawTs ? new Date(rawTs).getTime() : NaN;
        return {
          id: s.candidate_id || s.id || `flow-${idx}`,
          symbol: s.symbol || "N/A",
          strategy: s.strategy || payload.strategy || "Unknown Strategy",
          direction: (s.direction || payload.direction || "N/A").toUpperCase(),
          timestamp: rawTs,
          tsMs: Number.isNaN(parsedTs) ? null : parsedTs,
          final_decision: decision,
          rejection_stage: stage,
          rejection_reason: s.rejection_reason || payload.rejection_reason || null,
          ml_prob: num(s.ml_confidence ?? s.ml_probability ?? payload.ml_confidence ?? payload.ml_probability ?? gateEv.ml_probability),
          meta_prob: num(s.meta_probability ?? payload.meta_probability ?? gateEv.meta_probability),
          trade_score: num(s.trade_score ?? payload.trade_score ?? gateEv.trade_score),
          ml_threshold: num(s.ml_threshold ?? payload.ml_threshold ?? gateEv.applied_ml_threshold ?? gateEv.ml_threshold),
          step_reached: step,
        };
      });
      parsed.sort((a, b) => (b.tsMs ?? -1) - (a.tsMs ?? -1));
      setStream(parsed);
      setLastTickTime(new Date());
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
    if (logs.length > 0) fetchLiveStream();
  }, [logs.length]);

  return (
    <div style={{ padding: 14, color: "#e2e8f0", background: "#0b1329", borderRadius: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12, color: "#94a3b8", fontSize: 12 }}>
        <span>LIVE PIPELINE FLOW</span><span>Updated {lastTickTime.toLocaleTimeString()}</span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {stream.slice(0, 30).map((item) => {
          const isAccepted = accepted(item.final_decision);
          return (
            <div key={item.id} style={{ display: "grid", gridTemplateColumns: "235px minmax(650px, 1fr) 310px", gap: 18, alignItems: "center", padding: "14px 16px", background: "#111c35", border: "1px solid #1e293b", borderRadius: 10, overflowX: "auto" }}>
              <div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <b style={{ fontSize: 18 }}>{item.symbol}</b>
                  <span style={{ padding: "2px 7px", borderRadius: 5, fontSize: 12, fontWeight: 800, color: item.direction === "LONG" ? "#4ade80" : "#f87171", background: item.direction === "LONG" ? "#14532d66" : "#7f1d1d66" }}>{item.direction}</span>
                </div>
                <div style={{ marginTop: 4, color: "#cbd5e1" }}>{item.strategy}</div>
                <div style={{ color: "#64748b", fontSize: 12 }}>{item.timestamp ? fmtTime(item.timestamp) : ""}</div>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: `repeat(${STAGES.length}, minmax(86px, 1fr))`, gap: 8 }}>
                {STAGES.map((stage) => {
                  const rejectedHere = item.final_decision === "rejected" && item.step_reached === stage.id;
                  const passedHere = item.step_reached !== null && stage.id < item.step_reached;
                  const submitted = stage.id === 7 && isAccepted;
                  const reachedPending = item.final_decision === "pending" && item.step_reached === stage.id;
                  const inactive = !rejectedHere && !passedHere && !submitted && !reachedPending;
                  const value = stageValue(item, stage.id);
                  const color = rejectedHere ? "#f87171" : submitted ? "#60a5fa" : passedHere ? "#4ade80" : "#64748b";
                  const border = rejectedHere ? "#ef4444" : submitted ? "#2563eb" : passedHere ? "#15803d" : "#25334a";
                  const background = rejectedHere ? "#7f1d1d25" : submitted ? "#1d4ed833" : passedHere ? "#14532d30" : "#0b1220";
                  return (
                    <div key={stage.id} style={{ opacity: inactive ? 0.34 : 1 }}>
                      <div style={{ color: inactive ? "#64748b" : "#aebbd0", fontSize: 11, fontWeight: 700, marginBottom: 5, textAlign: "center", whiteSpace: "nowrap" }}>{stage.title}</div>
                      <div style={{ minHeight: 52, border: `1px solid ${border}`, background, borderRadius: 7, display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", color, fontWeight: 800, fontSize: 12 }}>
                        {!inactive && (rejectedHere ? "REJECT" : submitted ? "SUBMITTED" : passedHere ? "PASS" : "EVALUATING")}
                        {!inactive && value && <span style={{ marginTop: 2, fontSize: 11, fontWeight: 600 }}>{value}</span>}
                      </div>
                    </div>
                  );
                })}
              </div>

              <div>
                <div style={{ fontWeight: 800, color: isAccepted ? "#4ade80" : item.final_decision === "rejected" ? "#f87171" : "#facc15" }}>
                  {isAccepted ? "APPROVED · ORDER SENT" : item.final_decision === "rejected" ? `REJECTED · ${(item.rejection_stage || "GATE").replaceAll("_", " ").toUpperCase()}` : "EVALUATING"}
                </div>
                {item.final_decision === "rejected" && item.rejection_reason && <div style={{ marginTop: 5, color: "#94a3b8", fontSize: 12 }}>{item.rejection_reason}</div>}
                {item.rejection_stage === "ml_gate" && item.ml_prob !== null && item.ml_threshold !== null && <div style={{ marginTop: 3, color: "#64748b", fontSize: 12 }}>ML confidence {item.ml_prob.toFixed(4)} &lt; threshold {item.ml_threshold.toFixed(4)}</div>}
              </div>
            </div>
          );
        })}
        {stream.length === 0 && <div style={{ padding: 28, textAlign: "center", color: "#64748b" }}>No pipeline activity yet</div>}
      </div>
    </div>
  );
}
