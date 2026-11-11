import { useState } from "react";
import { api } from "../api";
import { fmtCompact, fmtPct, pnlClass } from "../format";
import { useStore } from "../store";

// [MODIFIED 2026-11-11]: Safe Unicode Escapes for Regime Arrows
const REGIME_ICONS: Record<string, string> = {
  trend_up: "\u2197",
  trend_down: "\u2198",
  range: "\u2194",
  volatile: "\u26A1",
};

export function Watchlist() {
  const quotes = useStore((s) => s.quotes);
  const status = useStore((s) => s.status);
  const selected = useStore((s) => s.selectedSymbol);
  const selectSymbol = useStore((s) => s.selectSymbol);
  const showToast = useStore((s) => s.showToast);
  const [draft, setDraft] = useState("");
  const [scanning, setScanning] = useState(false);

  const watchlist = status?.watchlist ?? [];
  const coreWatchlist = status?.core_watchlist ?? [];
  const scannerCtx = status?.scanner_context ?? {};
  const quoteBySymbol = new Map(quotes.map((q) => [q.symbol, q]));

  async function addSymbol() {
    const sym = draft.trim().toUpperCase();
    if (!sym) return;
    try {
      const base = coreWatchlist.length > 0 ? coreWatchlist : watchlist;
      await api.setWatchlist(Array.from(new Set([...base, sym])));
      setDraft("");
    } catch (e) {
      showToast("error", (e as Error).message);
    }
  }

  async function removeSymbol(sym: string) {
    try {
      const base = coreWatchlist.length > 0 ? coreWatchlist : watchlist;
      await api.setWatchlist(base.filter((s) => s !== sym));
    } catch (e) {
      showToast("error", (e as Error).message);
    }
  }

  async function triggerScanner() {
    setScanning(true);
    showToast("info", "Starting market scan & validation...");
    try {
      await api.runScanner();
      showToast("info", `Market scan complete! Watchlist updated.`);
    } catch (e) {
      showToast("error", `Scanner failed: ${(e as Error).message}`);
    } finally {
      setScanning(false);
    }
  }

  return (
    <div className="panel watchlist-panel">
      <div className="panel-head" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span>Watchlist</span>
          <span className="faint">{watchlist.length}</span>
        </div>
        <button
          className={`btn sm ${scanning ? "loading" : ""}`}
          style={{ padding: "2px 8px", fontSize: "11px", height: "24px", minWidth: "60px" }}
          onClick={triggerScanner}
          disabled={scanning}
        >
          {/* [MODIFIED 2026-11-11]: SVG Search Scan Icon */}
          {/* [PREVIOUS]: {scanning ? "Scanning..." : "🔍 Scan"} */}
          {scanning ? "Scanning..." : (
            <span style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="11" cy="11" r="8" />
                <line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
              Scan
            </span>
          )}
        </button>
      </div>
      <div className="panel-body">
        {watchlist.length === 0 && <div className="empty-state">Add symbols below</div>}
        {watchlist.map((sym) => {
          const q = quoteBySymbol.get(sym);
          const isCore = coreWatchlist.length === 0 || coreWatchlist.includes(sym);
          const sc = scannerCtx[sym];
          return (
            <div
              key={sym}
              className={`watch-row ${selected === sym ? "selected" : ""}`}
              onClick={() => selectSymbol(sym)}
            >
              <span className="sym">
                {sym}{" "}
                {!isCore && (
                  <span
                    className={`scanned-badge ${q && q.change_pct < 0 ? 'loser' : 'gainer'}`}
                    title="Dynamically selected by AI Market Scanner"
                    style={{
                      color: q && q.change_pct < 0 ? '#ff4d4f' : '#52c41a',
                      borderColor: q && q.change_pct < 0 ? '#ff4d4f33' : '#52c41a33',
                      backgroundColor: q && q.change_pct < 0 ? '#ff4d4f11' : '#52c41a11'
                    }}
                  >
                    {q && q.change_pct < 0 ? "LOSER" : "GAINER"}
                  </span>
                )}
                {sc && (
                  <span
                    className="ml-tag"
                    title={`Scanner ML — tech score ${sc.score?.toFixed(2) ?? "n/a"}, walk-forward AUC ${sc.auc > 0 ? sc.auc.toFixed(3) : "n/a"} (min promotable 0.53)`}
                    style={{
                      color: sc.auc >= 0.53 ? "#52c41a" : sc.auc > 0 ? "#faad14" : "#8c8c8c",
                      borderColor: sc.auc >= 0.53 ? "#52c41a33" : sc.auc > 0 ? "#faad1433" : "#8c8c8c22",
                      backgroundColor: sc.auc >= 0.53 ? "#52c41a11" : sc.auc > 0 ? "#faad1411" : "#8c8c8c0d",
                      border: "1px solid",
                      borderRadius: "3px",
                      fontSize: "9px",
                      fontWeight: 700,
                      padding: "0 4px",
                      marginLeft: "2px",
                    }}
                  >
                    {sc.auc > 0 ? `ML ${sc.auc.toFixed(2)}` : "ML ·"}
                  </span>
                )}
                {q?.regime && (
                  <span className="faint" style={{ marginLeft: "4px" }} title={q.regime}>
                    {REGIME_ICONS[q.regime] ?? ""}
                  </span>
                )}
              </span>
              <span className="num px">{q ? q.price.toFixed(2) : "—"}</span>
              <span className="sub">
                Vol {q ? fmtCompact(q.volume) : "—"}
                <button
                  className="del"
                  title={`Remove ${sym}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    removeSymbol(sym);
                  }}
                >
                  ✕
                </button>
              </span>
              <span className={`chg num ${pnlClass(q?.change_pct)}`}>{fmtPct(q?.change_pct)}</span>
            </div>
          );
        })}
      </div>
      <div className="watch-add">
        <input
          type="text"
          placeholder="Add symbol…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addSymbol()}
          maxLength={8}
        />
        <button className="btn sm" onClick={addSymbol}>+</button>
      </div>
    </div>
  );
}
