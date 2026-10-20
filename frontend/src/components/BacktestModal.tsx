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

function etDateInput(date: Date): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

export function BacktestModal(props: { onClose: () => void }) {
  const status = useStore((s) => s.status);
  const progress = useStore((s) => s.backtestProgress);
  const showToast = useStore((s) => s.showToast);
  const clearBacktestProgress = useStore((s) => s.clearBacktestProgress);

  const watchlist = status?.watchlist ?? [];
  const [symbols, setSymbols] = useState<string[]>(watchlist.slice(0, 3));
  const [days, setDays] = useState(5);
  const [rangeMode, setRangeMode] = useState<"days" | "dates">("days");
  const [startDate, setStartDate] = useState(() => etDateInput(new Date(Date.now() - 5 * 86400000)));
  const [endDate, setEndDate] = useState(() => etDateInput(new Date()));
  const [dynamicScanner, setDynamicScanner] = useState(false);
  const [cash, setCash] = useState(100000);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);

  function toggleSymbol(sym: string) {
    setSymbols((prev) => (prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym]));
  }

  async function run() {
    if (symbols.length === 0) {
      showToast("error", "Select at least one symbol");
      return;
    }
    if (rangeMode === "dates" && (!startDate || !endDate || startDate > endDate)) {
      showToast("error", "Choose a valid custom date range");
      return;
    }
    if (rangeMode === "days" && (!Number.isInteger(days) || days < 1 || days > 90)) {
      showToast("error", "Lookback days must be between 1 and 90");
      return;
    }
    if (!Number.isFinite(cash) || cash < 1000) {
      showToast("error", "Starting cash must be at least $1,000");
      return;
    }
    setRunning(true);
    setResult(null);
    clearBacktestProgress();
    try {
      const res = await api.runBacktest({
        symbols,
        days,
        starting_cash: cash,
        use_dynamic_scanner: dynamicScanner,
        start_date: rangeMode === "dates" ? startDate : null,
        end_date: rangeMode === "dates" ? endDate : null,
      });
      setResult(res);
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setRunning(false);
      clearBacktestProgress();
    }
  }

  return (
    <Modal title="Backtest" onClose={props.onClose}>
      <div className="modal-body">
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
          <label className="field" style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <input
              type="checkbox"
              checked={dynamicScanner}
              onChange={(e) => setDynamicScanner(e.target.checked)}
            />
            <span>
              <b>Use historical dynamic scanner</b>
              <span className="hint" style={{ display: "block" }}>
                Replays scanner decisions causally from the historical stream; slower but auditable.
              </span>
            </span>
          </label>
          <div className="form-notice" style={{ gridColumn: "1 / -1" }} role="note">
            Production ML, risk, execution, and look-ahead protections remain enabled for every run. This screen does not expose unsafe bypass switches.
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
                const v = (result.metrics ?? {})[key];
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
              <EquityChart curve={result.equity_curve ?? []} height={180} />
            </div>
            {(result.warnings ?? []).length > 0 && (
              <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>
                {(result.warnings ?? []).map((w, i) => (
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
                  {(result.trades ?? []).map((t) => (
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
                  {(result.trades ?? []).length === 0 && (
                    <tr><td colSpan={10} className="muted">No trades in this run</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
      <div className="modal-foot">
        <button type="button" className="btn ghost" onClick={props.onClose}>Close</button>
        <button type="button" className="btn primary" onClick={run} disabled={running}>
          {running ? "Running…" : "Run backtest"}
        </button>
      </div>
    </Modal>
  );
}
