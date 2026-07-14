import { useState } from "react";
import { api } from "../api";
import { useStore } from "../store";

export function TrainerPanel() {
  const status = useStore((s) => s.status);
  const showToast = useStore((s) => s.showToast);
  const [running, setRunning] = useState(false);

  const trainer = status?.trainer;
  const isTraining = trainer?.training_in_progress ?? false;
  const lastTrained = trainer?.last_trained
    ? new Date(trainer.last_trained).toLocaleString()
    : "Never";
  const lastResult = trainer?.last_result ?? {};
  const specialists = lastResult.specialists ?? {};
  const specialistCount = Object.keys(specialists).length;
  const bestSpecialist = Object.entries(specialists)
    .map(([symbol, result]) => {
      // The backend returns a result object for each specialist, not a raw
      // AUC number.  Retained models expose incumbent_auc; newly fitted ones
      // expose candidate_auc.  Support the old numeric payload too.
      const auc = typeof result === "number"
        ? result
        : result.incumbent_auc ?? result.candidate_auc ?? result.auc;
      return { symbol, auc };
    })
    .filter((item): item is { symbol: string; auc: number } =>
      typeof item.auc === "number" && Number.isFinite(item.auc),
    )
    .sort((a, b) => b.auc - a.auc)[0] ?? null;

  async function triggerTraining() {
    setRunning(true);
    try {
      await api.runTrainer();
      showToast("info", "Training cycle started in background");
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="panel trainer-panel">
      <div className="panel-header">
        <span className="panel-title">
          🧠 Continuous Learning
          {isTraining && (
            <span className="training-badge pulse">Training...</span>
          )}
        </span>
        <button
          className={`btn sm ${isTraining || running ? "ghost" : "primary"}`}
          onClick={triggerTraining}
          disabled={isTraining || running}
          id="btn-run-trainer"
          title="Trigger a full training cycle now"
        >
          {isTraining || running ? "⏳ Training…" : "▶ Train Now"}
        </button>
      </div>

      <div className="trainer-grid">
        <div className="trainer-stat">
          <div className="trainer-stat-label">Last trained</div>
          <div className="trainer-stat-value">{lastTrained}</div>
        </div>
        {lastResult.elapsed_sec != null && (
          <div className="trainer-stat">
            <div className="trainer-stat-label">Training time</div>
            <div className="trainer-stat-value">{lastResult.elapsed_sec}s</div>
          </div>
        )}
        {lastResult.symbols_trained != null && (
          <div className="trainer-stat">
            <div className="trainer-stat-label">Symbols trained</div>
            <div className="trainer-stat-value">{lastResult.symbols_trained}</div>
          </div>
        )}
        {lastResult.global_auc != null && (
          <div className="trainer-stat">
            <div className="trainer-stat-label">Global AUC</div>
            <div
              className={`trainer-stat-value num ${
                lastResult.global_auc >= 0.55 ? "pos" : lastResult.global_auc > 0.5 ? "" : "neg"
              }`}
            >
              {lastResult.global_auc.toFixed(3)}
            </div>
          </div>
        )}
        {specialistCount > 0 && (
          <div className="trainer-stat">
            <div className="trainer-stat-label">Specialists saved</div>
            <div className="trainer-stat-value">{specialistCount}</div>
          </div>
        )}
        {bestSpecialist && (
          <div className="trainer-stat">
            <div className="trainer-stat-label">Best specialist</div>
            <div className="trainer-stat-value pos num">
              {bestSpecialist.symbol} ({bestSpecialist.auc.toFixed(3)})
            </div>
          </div>
        )}
      </div>

      <div className="trainer-hint muted">
        Auto-retrains daily at 16:15 ET. Promotes models only if AUC improves by ≥ 0.2%.
        Per-symbol specialist models are saved and used for inference automatically.
      </div>
    </div>
  );
}
