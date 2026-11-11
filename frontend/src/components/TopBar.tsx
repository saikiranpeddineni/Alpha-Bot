import { useState } from "react";
import { api } from "../api";
import { fmtMoney0, fmtPct, fmtSigned, pnlClass } from "../format";
import { useStore } from "../store";
import { HeartbeatIndicator } from "./HeartbeatIndicator";
import { Modal } from "./Modal";

export function TopBar(props: { onBacktest: () => void; onSettings: () => void }) {
  const status = useStore((s) => s.status);
  const account = useStore((s) => s.account);
  const connected = useStore((s) => s.connected);
  const showToast = useStore((s) => s.showToast);
  const [mode, setMode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [liveConfirm, setLiveConfirm] = useState(false);
  const [stopConfirm, setStopConfirm] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const running = status?.state === "running";
  const ready = Boolean(status);
  const currentMode = mode ?? status?.mode ?? "demo";
  const market = status?.market;

  async function start(confirm = "") {
    setBusy(true);
    try {
      const nextStatus = await api.startEngine(currentMode, confirm);
      useStore.getState().setStatus(nextStatus);
      showToast("info", `Engine started (${currentMode} mode)`);
    } catch (error) {
      showToast("error", (error as Error).message);
    } finally {
      setBusy(false);
      setLiveConfirm(false);
      setConfirmText("");
    }
  }

  async function stop(flatten: boolean) {
    setBusy(true);
    try {
      const nextStatus = await api.stopEngine(flatten);
      useStore.getState().setStatus(nextStatus);
      showToast("info", flatten ? "Engine stopped and positions flattened" : "Engine stopped; positions preserved");
      setStopConfirm(false);
    } catch (error) {
      showToast("error", (error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function onStartStop() {
    if (!ready) return;
    if (running) {
      setStopConfirm(true);
      return;
    }
    if (currentMode === "live") {
      setLiveConfirm(true);
      return;
    }
    void start();
  }

  return (
    <header className="topbar">
      {/* [MODIFIED 2026-11-11]: Fix Corrupted TopBar Bolt Glyphs */}
      {/* [PREVIOUS]: <div className="brand"><span className="bolt">⚡</span><span>AlphaDesk</span></div> */}
      <div className="brand"><span className="bolt" style={{ display: "inline-flex", alignItems: "center" }}><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2L3 14h7v8l11-12h-8z"/></svg></span><span>AlphaDesk</span></div>
      <span className={`badge ${status?.mode ?? "demo"}`}>{status?.mode ?? "—"}</span>
      <span className="market-dot">
        <span className={`dot ${market?.is_open ? "open" : ""}`} />
        {!status ? "Waiting for backend" : market?.always_open ? "Demo market" : market?.is_open ? "Market open" : "Market closed"}
        {market?.is_open && market.minutes_to_close != null && !market.always_open && <span className="faint num"> · {Math.floor(market.minutes_to_close)}m to close</span>}
      </span>
      <span className="market-dot" title={connected ? "Live stream connected" : "Stream disconnected — reconnecting"}>
        <span className={`dot ${connected ? "connected" : "disconnected"}`} />{connected ? "Stream" : "Reconnecting…"}
      </span>
      <HeartbeatIndicator />
      <div className="top-stats">
        <div className="stat"><span className="label">Equity</span><span className="value num">{fmtMoney0(account?.equity)}</span></div>
        <div className="stat"><span className="label">Day P&amp;L</span><span className={`value num ${pnlClass(account?.day_pnl)}`}>{fmtSigned(account?.day_pnl)} <span style={{ fontSize: 11 }}>({fmtPct(account?.day_pnl_pct)})</span></span></div>
        <div className="stat"><span className="label">Drawdown</span><span className="value num">{account ? `${account.drawdown_pct.toFixed(1)}%` : "—"}</span></div>
        <select value={currentMode} disabled={!ready || running || busy} onChange={(event) => setMode(event.target.value)} title="Trading mode">
          <option value="demo">Demo (offline)</option>
          <option value="paper">Paper</option>
          <option value="live" disabled={!status?.live_trading_enabled}>Live {status?.live_trading_enabled ? "" : "(disabled)"}</option>
        </select>
        <button className={`btn ${running ? "danger" : "success"}`} onClick={onStartStop} disabled={!ready || busy || status?.state === "halted"}>
          {/* [MODIFIED 2026-11-11]: Safe Stop/Start Indicator */}
          {/* [PREVIOUS]: {busy ? "…" : running ? "■ Stop" : "▶ Start"} */}
          {busy ? "..." : running ? "■ Stop" : "▶ Start"}
        </button>
        <button className="btn" onClick={props.onBacktest}>Backtest</button>
        {/* [MODIFIED 2026-11-11]: SVG Settings Gear Icon */}
        {/* [PREVIOUS]: <button className="btn icon" onClick={props.onSettings} title="Settings">⚙</button> */}
        <button className="btn icon" onClick={props.onSettings} title="Settings" style={{ display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      </div>

      {liveConfirm && (
        <Modal narrow title="Confirm live trading" onClose={() => setLiveConfirm(false)}>
          <div className="modal-body">
            <p style={{ marginTop: 0 }}>You are about to start the engine in <b style={{ color: "var(--red)" }}>LIVE</b> mode. Orders will be placed with real money through your broker.</p>
            <p className="muted">Type <b className="mono">LIVE</b> to confirm.</p>
            <input type="text" className="mono" value={confirmText} onChange={(event) => setConfirmText(event.target.value)} placeholder="LIVE" style={{ width: "100%" }} autoFocus />
          </div>
          <div className="modal-foot"><button className="btn ghost" onClick={() => setLiveConfirm(false)}>Cancel</button><button className="btn danger" disabled={confirmText !== "LIVE" || busy} onClick={() => void start("LIVE")}>Start live trading</button></div>
        </Modal>
      )}

      {stopConfirm && (
        <Modal narrow title="Stop engine" onClose={() => !busy && setStopConfirm(false)}>
          <div className="modal-body">
            <p style={{ marginTop: 0 }}>Choose how to stop the engine. This action does not change any trading gates.</p>
            <p className="muted">Flattening closes all open positions before the engine stops. Stop only leaves positions with their existing protection.</p>
          </div>
          <div className="modal-foot">
            <button className="btn ghost" onClick={() => setStopConfirm(false)} disabled={busy}>Cancel</button>
            <button className="btn" onClick={() => void stop(false)} disabled={busy}>Stop only</button>
            <button className="btn danger" onClick={() => void stop(true)} disabled={busy}>Stop and flatten</button>
          </div>
        </Modal>
      )}
    </header>
  );
}
