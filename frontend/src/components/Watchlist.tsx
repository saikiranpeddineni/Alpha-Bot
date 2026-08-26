import { useState } from "react";
import { api } from "../api";
import { fmtCompact, fmtPct, pnlClass } from "../format";
import { useStore } from "../store";

const REGIME_ICONS: Record<string, string> = {
  trend_up: "↗",
  trend_down: "↘",
  range: "↔",
  volatile: "⚡",
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
      await api.setWatchlist([...watchlist, sym]);
      setDraft("");
    } catch (e) {
      showToast("error", (e as Error).message);
    }
  }

  async function removeSymbol(sym: string) {
    try {
      await api.setWatchlist(watchlist.filter((s) => s !== sym));
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
          {scanning ? "Scanning..." : "🔍 Scan"}
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
