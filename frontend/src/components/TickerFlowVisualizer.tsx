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
  market_regime: string | null;
  ml_threshold: number | null;
  step_reached: number | null;
}

const STAGES = [
  { id: 1, label: "Scan", detail: "Universe" },
  { id: 2, label: "Strategy", detail: "Signal" },
  { id: 3, label: "ML", detail: "Probability" },
  { id: 4, label: "Meta", detail: "Quality" },
  { id: 5, label: "Score", detail: "Setup" },
  { id: 6, label: "Portfolio / Risk", detail: "Capital" },
  { id: 7, label: "Execution", detail: "Broker" },
];

function isAccepted(item: LiveTickerFlowItem) {
  return item.final_decision === "accepted" || item.final_decision === "pipeline_approved";
}

function stageForRejection(stage: string | null): number | null {
  if (stage === "ml_gate") return 3;
  if (stage === "meta_label_gate") return 4;
  if (stage === "trade_score_gate") return 5;
  if (stage === "portfolio_gate" || stage === "risk_gate" || stage === "behavioral_safety_gate") return 6;
  return null;
}

function metricForStage(item: LiveTickerFlowItem, stageId: number) {
  if (stageId === 3) {
    const value = item.ml_prob == null ? "—" : `${(item.ml_prob * 100).toFixed(1)}%`;
    const threshold = item.ml_threshold == null ? "" : ` / ${(item.ml_threshold * 100).toFixed(0)}%`;
    return `${value}${threshold}`;
  }
  if (stageId === 4) return item.meta_prob == null ? "—" : `${(item.meta_prob * 100).toFixed(1)}%`;
  if (stageId === 5) return item.trade_score == null ? "—" : item.trade_score.toFixed(1);
  if (stageId === 6) return item.market_regime ?? "—";
  if (stageId === 7) return isAccepted(item) ? "Broker" : "—";
  return "";
}

export function TickerFlowVisualizer() {
  const [stream, setStream] = useState<LiveTickerFlowItem[]>([]);
  const [lastTickTime, setLastTickTime] = useState<Date>(new Date());
  const logs = useStore((s) => s.logs);

  async function fetchLiveStream() {
    try {
      const data = await api.shadowLedgerCandidates(60);
      if (!Array.isArray(data)) return;
      const parsed: LiveTickerFlowItem[] = data.map((s, idx) => {
        const payload = s.payload && typeof s.payload === "object" ? s.payload : {};
        const gateEv = s.gate_evidence_json || payload.gate_evidence_json || {};
        const decision = String(s.final_decision ?? payload.final_decision ?? "pending").toLowerCase();
        const rejectionStage = s.rejection_stage || payload.rejection_stage || gateEv.first_decisive_gate || null;
        const rawTs = s.timestamp || s.ts || payload.timestamp || "";
        const parsedTs = rawTs ? new Date(rawTs).getTime() : NaN;
        const numberOrNull = (value: unknown) => value == null || Number.isNaN(Number(value)) ? null : Number(value);
        return {
          id: s.candidate_id || s.id || `flow-${idx}`,
          symbol: s.symbol || "N/A",
          strategy: s.strategy || payload.strategy || "Unknown Strategy",
          direction: String(s.direction || payload.direction || "N/A").toUpperCase(),
          timestamp: rawTs,
          tsMs: Number.isNaN(parsedTs) ? null : parsedTs,
          final_decision: decision,
          rejection_stage: rejectionStage,
          rejection_reason: s.rejection_reason || payload.rejection_reason || null,
          ml_prob: numberOrNull(s.ml_confidence ?? s.ml_probability ?? payload.ml_confidence ?? payload.ml_probability ?? gateEv.ml_probability),
          meta_prob: numberOrNull(s.meta_probability ?? payload.meta_probability ?? gateEv.meta_probability),
          trade_score: numberOrNull(s.trade_score ?? payload.trade_score ?? gateEv.trade_score),
          market_regime: String(s.market_regime || payload.market_regime || gateEv.market_regime || "").toUpperCase() || null,
          ml_threshold: numberOrNull(s.ml_threshold ?? payload.ml_threshold ?? gateEv.applied_ml_threshold ?? gateEv.ml_threshold),
          step_reached: decision === "rejected" ? stageForRejection(rejectionStage) : decision === "pending" ? 2 : 7,
        };
      });
      parsed.sort((a, b) => (b.tsMs ?? -1) - (a.tsMs ?? -1));
      setStream(parsed);
      setLastTickTime(new Date());
    } catch (err) {
      console.error("Failed to load live flow stream", err);
    }
  }

  useEffect(() => {
    fetchLiveStream();
    const timer = setInterval(fetchLiveStream, 2000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (logs.length > 0) fetchLiveStream();
  }, [logs.length]);

  const accepted = stream.filter(isAccepted).length;
  const rejected = stream.filter((x) => x.final_decision === "rejected").length;
  const pending = Math.max(0, stream.length - accepted - rejected);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", flexWrap: "wrap" }}>
        <div>
          <b>Live decision journey</b>
          <div className="muted" style={{ fontSize: 11 }}>Scan → strategy → ML → meta → score → portfolio/risk → execution</div>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <span className="badge">{stream.length} sampled</span>
          <span className="badge success">{accepted} accepted</span>
          <span className="badge" style={{ color: "var(--danger)" }}>{rejected} vetoed</span>
          {pending > 0 && <span className="badge">{pending} evaluating</span>}
          <span className="faint num" style={{ fontSize: 11 }}>Updated {lastTickTime.toLocaleTimeString()}</span>
        </div>
      </div>

      <div style={{ overflowX: "auto", border: "1px solid var(--border)", borderRadius: 8 }}>
        <table className="grid" style={{ minWidth: 980 }}>
          <thead>
            <tr>
              <th>Candidate</th>
              {STAGES.map((stage) => <th key={stage.id}>{stage.label}<div className="faint" style={{ fontWeight: 400, fontSize: 10 }}>{stage.detail}</div></th>)}
              <th>Decision</th>
            </tr>
          </thead>
          <tbody>
            {stream.slice(0, 30).map((item) => (
              <tr key={item.id}>
                <td style={{ minWidth: 150 }}>
                  <div><b>{item.symbol}</b> <span className={`side-tag ${item.direction.toLowerCase()}`}>{item.direction}</span></div>
                  <div className="muted" style={{ fontSize: 11 }}>{item.strategy}</div>
                  <div className="faint num" style={{ fontSize: 10 }}>{item.timestamp ? fmtTime(item.timestamp) : "—"}</div>
                </td>
                {STAGES.map((stage) => {
                  const stoppedHere = item.final_decision === "rejected" && item.step_reached === stage.id;
                  const reached = item.step_reached != null && stage.id <= item.step_reached;
                  const passed = reached && !stoppedHere && (stage.id < (item.step_reached ?? 0) || (stage.id === 7 && isAccepted(item)));
                  return (
                    <td key={stage.id} style={{ minWidth: stage.id === 6 ? 120 : 82 }}>
                      <div className={stoppedHere ? "neg" : passed ? "pos" : reached ? "" : "faint"} style={{ fontWeight: 600, fontSize: 11 }}>
                        {stoppedHere ? "VETO" : passed ? "PASS" : reached ? "CHECK" : "—"}
                      </div>
                      {metricForStage(item, stage.id) && <div className="faint num" style={{ fontSize: 10 }}>{metricForStage(item, stage.id)}</div>}
                    </td>
                  );
                })}
                <td style={{ minWidth: 190 }}>
                  {isAccepted(item) ? (
                    <span className="badge success">Executed</span>
                  ) : item.final_decision === "rejected" ? (
                    <>
                      <span className="badge" style={{ color: "var(--danger)" }}>Rejected</span>
                      <div className="muted" style={{ marginTop: 4, fontSize: 10 }}>{item.rejection_stage || "Unknown gate"}</div>
                      {item.rejection_reason && <div className="faint" title={item.rejection_reason} style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 10 }}>{item.rejection_reason}</div>}
                    </>
                  ) : (
                    <span className="badge">Evaluating</span>
                  )}
                </td>
              </tr>
            ))}
            {stream.length === 0 && <tr><td colSpan={9} className="muted">No pipeline candidates yet</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
