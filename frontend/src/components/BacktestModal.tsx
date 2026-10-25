import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtDateTime } from "../format";
import { useStore } from "../store";
import type { BacktestResult, BacktestSummary } from "../types";
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
  const [rangeMode, setRangeMode] = useState<"days" | "dates">("days");
  const [days, setDays] = useState(5);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [mlGate, setMlGate] = useState(true);
  const [cash, setCash] = useState(100000);
  const [disableScanner, setDisableScanner] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  
  const [activeTab, setActiveTab] = useState<"new" | "history">("new");
  const [history, setHistory] = useState<BacktestSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [draftSymbol, setDraftSymbol] = useState("");

  useEffect(() => {
    if (activeTab === "history" && history.length === 0) {
      setLoadingHistory(true);
      api.backtests()
        .then(setHistory)
        .catch(e => showToast("error", (e as Error).message))
        .finally(() => setLoadingHistory(false));
    }
  }, [activeTab]);

  async function loadRun(id: string) {
    setRunning(true);
    setResult(null);
    try {
      const res = await api.backtest(id);
      setResult(res);
      setActiveTab("new");
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  function toggleSymbol(sym: string) {
    setSymbols((prev) => (prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym]));
  }

  async function run() {
    if (symbols.length === 0) {
      showToast("error", "Select at least one symbol");
      return;
    }
    setRunning(true);
    setResult(null);
    try {
      const payload: any = {
        symbols,
        starting_cash: cash,
        use_ml_gate: mlGate,
        disable_scanner: disableScanner,
      };
      if (rangeMode === "days") {
        payload.days = days;
      } else {
        if (!startDate || !endDate) {
          showToast("error", "Please select both start and end dates");
          setRunning(false);
          return;
        }
        payload.start_date = startDate;
        payload.end_date = endDate;
      }
      const res = await api.runBacktest(payload);
      setResult(res);
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <Modal title="Backtest" onClose={props.onClose}>
      <div className="modal-body">
        <div style={{ display: "flex", gap: 16, marginBottom: 16, borderBottom: "1px solid var(--border)" }}>
          <button
            style={{ padding: "8px 0", background: "none", border: "none", color: activeTab === "new" ? "var(--fg)" : "var(--muted)", fontWeight: activeTab === "new" ? "bold" : "normal", cursor: "pointer", borderBottom: activeTab === "new" ? "2px solid var(--primary)" : "2px solid transparent" }}
            onClick={() => setActiveTab("new")}
          >
            New Run
          </button>
          <button
            style={{ padding: "8px 0", background: "none", border: "none", color: activeTab === "history" ? "var(--fg)" : "var(--muted)", fontWeight: activeTab === "history" ? "bold" : "normal", cursor: "pointer", borderBottom: activeTab === "history" ? "2px solid var(--primary)" : "2px solid transparent" }}
            onClick={() => setActiveTab("history")}
          >
            History
          </button>
        </div>

        {activeTab === "new" ? (
          <>
            <div className="form-grid">
          <div className="field" style={{ gridColumn: "1 / -1" }}>
            <label>Symbols</label>
            <div className="row" style={{ flexWrap: "wrap", gap: "8px", alignItems: "center" }}>
              {Array.from(new Set([...watchlist, ...symbols])).map((sym) => (
                <button
                  key={sym}
                  className={`btn sm ${symbols.includes(sym) ? "primary" : "ghost"}`}
                  onClick={() => toggleSymbol(sym)}
                >
                  {sym}
                </button>
              ))}
              <input
                type="text"
                placeholder="Add symbol…"
                value={draftSymbol}
                onChange={(e) => setDraftSymbol(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    const newSyms = draftSymbol
                      .split(",")
                      .map((s) => s.trim().toUpperCase())
                      .filter((s) => s && !symbols.includes(s));
                    if (newSyms.length > 0) {
                      setSymbols([...symbols, ...newSyms]);
                    }
                    setDraftSymbol("");
                  }
                }}
                style={{ width: 300, padding: "4px 8px", border: "1px solid var(--border)", borderRadius: 4, background: "var(--bg)", color: "var(--fg)" }}
              />
            </div>
          </div>
          <div className="field">
            <label>Mode</label>
            <select className="num" value={rangeMode} onChange={(e) => setRangeMode(e.target.value as any)}>
              <option value="days">Lookback (days)</option>
              <option value="dates">Date Range</option>
            </select>
          </div>
          {rangeMode === "days" ? (
            <div className="field">
              <label>Lookback (days)</label>
              <input
                type="number" className="num" min={1} max={90} value={days}
                onChange={(e) => setDays(Number(e.target.value))}
              />
              <span className="hint">Demo/synthetic: any range. Yahoo 1m data: ≤7 days.</span>
            </div>
          ) : (
            <div className="field row" style={{ gap: 8 }}>
              <div style={{ flex: 1 }}>
                <label>Start Date</label>
                <input type="date" className="num" style={{ width: "100%" }} value={startDate} onChange={(e) => setStartDate(e.target.value)} />
              </div>
              <div style={{ flex: 1 }}>
                <label>End Date</label>
                <input type="date" className="num" style={{ width: "100%" }} value={endDate} onChange={(e) => setEndDate(e.target.value)} />
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
              <button className={`switch ${mlGate ? "on" : ""}`} onClick={() => setMlGate(!mlGate)} />
              <span className="muted">{mlGate ? "ML/heuristic gate on" : "raw strategy signals"}</span>
            </div>
          </div>
          <div className="field">
            <label>Disable scanner</label>
            <div className="row">
              <button className={`switch ${disableScanner ? "on" : ""}`} onClick={() => setDisableScanner(!disableScanner)} />
              <span className="muted">{disableScanner ? "Scanner off" : "Dynamic scanning on"}</span>
            </div>
          </div>
        </div>

        {(running || progress != null) && (
          <div>
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
                    <th>Entry Time</th><th>Exit Time</th><th>Symbol</th><th>Side</th><th className="r">Qty</th>
                    <th className="r">Entry</th><th className="r">Exit</th><th className="r">P&L</th>
                    <th className="r">R</th><th>Strategy</th><th>Exit</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((t) => (
                    <tr key={t.id}>
                      <td className="num faint" style={{ whiteSpace: "nowrap" }}>{fmtDateTime(t.entry_at)}</td>
                      <td className="num faint" style={{ whiteSpace: "nowrap" }}>{fmtDateTime(t.exit_at)}</td>
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
                    <tr><td colSpan={11} className="muted">No trades in this run</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
          </>
        ) : (
          <div className="history-list">
            {loadingHistory && <div className="muted">Loading history...</div>}
            {!loadingHistory && history.length === 0 && <div className="muted">No backtest history found.</div>}
            {!loadingHistory && history.length > 0 && (
              <table className="grid">
                <thead>
                  <tr>
                    <th>Time</th><th>Symbols</th><th>Days</th><th className="r">Return</th>
                    <th className="r">Max DD</th><th className="r">Trades</th><th className="r">Win Rate</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {history.map(h => (
                    <tr key={h.id}>
                      <td className="muted">{fmtDateTime(h.created_at)}</td>
                      <td>{h.symbols.join(", ")}</td>
                      <td>{h.days}</td>
                      <td className={`r num ${h.total_return_pct && h.total_return_pct >= 0 ? "pos" : "neg"}`}>{h.total_return_pct?.toFixed(2) ?? "—"}%</td>
                      <td className="r num">{h.max_drawdown_pct?.toFixed(2) ?? "—"}%</td>
                      <td className="r num">{h.total_trades ?? 0}</td>
                      <td className="r num">{h.win_rate ? (h.win_rate * 100).toFixed(1) + "%" : "—"}</td>
                      <td className="r"><button className="btn sm ghost" onClick={() => loadRun(h.id)}>View</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
      <div className="modal-foot">
        <button className="btn ghost" onClick={props.onClose}>Close</button>
        <button className="btn primary" onClick={run} disabled={running}>
          {running ? "Running…" : "Run backtest"}
        </button>
      </div>
    </Modal>
  );
}
