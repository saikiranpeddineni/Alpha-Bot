import { useEffect, useState } from "react";
import { api } from "./api";
import "./replay.css";
import "./mobile-premium.css";
import "./mobile-finish.css";
import { BacktestModal } from "./components/BacktestModal";
import { ReplayModal } from "./components/ReplayModal";
import { BottomTabs } from "./components/BottomTabs";
import { PriceChart } from "./components/PriceChart";
import { SettingsModal } from "./components/SettingsModal";
import { SidePanel, StrategiesPanel } from "./components/SidePanel";
import { TopBar } from "./components/TopBar";
import { TrainerPanel } from "./components/TrainerPanel";
import { Watchlist } from "./components/Watchlist";
import { Modal } from "./components/Modal";
import { MobileHome } from "./components/MobileHome";
import { MobileActivity } from "./components/MobileActivity";
import { useStore } from "./store";
import { connectWebSocket } from "./ws";

type MobileTab = "home" | "markets" | "trade" | "activity" | "more";

export default function App() {
  const status = useStore((s) => s.status);
  const toast = useStore((s) => s.toast);
  const connected = useStore((s) => s.connected);
  const [showBacktest, setShowBacktest] = useState(false);
  const [showReplay, setShowReplay] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);
  const [isMobile, setIsMobile] = useState(window.innerWidth < 1024);
  const [mobileTab, setMobileTab] = useState<MobileTab>("home");

  function retryBootstrap() {
    setBootError(null);
    void api.status()
      .then((nextStatus) => useStore.getState().setStatus(nextStatus))
      .catch((error: unknown) => setBootError(error instanceof Error ? error.message : "Backend is unavailable."));
    void api.strategies()
      .then((strategies) => useStore.getState().setStrategies(strategies))
      .catch(() => undefined);
  }

  useEffect(() => {
    connectWebSocket();
    retryBootstrap();
    const refreshExecutionViews = () => {
      void Promise.all([api.positions(), api.orders(), api.trades()])
        .then(([positions, orders, trades]) => useStore.getState().setExecutionSnapshot(positions, orders, trades))
        .catch(() => undefined);
    };
    refreshExecutionViews();
    const executionRefresh = window.setInterval(refreshExecutionViews, 15000);
    const handleResize = () => setIsMobile(window.innerWidth < 1024);
    window.addEventListener("resize", handleResize);
    return () => { window.removeEventListener("resize", handleResize); window.clearInterval(executionRefresh); };
  }, []);

  const halted = status?.state === "halted" || status?.risk?.hard_halted || status?.system_health === "HALTED";

  if (isMobile) {
    return (
      <div className="app mobile premium-mobile">
        <MobileHeader onSettings={() => setShowSettings(true)} />
        <SystemStatusBanner status={status} connected={connected} bootError={bootError} onRetry={retryBootstrap} />
        {halted && (
          <div className="halt-banner">
            ⛔ KILL SWITCH ACTIVE — {status?.risk?.hard_halt_reason || "max drawdown breached"}
            <button className="btn sm danger" onClick={() => api.resetHalt().then((s) => useStore.getState().setStatus(s)).catch((error: unknown) => useStore.getState().showToast("error", error instanceof Error ? error.message : "Unable to reset halt."))} style={{ marginLeft: "8px" }}>
              Reset
            </button>
          </div>
        )}
        <main className="mobile-content">
          {!status ? <ConnectionEmpty onRetry={retryBootstrap} /> : <>
            {mobileTab === "home" && <MobileHome onOpenActivity={() => setMobileTab("activity")} onOpenChart={() => setMobileTab("trade")} />}
            {mobileTab === "markets" && (
              <div className="mobile-page">
                <div className="mobile-page-title"><span>Markets</span><small>Your live universe</small></div>
                <Watchlist />
              </div>
            )}
            {mobileTab === "trade" && (
              <div className="mobile-page mobile-trade-page">
                <PriceChart />
              </div>
            )}
            {mobileTab === "activity" && <MobileActivity />}
            {mobileTab === "more" && (
              <MobileMore
                onSettings={() => setShowSettings(true)}
                onBacktest={() => setShowBacktest(true)}
                onReplay={() => setShowReplay(true)}
              />
            )}
          </>}
        </main>
        <nav className="mobile-nav premium-mobile-nav" aria-label="Primary navigation">
          <MobileNavButton active={mobileTab === "home"} label="Home" icon="⌂" onClick={() => setMobileTab("home")} />
          <MobileNavButton active={mobileTab === "markets"} label="Markets" icon="◫" onClick={() => setMobileTab("markets")} />
          <MobileNavButton active={mobileTab === "trade"} label="Trade" icon="↗" onClick={() => setMobileTab("trade")} />
          <MobileNavButton active={mobileTab === "activity"} label="Activity" icon="≋" onClick={() => setMobileTab("activity")} />
          <MobileNavButton active={mobileTab === "more"} label="More" icon="•••" onClick={() => setMobileTab("more")} />
        </nav>
        {showBacktest && <BacktestModal onClose={() => setShowBacktest(false)} />}
        {showReplay && <ReplayModal onClose={() => setShowReplay(false)} />}
        {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
        {toast && <div className={`toast ${toast.kind}`} role={toast.kind === "error" ? "alert" : "status"} aria-live="polite">{toast.text}</div>}
      </div>
    );
  }

  return <div className="app">
    <TopBar onBacktest={() => setShowBacktest(true)} onReplay={() => setShowReplay(true)} onSettings={() => setShowSettings(true)} />
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      <SystemStatusBanner status={status} connected={connected} bootError={bootError} onRetry={retryBootstrap} />
      {halted && <div className="halt-banner">⛔ KILL SWITCH — {status?.risk?.hard_halt_reason || "max drawdown breached"}<button className="btn sm danger" onClick={() => api.resetHalt().then((s) => useStore.getState().setStatus(s)).catch((error: unknown) => useStore.getState().showToast("error", error instanceof Error ? error.message : "Unable to reset halt."))}>Reset halt</button></div>}
      {status ? <div className="main" style={{ flex: 1, minHeight: 0 }}><div className="left-col"><Watchlist /><TrainerPanel /></div><div className="center"><PriceChart /><BottomTabs /></div><SidePanel /></div> : <ConnectionEmpty onRetry={retryBootstrap} />}
    </div>
    {showBacktest && <BacktestModal onClose={() => setShowBacktest(false)} />}
    {showReplay && <ReplayModal onClose={() => setShowReplay(false)} />}
    {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    {toast && <div className={`toast ${toast.kind}`} role={toast.kind === "error" ? "alert" : "status"} aria-live="polite">{toast.text}</div>}
  </div>;
}

function ConnectionEmpty({ onRetry }: { onRetry: () => void }) {
  return <div className="app-empty-state" role="status"><strong>Waiting for backend data</strong><span>Live account, market, and risk state will appear after the initial snapshot.</span><button className="btn sm" onClick={onRetry}>Retry connection</button></div>;
}

function SystemStatusBanner({
  status,
  connected,
  bootError,
  onRetry,
}: {
  status: ReturnType<typeof useStore.getState>["status"];
  connected: boolean;
  bootError: string | null;
  onRetry: () => void;
}) {
  if (!status) {
    return (
      <div className="system-banner loading" role="status" aria-live="polite">
        <span>{bootError ? `Backend unavailable: ${bootError}` : "Connecting to AlphaDesk…"}</span>
        <button className="btn sm" onClick={onRetry}>Retry</button>
      </div>
    );
  }

  const issues: string[] = [];
  if (!connected) issues.push("stream disconnected");
  if (status.last_error) issues.push(status.last_error);
  if (status.system_health && status.system_health !== "NORMAL") issues.push(`health ${status.system_health}`);
  if (status.reconciliation_failed) issues.push("broker reconciliation uncertain");
  if (status.allow_new_entries === false && status.state === "running") issues.push("new entries blocked");
  if (status.risk.daily_halted) issues.push("daily loss halt active");
  if (status.risk.profit_locked) issues.push("profit lock active");
  if (status.data_gap_symbols?.length) issues.push(`stale data: ${status.data_gap_symbols.join(", ")}`);
  if (!issues.length) return null;

  return (
    <div className="system-banner warning" role="status" aria-live="polite">
      <span><b>System status:</b> {issues.join(" · ")}</span>
      <button className="btn sm" onClick={onRetry}>Refresh</button>
    </div>
  );
}

function MobileNavButton({ active, label, icon, onClick }: { active: boolean; label: string; icon: string; onClick: () => void }) {
  return (
    <button className={`mobile-nav-btn ${active ? "active" : ""}`} onClick={onClick} aria-current={active ? "page" : undefined}>
      <span className="icon">{icon}</span>
      <span>{label}</span>
    </button>
  );
}

function MobileMore({ onSettings, onBacktest, onReplay }: { onSettings: () => void; onBacktest: () => void; onReplay: () => void }) {
  return (
    <div className="mobile-page">
      <div className="mobile-page-title">
        <span>More</span>
        <small>Research and configuration</small>
      </div>
      <div className="mobile-more-grid">
        <button onClick={onSettings}>
          <b>Settings</b>
          <span>Risk, broker and app preferences</span>
        </button>
        <button onClick={onBacktest}>
          <b>Backtest</b>
          <span>Historical strategy research</span>
        </button>
        <button onClick={onReplay}>
          <b>Replay</b>
          <span>Deterministic session replay</span>
        </button>
      </div>
      <div className="mobile-more-stack">
        <StrategiesPanel />
        <TrainerPanel />
      </div>
    </div>
  );
}

function MobileHeader({ onSettings }: { onSettings: () => void }) {
  const status = useStore((s) => s.status);
  const running = status?.state === "running";
  const market = status?.market;
  const showToast = useStore((s) => s.showToast);
  const [busy, setBusy] = useState(false);
  const [liveConfirm, setLiveConfirm] = useState(false);
  const [stopConfirm, setStopConfirm] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [mode, setMode] = useState<string | null>(null);
  const currentMode = mode ?? status?.mode ?? "demo";

  async function start(confirm = "") {
    setBusy(true);
    try {
      const s = await api.startEngine(currentMode, confirm);
      useStore.getState().setStatus(s);
      showToast("info", `Engine started (${currentMode} mode)`);
    } catch (e) {
      showToast("error", (e as Error).message);
    } finally {
      setBusy(false);
      setLiveConfirm(false);
      setConfirmText("");
    }
  }

  async function handleStartStop() {
    if (running) {
      setStopConfirm(true);
      return;
    }
    if (currentMode === "live") { setLiveConfirm(true); return; }
    await start();
  }

  return (
    <header className="mobile-header premium-mobile-header">
      <div className="mobile-brand">
        <span className="mobile-logo">A</span>
        <div>
          <b>AlphaDesk</b>
          <small>
            <span className={`mobile-status-dot ${market?.is_open ? "live" : ""}`} />
            {market?.is_open ? "Market open" : "Market closed"}
          </small>
        </div>
      </div>
      <div className="actions">
        <select className="mobile-mode-select" value={currentMode} disabled={!status || running || busy} onChange={(event) => setMode(event.target.value)} aria-label="Trading mode">
          <option value="demo">Demo</option>
          <option value="paper">Paper</option>
          <option value="live" disabled={!status?.live_trading_enabled}>Live</option>
        </select>
        <button className={`mobile-engine-btn ${running ? "running" : ""}`} onClick={handleStartStop} disabled={!status || busy || status?.state === "halted"}>
          {busy ? "…" : running ? "Live" : "Start"}
        </button>
        <button className="mobile-icon-btn" onClick={onSettings} aria-label="Settings">⚙</button>
      </div>
      {liveConfirm && <Modal narrow title="Confirm live trading" onClose={() => setLiveConfirm(false)}><div className="modal-body"><p style={{ marginTop: 0 }}>You are about to start the engine in <b style={{ color: "var(--red)" }}>LIVE</b> mode. Orders will be placed with real money through your broker.</p><p className="muted">Type <b className="mono">LIVE</b> to confirm.</p><input type="text" className="mono" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} placeholder="LIVE" style={{ width: "100%" }} autoFocus /></div><div className="modal-foot"><button className="btn ghost" onClick={() => setLiveConfirm(false)}>Cancel</button><button className="btn danger" disabled={confirmText !== "LIVE" || busy} onClick={() => start("LIVE")}>Start live trading</button></div></Modal>}
      {stopConfirm && <Modal narrow title="Stop engine" onClose={() => !busy && setStopConfirm(false)}><div className="modal-body"><p style={{ marginTop: 0 }}>Choose how to stop the engine.</p><p className="muted">Flattening closes all open positions. Stop only leaves existing positions and protection in place.</p></div><div className="modal-foot"><button className="btn ghost" onClick={() => setStopConfirm(false)} disabled={busy}>Cancel</button><button className="btn" onClick={() => { setBusy(true); api.stopEngine(false).then((s) => { useStore.getState().setStatus(s); showToast("info", "Engine stopped; positions preserved"); setStopConfirm(false); }).catch((e) => showToast("error", (e as Error).message)).finally(() => setBusy(false)); }} disabled={busy}>Stop only</button><button className="btn danger" onClick={() => { setBusy(true); api.stopEngine(true).then((s) => { useStore.getState().setStatus(s); showToast("info", "Engine stopped and positions flattened"); setStopConfirm(false); }).catch((e) => showToast("error", (e as Error).message)).finally(() => setBusy(false)); }} disabled={busy}>Stop and flatten</button></div></Modal>}
    </header>
  );
}
