import { useEffect, useState } from "react";
import { api } from "./api";
import "./replay.css";
import { BacktestModal } from "./components/BacktestModal";
import { ReplayModal } from "./components/ReplayModal";
import { BottomTabs } from "./components/BottomTabs";
import { PriceChart } from "./components/PriceChart";
import { SettingsModal } from "./components/SettingsModal";
import { SidePanel, InsightsPanel, PerformancePanel, StrategiesPanel } from "./components/SidePanel";
import { ThemeSelector } from "./components/ThemeSelector";
import { TopBar } from "./components/TopBar";
import { TrainerPanel } from "./components/TrainerPanel";
import { Watchlist } from "./components/Watchlist";
import { useStore } from "./store";
import { AppTheme, applyTheme, persistTheme, readTheme } from "./theme";
import { connectWebSocket } from "./ws";

export default function App() {
  const status = useStore((s) => s.status);
  const toast = useStore((s) => s.toast);
  const [showBacktest, setShowBacktest] = useState(false);
  const [showReplay, setShowReplay] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [isMobile, setIsMobile] = useState(window.innerWidth < 1024);
  const [mobileTab, setMobileTab] = useState<"monitor" | "chart" | "watchlist" | "config" | "activity">("monitor");
  const [theme, setTheme] = useState<AppTheme>(() => readTheme());

  useEffect(() => { applyTheme(theme); }, [theme]);
  useEffect(() => {
    connectWebSocket();
    api.strategies().then((s) => useStore.getState().setStrategies(s)).catch(() => undefined);
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

  function changeTheme(nextTheme: AppTheme) { persistTheme(nextTheme); setTheme(nextTheme); }
  const halted = status?.state === "halted" || status?.risk?.hard_halted;

  if (isMobile) {
    return <div className="app mobile">
      <MobileHeader theme={theme} onThemeChange={changeTheme} onSettings={() => setShowSettings(true)} />
      {halted && <div className="halt-banner">⛔ KILL SWITCH ACTIVE — {status?.risk?.hard_halt_reason || "max drawdown breached"}<button className="btn sm danger" onClick={() => api.resetHalt().then((s) => useStore.getState().setStatus(s)).catch(() => undefined)} style={{ marginLeft: "8px" }}>Reset halt</button></div>}
      <div className="main">
        {mobileTab === "monitor" && <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}><PerformancePanel /><InsightsPanel /></div>}
        {mobileTab === "chart" && <PriceChart />}{mobileTab === "watchlist" && <Watchlist />}
        {mobileTab === "config" && <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}><StrategiesPanel /><TrainerPanel /></div>}
        {mobileTab === "activity" && <BottomTabs />}
      </div>
      <div className="mobile-nav">
        <button className={`mobile-nav-btn ${mobileTab === "monitor" ? "active" : ""}`} onClick={() => setMobileTab("monitor")}><span className="icon">📊</span><span>Monitor</span></button>
        <button className={`mobile-nav-btn ${mobileTab === "chart" ? "active" : ""}`} onClick={() => setMobileTab("chart")}><span className="icon">📈</span><span>Chart</span></button>
        <button className={`mobile-nav-btn ${mobileTab === "watchlist" ? "active" : ""}`} onClick={() => setMobileTab("watchlist")}><span className="icon">📋</span><span>Watch</span></button>
        <button className={`mobile-nav-btn ${mobileTab === "config" ? "active" : ""}`} onClick={() => setMobileTab("config")}><span className="icon">⚙️</span><span>Config</span></button>
        <button className={`mobile-nav-btn ${mobileTab === "activity" ? "active" : ""}`} onClick={() => setMobileTab("activity")}><span className="icon">📜</span><span>Activity</span></button>
      </div>
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}{toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}
    </div>;
  }

  return <div className="app">
    <TopBar theme={theme} onThemeChange={changeTheme} onBacktest={() => setShowBacktest(true)} onReplay={() => setShowReplay(true)} onSettings={() => setShowSettings(true)} />
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      {halted && <div className="halt-banner">⛔ KILL SWITCH — {status?.risk?.hard_halt_reason || "max drawdown breached"}<button className="btn sm danger" onClick={() => api.resetHalt().then((s) => useStore.getState().setStatus(s)).catch(() => undefined)}>Reset halt</button></div>}
      <div className="main" style={{ flex: 1, minHeight: 0 }}><div className="left-col"><Watchlist /><TrainerPanel /></div><div className="center"><PriceChart /><BottomTabs /></div><SidePanel /></div>
    </div>
    {showBacktest && <BacktestModal onClose={() => setShowBacktest(false)} />}
    {showReplay && <ReplayModal onClose={() => setShowReplay(false)} />}
    {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    {toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}
  </div>;
}

function MobileHeader({ theme, onThemeChange, onSettings }: { theme: AppTheme; onThemeChange: (theme: AppTheme) => void; onSettings: () => void }) {
  const status = useStore((s) => s.status); const running = status?.state === "running"; const market = status?.market; const showToast = useStore((s) => s.showToast); const [busy, setBusy] = useState(false);
  async function handleStartStop() { setBusy(true); try { if (running) { const s = await api.stopEngine(); useStore.getState().setStatus(s); showToast("info", "Engine stopped"); } else { const currentMode = status?.mode ?? "demo"; const s = await api.startEngine(currentMode); useStore.getState().setStatus(s); showToast("info", `Engine started (${currentMode} mode)`); } } catch (e) { showToast("error", (e as Error).message); } finally { setBusy(false); } }
  return <div className="mobile-header"><div className="brand" style={{ display: "flex", alignItems: "center", gap: "6px" }}><span className="bolt">⚡</span> AlphaDesk <span className={`badge ${status?.mode ?? "demo"}`} style={{ fontSize: "9px", padding: "1px 5px", textTransform: "uppercase" }}>{status?.mode ?? "demo"}</span></div><div className="actions" style={{ display: "flex", alignItems: "center", gap: "8px" }}><ThemeSelector theme={theme} onThemeChange={onThemeChange} compact /><span className={`dot ${market?.is_open ? "open" : ""}`} aria-label={market?.is_open ? "Market open" : "Market closed"} /><button className={`btn sm ${running ? "danger" : "success"}`} onClick={handleStartStop} disabled={busy || status?.state === "halted"} style={{ padding: "4px 10px", height: "26px", fontSize: "11px" }}>{busy ? "…" : running ? "Stop" : "Start"}</button><button className="btn sm icon" onClick={onSettings} style={{ padding: "4px 8px", height: "26px", fontSize: "11px" }} title="Settings">⚙</button></div></div>;
}
