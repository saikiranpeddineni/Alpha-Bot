import { useState } from "react";
import { api } from "../api";
import { fmtDateTime } from "../format";
import { useStore } from "../store";
import type { BacktestResult } from "../types";
import { EquityChart } from "./EquityChart";
import { Modal } from "./Modal";

const METRIC_LABELS: [string, string, boolean][] = [
  // [key, label, colorize]
  ["total_return_pct", "Total return %", true],
  ["max_drawdown_pct", "Max drawdown %", false],
  ["sharpe", "Sharpe", false],
  ["sortino", "Sortino", false],
  ["win_rate", "Win rate %", false],
  ["profit_factor", "Profit factor", false],
  ["total_trades", "Trades", false],
  ["expectancy", "Expectancy $", true],
  ["avg_r", "Avg R", true],
  ["final_equity", "Final equity $", false],
];

export function BacktestModal(props: { onClose: () => void }) {
  const status = useStore((s) => s.status);
  const progress = useStore((s) => s.backtestProgress);
  const showToast = useStore((s) => s.showToast);

  const watchlist = status?.watchlist ?? [];
  const [symbols, setSymbols] = useState<string[]>(watchlist.slice(0, 3));
  const [days, setDays] = useState(5);
  const [rangeMode, setRangeMode] = useState<"days" | "dates">("days");
  const [startDate, setStartDate] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - 5);
    return d.toISOString().split("T")[0];
  });
  const [endDate, setEndDate] = useState(() => new Date().toISOString().split("T")[0]);
  const [mlGate, setMlGate] = useState(true);
  const [dynamicScanner, setDynamicScanner] = useState(true);
  const [ignoreTemporalLeakage, setIgnoreTemporalLeakage] = useState(false);
  const [cash, setCash] = useState(100000);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);

  // Validation batch runner state
  const [activeTab, setActiveTab] = useState<"single" | "batch">("single");
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchReport, setBatchReport] = useState<string | null>(null);

  // Batch runner custom configuration states
  const [batchRangeMode, setBatchRangeMode] = useState<"cache" | "dates">("cache");
  const [batchStartDate, setBatchStartDate] = useState("2026-07-06");
  const [batchEndDate, setBatchEndDate] = useState("2026-07-17");
  const [batchSymbols, setBatchSymbols] = useState<string[]>([]);

  function toggleSymbol(sym: string) {
    setSymbols((prev) => (prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym]));
  }

  function toggleBatchSymbol(sym: string) {
    setBatchSymbols((prev) => (prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym]));
  }

  async function run() {
    if (symbols.length === 0) {
      showToast("error", "Select at least one symbol");
      return;
    }
    setRunning(true);
    setResult(null);
    try {
      const res = await api.runBacktest({
        symbols,
        days,
        starting_cash: cash,
        use_ml_gate: mlGate,
        use_dynamic_scanner: dynamicScanner,
        ignore_temporal_leakage: ignoreTemporalLeakage,
        start_date: rangeMode === "dates" ? startDate : null,
        end_date: rangeMode === "dates" ? endDate : null,
      });
      setResult(res);
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  async function runBatchValidation() {
    setBatchRunning(true);
    setBatchReport(null);
    try {
      const res = await api.runProfitValidation({
        symbols: batchSymbols.length > 0 ? batchSymbols : undefined,
        start_date: batchRangeMode === "dates" ? batchStartDate : null,
        end_date: batchRangeMode === "dates" ? batchEndDate : null,
      });
      setBatchReport(res.report);
      showToast("info", "Validation batch suite run complete!");
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setBatchRunning(false);
    }
  }

  return (
    <Modal title="Backtest & Validation" onClose={props.onClose}>
      <div className="modal-body">
        <div className="tabs" style={{ marginBottom: 14 }}>
          <button
            type="button"
            className={activeTab === "single" ? "active" : ""}
            onClick={() => setActiveTab("single")}
          >
            Single Backtest
          </button>
          <button
            type="button"
            className={activeTab === "batch" ? "active" : ""}
            onClick={() => setActiveTab("batch")}
          >
            Comparative Validation (Batch)
          </button>
        </div>

        {activeTab === "single" ? (
          <>
            <div className="form-grid">
              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label>Symbols</label>
                <div className="row" style={{ flexWrap: "wrap" }}>
                  {watchlist.map((sym) => (
                    <button
                      key={sym}
                      type="button"
                      className={`btn sm ${symbols.includes(sym) ? "primary" : "ghost"}`}
                      onClick={() => toggleSymbol(sym)}
                    >
                      {sym}
                    </button>
                  ))}
                </div>
              </div>
              <div className="field">
                <label>Range mode</label>
                <div className="row" style={{ gap: "4px" }}>
                  <button
                    type="button"
                    className={`btn sm ${rangeMode === "days" ? "primary" : "ghost"}`}
                    onClick={() => setRangeMode("days")}
                  >
                    Lookback days
                  </button>
                  <button
                    type="button"
                    className={`btn sm ${rangeMode === "dates" ? "primary" : "ghost"}`}
                    onClick={() => setRangeMode("dates")}
                  >
                    Custom dates
                  </button>
                </div>
              </div>
              {rangeMode === "days" ? (
                <div className="field">
                  <label>Lookback (days)</label>
                  <input
                    type="number" className="num" min={1} max={90} value={days}
                    onChange={(e) => setDays(Number(e.target.value))}
                  />
                  <span className="hint">
                    Active provider: <b style={{ textTransform: "capitalize" }}>{status?.provider ?? "unknown"}</b>.{" "}
                    {(status?.provider ?? "yahoo") === "yahoo"
                      ? "Yahoo 1m data: ≤7 days."
                      : "Any range supported."}
                  </span>
                </div>
              ) : (
                <div className="field" style={{ display: "flex", gap: "8px" }}>
                  <div style={{ flex: 1 }}>
                    <label>Start date</label>
                    <input
                      type="date"
                      className="num"
                      value={startDate}
                      onChange={(e) => setStartDate(e.target.value)}
                      style={{ width: "100%", padding: "6px" }}
                    />
                  </div>
                  <div style={{ flex: 1 }}>
                    <label>End date</label>
                    <input
                      type="date"
                      className="num"
                      value={endDate}
                      onChange={(e) => setEndDate(e.target.value)}
                      style={{ width: "100%", padding: "6px" }}
                    />
                  </div>
                </div>
              )}
              <div className="field">
                <label>Starting cash</label>
                <input
                  type="number" className="num" min={1000} step={1000} value={cash}
                  onChange={(e) => setCash(Number(e.target.value))}
                />
              </div>
              <div className="field">
                <label>AI signal gate</label>
                <div className="row">
                  <button type="button" className={`switch ${mlGate ? "on" : ""}`} onClick={() => setMlGate(!mlGate)} />
                  <span className="muted">{mlGate ? "ML/heuristic gate on" : "raw strategy signals"}</span>
                </div>
              </div>
              <div className="field">
                <label>Dynamic Market Scanner</label>
                <div className="row">
                  <button type="button" className={`switch ${dynamicScanner ? "on" : ""}`} onClick={() => setDynamicScanner(!dynamicScanner)} />
                  <span className="muted">{dynamicScanner ? "Active pre-market rotation" : "Static watchlist"}</span>
                </div>
              </div>
              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label>Ignore Temporal Leakage</label>
                <div className="row" style={{ alignItems: "center" }}>
                  <button type="button" className={`switch ${ignoreTemporalLeakage ? "on" : ""}`} onClick={() => setIgnoreTemporalLeakage(!ignoreTemporalLeakage)} />
                  <span className="muted">{ignoreTemporalLeakage ? "Bypassed (Models will run)" : "Enforced (Look-ahead protection)"}</span>
                </div>
                <span className="hint">
                  Enables backtesting past dates using models trained in the future. <b>Warning:</b> May introduce look-ahead bias if the model has trained on your backtest data.
                </span>
              </div>
            </div>

            {(running || progress != null) && (
              <div style={{ marginTop: 12 }}>
                <div className="progress-bar">
                  <div className="fill" style={{ width: `${Math.round((progress ?? 0) * 100)}%` }} />
                </div>
                <div className="muted" style={{ fontSize: 11 }}>
                  Replaying bars through the live engine stack…
                </div>
              </div>
            )}

            {result && (
              <div style={{ marginTop: 14 }}>
                <div className="metric-grid">
                  {METRIC_LABELS.map(([key, label, colorize]) => {
                    const v = result.metrics[key];
                    const cls = colorize && typeof v === "number" ? (v >= 0 ? "pos" : "neg") : "";
                    return (
                      <div className="metric-card" key={key}>
                        <div className="k">{label}</div>
                        <div className={`v num ${cls}`}>{v ?? "—"}</div>
                      </div>
                    );
                  })}
                </div>
                <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "6px 4px" }}>
                  <EquityChart curve={result.equity_curve} height={180} />
                </div>
                {result.warnings.length > 0 && (
                  <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>
                    {result.warnings.map((w, i) => (
                      <div key={i}>⚠ {w}</div>
                    ))}
                  </div>
                )}
                <div style={{ maxHeight: 220, overflowY: "auto", marginTop: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
                  <table className="grid">
                    <thead>
                      <tr>
                        <th>Closed</th><th>Symbol</th><th>Side</th><th className="r">Qty</th>
                        <th className="r">Entry</th><th className="r">Exit</th><th className="r">P&L</th>
                        <th className="r">R</th><th>Strategy</th><th>Exit</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.map((t) => (
                        <tr key={t.id}>
                          <td className="num faint">{fmtDateTime(t.exit_at)}</td>
                          <td><b>{t.symbol}</b></td>
                          <td><span className={`side-tag ${t.side}`}>{t.side}</span></td>
                          <td className="r num">{t.qty}</td>
                          <td className="r num">{t.entry_price.toFixed(2)}</td>
                          <td className="r num">{t.exit_price.toFixed(2)}</td>
                          <td className={`r num ${t.pnl >= 0 ? "pos" : "neg"}`}>{t.pnl.toFixed(2)}</td>
                          <td className="r num">{t.r_multiple?.toFixed(1) ?? "—"}</td>
                          <td className="muted">{t.strategy}</td>
                          <td className="muted">{t.exit_reason}</td>
                        </tr>
                      ))}
                      {result.trades.length === 0 && (
                        <tr><td colSpan={10} className="muted">No trades in this run</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <p className="muted" style={{ marginTop: 0, fontSize: 13, lineHeight: 1.4 }}>
              Runs the offline <strong>Profit Optimization Validation & Comparative Testing Suite</strong> (<code>run_profit_validation.py</code>). This dynamically re-runs baseline models, simulates 5 ML thresholds (0.45 to 0.65), re-evaluates all rejected signals, compares multiple exit policies, and validates watchlist vs. scanner results.
            </p>

            <div className="form-grid" style={{ gap: 12, marginBottom: 4 }}>
              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label>Validation Symbols (select to filter, leave empty for all cached symbols)</label>
                <div className="row" style={{ flexWrap: "wrap" }}>
                  {watchlist.map((sym) => (
                    <button
                      key={sym}
                      type="button"
                      className={`btn sm ${batchSymbols.includes(sym) ? "primary" : "ghost"}`}
                      onClick={() => toggleBatchSymbol(sym)}
                    >
                      {sym}
                    </button>
                  ))}
                </div>
              </div>

              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label>Range mode</label>
                <div className="row" style={{ gap: "4px" }}>
                  <button
                    type="button"
                    className={`btn sm ${batchRangeMode === "cache" ? "primary" : "ghost"}`}
                    onClick={() => setBatchRangeMode("cache")}
                  >
                    Full cache bounds
                  </button>
                  <button
                    type="button"
                    className={`btn sm ${batchRangeMode === "dates" ? "primary" : "ghost"}`}
                    onClick={() => setBatchRangeMode("dates")}
                  >
                    Custom dates
                  </button>
                </div>
              </div>

              {batchRangeMode === "dates" && (
                <>
                  <div className="field">
                    <label>Start date</label>
                    <input
                      type="date"
                      className="num"
                      value={batchStartDate}
                      onChange={(e) => setBatchStartDate(e.target.value)}
                      style={{ width: "100%", padding: "6px" }}
                    />
                  </div>
                  <div className="field">
                    <label>End date</label>
                    <input
                      type="date"
                      className="num"
                      value={batchEndDate}
                      onChange={(e) => setBatchEndDate(e.target.value)}
                      style={{ width: "100%", padding: "6px" }}
                    />
                  </div>
                </>
              )}
            </div>

            {batchRunning && (
              <div style={{ margin: "8px 0" }}>
                <div className="progress-bar">
                  <div className="fill animate-pulse" style={{ width: "100%" }} />
                </div>
                <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                  Executing batch re-runs and calculating horizons (this takes ~30-45 seconds)…
                </div>
              </div>
            )}

            {batchReport && (
              <div style={{ marginTop: 6 }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                  <span className="label">Validation Results Report</span>
                  <span className="muted" style={{ fontSize: 10 }}>Saved to root & validation_reports/</span>
                </div>
                <div style={{
                  maxHeight: 220,
                  overflowY: "auto",
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  padding: 12,
                  background: "rgba(0,0,0,0.3)",
                  fontFamily: "monospace",
                  fontSize: 11,
                  whiteSpace: "pre-wrap",
                  lineHeight: 1.55,
                }}>
                  {batchReport}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
      <div className="modal-foot">
        <button type="button" className="btn ghost" onClick={props.onClose}>Close</button>
        {activeTab === "single" ? (
          <button type="button" className="btn primary" onClick={run} disabled={running}>
            {running ? "Running…" : "Run backtest"}
          </button>
        ) : (
          <button type="button" className="btn primary" onClick={runBatchValidation} disabled={batchRunning}>
            {batchRunning ? "Running Suite…" : "Run Validation Suite"}
          </button>
        )}
      </div>
    </Modal>
  );
}
