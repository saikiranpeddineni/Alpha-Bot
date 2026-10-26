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

interface ColumnDef {
  key: string;
  label: string;
  align?: "left" | "right";
  defaultWidth: number;
  minWidth: number;
}

const HISTORY_COLUMNS: ColumnDef[] = [
  { key: "time", label: "Time", defaultWidth: 125, minWidth: 85 },
  { key: "symbols", label: "Symbols", defaultWidth: 260, minWidth: 110 },
  { key: "days", label: "Days", defaultWidth: 55, minWidth: 40 },
  { key: "return", label: "Return", align: "right", defaultWidth: 80, minWidth: 55 },
  { key: "maxDd", label: "Max DD", align: "right", defaultWidth: 75, minWidth: 55 },
  { key: "trades", label: "Trades", align: "right", defaultWidth: 65, minWidth: 45 },
  { key: "winRate", label: "Win Rate", align: "right", defaultWidth: 75, minWidth: 55 },
  { key: "action", label: "", align: "right", defaultWidth: 65, minWidth: 50 },
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

  const [colWidths, setColWidths] = useState<Record<string, number>>(() => {
    const initial: Record<string, number> = {};
    HISTORY_COLUMNS.forEach((col) => {
      initial[col.key] = col.defaultWidth;
    });
    return initial;
  });
  const [resizingCol, setResizingCol] = useState<string | null>(null);

  function handleResizeStart(key: string, e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startWidth = colWidths[key] ?? 100;
    const colDef = HISTORY_COLUMNS.find((c) => c.key === key);
    const minW = colDef?.minWidth ?? 40;

    setResizingCol(key);

    const onMouseMove = (moveEvent: MouseEvent) => {
      const delta = moveEvent.clientX - startX;
      const newWidth = Math.max(minW, startWidth + delta);
      setColWidths((prev) => ({
        ...prev,
        [key]: newWidth,
      }));
    };

    const onMouseUp = () => {
      setResizingCol(null);
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  }

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

        {/* =============================================================================
            [MODIFIED 2026-10-26]: Defend Against Blank Screen on History Tab & Result View
            ORIGINAL INTENT: Assumed h.symbols is always an array, h.days is always defined,
            and result.warnings is always an array.
            REASON FOR CHANGE: If legacy or custom backtest summaries omit symbols or warnings,
            calling .join() or .length threw an uncaught TypeError in React, crashing the
            component tree and causing the screen to turn completely blank.
            SAFE ROLLBACK: Original JSX preserved below as comments.
            -----------------------------------------------------------------------------
            [PREVIOUS CODE - PRESERVED]:
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
            -----------------------------------------------------------------------------
            [NEW CODE]: */}
        {result && (
          <div style={{ marginTop: 14 }}>
            <div className="metric-grid">
              {METRIC_LABELS.map(([key, label, colorize]) => {
                const v = result.metrics ? result.metrics[key] : null;
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
              <EquityChart curve={result.equity_curve || []} height={180} />
            </div>
            {result.warnings && result.warnings.length > 0 && (
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
                  {(result.trades || []).map((t) => (
                    <tr key={t.id}>
                      <td className="num faint" style={{ whiteSpace: "nowrap" }}>{fmtDateTime(t.entry_at)}</td>
                      <td className="num faint" style={{ whiteSpace: "nowrap" }}>{fmtDateTime(t.exit_at)}</td>
                      <td><b>{t.symbol}</b></td>
                      <td><span className={`side-tag ${t.side}`}>{t.side}</span></td>
                      <td className="r num">{t.qty}</td>
                      <td className="r num">{typeof t.entry_price === "number" ? t.entry_price.toFixed(2) : t.entry_price}</td>
                      <td className="r num">{typeof t.exit_price === "number" ? t.exit_price.toFixed(2) : t.exit_price}</td>
                      <td className={`r num ${t.pnl >= 0 ? "pos" : "neg"}`}>{typeof t.pnl === "number" ? t.pnl.toFixed(2) : t.pnl}</td>
                      <td className="r num">{typeof t.r_multiple === "number" ? t.r_multiple.toFixed(1) : "—"}</td>
                      <td className="muted">{t.strategy}</td>
                      <td className="muted">{t.exit_reason}</td>
                    </tr>
                  ))}
                  {(!result.trades || result.trades.length === 0) && (
                    <tr><td colSpan={11} className="muted">No trades in this run</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
          </>
        ) : (
          <div className="history-list" style={{ overflowX: "auto", maxHeight: "60vh", overflowY: "auto" }}>
            {loadingHistory && <div className="muted">Loading history...</div>}
            {!loadingHistory && history.length === 0 && <div className="muted">No backtest history found.</div>}
            {!loadingHistory && history.length > 0 && (
              /* =============================================================================
                 [MODIFIED 2026-10-26]: Resizable Columns & Full Symbol Wrapping in History Tab
                 ORIGINAL INTENT: History table used fixed cells with whiteSpace: nowrap and
                 textOverflow: ellipsis on symbols (maxWidth: 220px), truncating long symbol lists.
                 REASON FOR CHANGE: User requested full visibility of all ticker symbols without
                 truncation, plus adjustable drag-handle column widths to customize table spacing
                 without expanding the outer modal size.
                 SAFE ROLLBACK: The original table implementation is preserved below as comments.
                 -----------------------------------------------------------------------------
                 [PREVIOUS CODE - PRESERVED]:
                 <table className="grid">
                   <thead>
                     <tr>
                       <th>Time</th><th>Symbols</th><th>Days</th><th className="r">Return</th>
                       <th className="r">Max DD</th><th className="r">Trades</th><th className="r">Win Rate</th><th></th>
                     </tr>
                   </thead>
                   <tbody>
                     {history.map(h => {
                       const symDisplay = Array.isArray(h.symbols) && h.symbols.length > 0
                         ? h.symbols.join(", ")
                         : (typeof h.symbols === "string" && h.symbols ? h.symbols : "All / Default");
                       const retDisplay = h.total_return_pct != null
                         ? `${h.total_return_pct >= 0 ? "+" : ""}${h.total_return_pct.toFixed(2)}%`
                         : "—";
                       const retClass = h.total_return_pct != null && h.total_return_pct >= 0 ? "pos" : "neg";
                       const ddDisplay = h.max_drawdown_pct != null ? `${h.max_drawdown_pct.toFixed(2)}%` : "—";
                       const wrDisplay = h.win_rate != null
                         ? (h.win_rate <= 1.0 ? (h.win_rate * 100).toFixed(1) : h.win_rate.toFixed(1)) + "%"
                         : "—";

                       return (
                         <tr key={h.id}>
                           <td className="muted">{fmtDateTime(h.created_at)}</td>
                           <td style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={symDisplay}>
                             {symDisplay}
                           </td>
                           <td>{h.days ?? "—"}</td>
                           <td className={`r num ${retClass}`}>{retDisplay}</td>
                           <td className="r num">{ddDisplay}</td>
                           <td className="r num">{h.total_trades ?? 0}</td>
                           <td className="r num">{wrDisplay}</td>
                           <td className="r"><button className="btn sm ghost" onClick={() => loadRun(h.id)}>View</button></td>
                         </tr>
                       );
                     })}
                   </tbody>
                 </table>
                 -----------------------------------------------------------------------------
                 [NEW CODE]:
              ============================================================================= */
              <table
                className="grid"
                style={{
                  tableLayout: "fixed",
                  width: "100%",
                  minWidth: Object.values(colWidths).reduce((a, b) => a + b, 0),
                  borderCollapse: "collapse",
                }}
              >
                <colgroup>
                  {HISTORY_COLUMNS.map((col) => (
                    <col key={col.key} style={{ width: colWidths[col.key] }} />
                  ))}
                </colgroup>
                <thead>
                  <tr>
                    {HISTORY_COLUMNS.map((col, idx) => (
                      <th
                        key={col.key}
                        className={`resizable ${col.align === "right" ? "r" : ""}`}
                        style={{
                          width: colWidths[col.key],
                          paddingRight: idx < HISTORY_COLUMNS.length - 1 ? 12 : 8,
                          verticalAlign: "middle",
                        }}
                      >
                        <span>{col.label}</span>
                        <div
                          className={`col-resizer ${resizingCol === col.key ? "active" : ""}`}
                          title="Drag horizontally to adjust column width"
                          onMouseDown={(e) => handleResizeStart(col.key, e)}
                        >
                          <div
                            className="resizer-bar"
                            style={{
                              width: 2,
                              height: "60%",
                              backgroundColor: "var(--border, #333)",
                              opacity: 0.4,
                              borderRadius: 1,
                              transition: "all 0.15s ease",
                            }}
                          />
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {history.map((h) => {
                    const symDisplay = Array.isArray(h.symbols) && h.symbols.length > 0
                      ? h.symbols.join(", ")
                      : (typeof h.symbols === "string" && h.symbols ? h.symbols : "All / Default");
                    const retDisplay = h.total_return_pct != null
                      ? `${h.total_return_pct >= 0 ? "+" : ""}${h.total_return_pct.toFixed(2)}%`
                      : "—";
                    const retClass = h.total_return_pct != null && h.total_return_pct >= 0 ? "pos" : "neg";
                    const ddDisplay = h.max_drawdown_pct != null ? `${h.max_drawdown_pct.toFixed(2)}%` : "—";
                    const wrDisplay = h.win_rate != null
                      ? (h.win_rate <= 1.0 ? (h.win_rate * 100).toFixed(1) : h.win_rate.toFixed(1)) + "%"
                      : "—";

                    return (
                      <tr key={h.id}>
                        <td className="muted" style={{ verticalAlign: "top" }}>{fmtDateTime(h.created_at)}</td>
                        <td
                          style={{
                            width: colWidths.symbols,
                            whiteSpace: "normal",
                            wordBreak: "break-word",
                            overflowWrap: "anywhere",
                            lineHeight: 1.35,
                            verticalAlign: "top",
                            fontSize: 11,
                          }}
                          title={symDisplay}
                        >
                          {symDisplay}
                        </td>
                        <td style={{ verticalAlign: "top" }}>{h.days ?? "—"}</td>
                        <td className={`r num ${retClass}`} style={{ verticalAlign: "top" }}>{retDisplay}</td>
                        <td className="r num" style={{ verticalAlign: "top" }}>{ddDisplay}</td>
                        <td className="r num" style={{ verticalAlign: "top" }}>{h.total_trades ?? 0}</td>
                        <td className="r num" style={{ verticalAlign: "top" }}>{wrDisplay}</td>
                        <td className="r" style={{ verticalAlign: "top" }}>
                          <button className="btn sm ghost" onClick={() => loadRun(h.id)}>View</button>
                        </td>
                      </tr>
                    );
                  })}
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
