import { useState } from "react";
import { api } from "../api";
import { fmtSigned, fmtTime, pnlClass } from "../format";
import { useStore } from "../store";
import { EquityChart } from "./EquityChart";

export function SidePanel() {
  return (
    <div className="side">
      <InsightsPanel />
      <PerformancePanel />
      <StrategiesPanel />
    </div>
  );
}

export function InsightsPanel() {
  const insights = useStore((s) => s.insights);
  const model = useStore((s) => s.status?.model);
  const showToast = useStore((s) => s.showToast);
  const [training, setTraining] = useState(false);

  async function train() {
    setTraining(true);
    showToast("info", "Training AI model — this can take a minute…");
    try {
      const info = await api.trainModel(null, 30);
      showToast("info", `Model trained (AUC ${info.validation_auc ?? "n/a"})`);
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setTraining(false);
    }
  }

  return (
    <div className="panel grow">
      <div className="panel-head">
        <span>AI Insights</span>
        <span className="faint" style={{ textTransform: "none", letterSpacing: 0 }}>
          {model?.trained ? "ML model" : "heuristic"}
        </span>
      </div>
      <div className="panel-body">
        {!model?.trained && (
          <div className="ai-banner">
            <span>Signals gated by transparent heuristic. Train the ML model on recent data for a data-driven gate.</span>
            <button className="btn sm primary" onClick={train} disabled={training}>
              {training ? "Training…" : "Train"}
            </button>
          </div>
        )}
        {insights.length === 0 && <div className="empty-state">Insights appear when the engine runs</div>}
        {insights.map((i) => (
          <div className={`insight ${i.severity}`} key={i.id}>
            <div className="t">
              <span>{i.title}</span>
              <span className="time num">{fmtTime(i.ts)}</span>
            </div>
            <div className="d">{i.detail}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function PerformancePanel() {
  const metrics = useStore((s) => s.metrics);
  const trades = useStore((s) => s.trades);
  const m = metrics?.metrics ?? {};
  const wins = trades.filter((t) => t.pnl > 0);
  const losses = trades.filter((t) => t.pnl <= 0);
  const grossWin = wins.reduce((sum, t) => sum + t.pnl, 0);
  const grossLoss = Math.abs(losses.reduce((sum, t) => sum + t.pnl, 0));
  const realizedPnl = trades.reduce((sum, t) => sum + t.pnl, 0);
  const winRate = trades.length ? (wins.length / trades.length) * 100 : 0;
  const profitFactor = grossLoss > 0 ? grossWin / grossLoss : null;

  return (
    <div className="panel" style={{ flexShrink: 0 }}>
      <div className="panel-head"><span>Performance</span></div>
      <EquityChart curve={metrics?.equity_curve ?? []} />
      <div className="kv-grid">
        <div className="kv">
          <div className="k">Trades</div>
          <div className="v num">{trades.length}</div>
        </div>
        <div className="kv">
          <div className="k">Win rate</div>
          <div className="v num">{trades.length ? `${winRate.toFixed(1)}%` : "—"}</div>
        </div>
        <div className="kv">
          <div className="k">Realized P&amp;L</div>
          <div className={`v num ${pnlClass(realizedPnl)}`}>
            {trades.length ? fmtSigned(realizedPnl) : "—"}
          </div>
        </div>
        <div className="kv">
          <div className="k">Profit factor</div>
          <div className="v num">{profitFactor != null ? profitFactor.toFixed(2) : "—"}</div>
        </div>
        <div className="kv">
          <div className="k">Max drawdown</div>
          <div className="v num">{m.max_drawdown_pct != null ? `${m.max_drawdown_pct}%` : "—"}</div>
        </div>
        <div className="kv">
          <div className="k">Sharpe</div>
          <div className="v num">{m.sharpe ?? "—"}</div>
        </div>
      </div>
    </div>
  );
}

interface StrategyRowProps {
  strategy: any;
  onToggle: (name: string, enabled: boolean) => void;
  onSaveParams: (name: string, params: Record<string, any>) => Promise<void>;
}

function StrategyRow({ strategy, onToggle, onSaveParams }: StrategyRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [params, setParams] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const [k, v] of Object.entries(strategy.params || {})) {
      initial[k] = String(v);
    }
    return initial;
  });
  const [saving, setSaving] = useState(false);

  function handleParamChange(key: string, val: string) {
    setParams((prev) => ({ ...prev, [key]: val }));
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    const typedParams: Record<string, any> = {};
    for (const [k, v] of Object.entries(params)) {
      const num = Number(v);
      typedParams[k] = isNaN(num) ? v : num;
    }
    try {
      await onSaveParams(strategy.name, typedParams);
    } finally {
      setSaving(false);
    }
  }

  const helpTexts: Record<string, string> = {
    range_minutes: "Duration of the opening range in minutes. Recommended: 15 to 30.",
    entry_cutoff_minute: "Stop taking new entries after this many minutes from open. Recommended: 180 (12:30 PM ET) to avoid lunchtime chop.",
    breakout_buffer_bps: "Basis points price must clear the opening range to trigger breakout. Recommended: 5 (0.05%) to 10 (0.10%).",
    atr_buffer_mult: "If > 0, overrides breakout_buffer_bps with (multiplier * ATR). Recommended: 0.0 (disabled) or 0.2 to 0.5.",
    volume_z_min: "Minimum volume z-score for breakout confirmation. Recommended: 0.5 (above average) to 1.0 (strong volume).",
    atr_stop_mult: "Multiplier of ATR to determine the stop loss distance. Recommended: 1.5 to 2.5.",
    reward_risk: "Target reward-to-risk ratio. Recommended: 2.0 (profit target is twice the stop loss).",
    trailing_stop_atr_mult: "If > 0, activates a dynamic trailing stop based on this multiplier of ATR. Recommended: 2.5 to 3.5.",
    width_percentile_lookback: "Bands squeeze lookback window length in bars. Recommended: 100.",
    width_percentile_threshold: "Percentile below which bands width is considered a squeeze. Recommended: 0.15.",
    vol_z_min: "Minimum volume z-score to confirm squeeze breakout. Recommended: 1.0.",
    ema_fast: "Period of the fast exponential moving average. Recommended: 9.",
    ema_slow: "Period of the slow exponential moving average. Recommended: 21.",
    rsi_period: "Period of the Relative Strength Index. Recommended: 14 (or 2 for short-term mean reversion).",
    rsi_exit_high: "RSI level to exit long positions. Recommended: 70 to 75.",
    rsi_exit_low: "RSI level to exit short positions. Recommended: 25 to 30.",
    rsi_lower: "RSI level to trigger oversold long entry. Recommended: 10 to 15.",
    rsi_upper: "RSI level to trigger overbought short entry. Recommended: 85 to 90.",
    vwap_dev_threshold: "Standard deviations from VWAP to trigger reversion. Recommended: 2.0.",
    rsi_extreme_high: "Overbought RSI level to trigger short reversion. Recommended: 70.",
    rsi_extreme_low: "Oversold RSI level to trigger long reversion. Recommended: 30.",
  };

  return (
    <div className="strat-row-container" style={{ borderBottom: "1px solid var(--border)" }}>
      <div className="strat-row">
        <div style={{ flex: 1, cursor: "pointer" }} onClick={() => setExpanded(!expanded)}>
          <div className="name">{strategy.name} <span style={{ fontSize: "9px", color: "var(--blue)" }}>{expanded ? "▲" : "▼"}</span></div>
          <div className="desc">{strategy.description}</div>
        </div>
        <label className="switch"><input type="checkbox" checked={strategy.enabled} onChange={(e) => onToggle(strategy.name, e.target.checked)} /><span className="slider" /></label>
      </div>
      {expanded && (
        <form onSubmit={handleSave} style={{ padding: "8px 12px", background: "var(--panel-2)", display: "flex", flexDirection: "column", gap: "8px" }}>
          {Object.entries(strategy.params || {}).map(([key]) => (
            <div key={key} style={{ display: "grid", gridTemplateColumns: "130px 1fr", alignItems: "center", gap: "8px" }}>
              <label style={{ fontSize: "10px", color: "var(--muted)" }} title={helpTexts[key] || ""}>{key}</label>
              <input className="num" style={{ width: "100%" }} value={params[key] ?? ""} onChange={(e) => handleParamChange(key, e.target.value)} title={helpTexts[key] || ""} />
            </div>
          ))}
          <button className="btn sm primary" type="submit" disabled={saving}>{saving ? "Saving…" : "Save parameters"}</button>
        </form>
      )}
    </div>
  );
}

export function StrategiesPanel() {
  const strategies = useStore((s) => s.strategies);
  const setStrategies = useStore((s) => s.setStrategies);
  const showToast = useStore((s) => s.showToast);

  async function toggle(name: string, enabled: boolean) {
    try {
      await api.updateStrategy(name, { enabled });
      setStrategies(strategies.map((s) => s.name === name ? { ...s, enabled } : s));
    } catch (e) { showToast("error", (e as Error).message); }
  }

  async function saveParams(name: string, params: Record<string, any>) {
    try {
      const updated = await api.updateStrategy(name, { params });
      setStrategies(strategies.map((s) => s.name === name ? updated : s));
      showToast("info", `${name} parameters saved`);
    } catch (e) { showToast("error", (e as Error).message); }
  }

  return <div className="panel"><div className="panel-head"><span>Strategies</span></div><div className="panel-body">{strategies.map((s) => <StrategyRow key={s.name} strategy={s} onToggle={toggle} onSaveParams={saveParams} />)}</div></div>;
}
