import { useEffect, useState } from "react";
import { useStore } from "../store";
import type { PulseIntensity } from "../types";

function formatAgo(isoStr?: string | null): string {
  if (!isoStr) return "—";
  try {
    const diffMs = Date.now() - new Date(isoStr).getTime();
    if (diffMs < 0) return "just now";
    const sec = Math.floor(diffMs / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    return `${Math.floor(min / 60)}h ago`;
  } catch {
    return "—";
  }
}

export function HeartbeatIndicator() {
  const connected = useStore((s) => s.connected);
  const status = useStore((s) => s.status);
  const heartbeat = useStore((s) => s.heartbeat);
  const styleMode = useStore((s) => s.heartbeatStyle);

  const [hovered, setHovered] = useState(false);
  const [pulse, setPulse] = useState<PulseIntensity | null>(null);
  const [, setTick] = useState(0);

  // Force re-render every second to update relative "ago" timestamps in hover card
  useEffect(() => {
    const timer = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  // Trigger pulse spike whenever heartbeat sequence changes
  useEffect(() => {
    if (!heartbeat) return;
    const intensity = heartbeat.event?.pulse || "small";
    setPulse(intensity);
    const timeout = setTimeout(() => setPulse(null), 600);
    return () => clearTimeout(timeout);
  }, [heartbeat?.seq]);

  if (styleMode === "off") {
    return null;
  }

  // Derive Health Color State
  const health = !connected
    ? "HALTED"
    : heartbeat?.health || (status?.state === "halted" ? "HALTED" : "NORMAL");

  let dotColor = "var(--green)";
  let dotStatus = "healthy";
  if (health === "HALTED" || !connected) {
    dotColor = "var(--red)";
    dotStatus = "halted";
  } else if (health === "DEGRADED") {
    dotColor = "var(--orange, #f59e0b)";
    dotStatus = "degraded";
  } else if (health === "WARNING") {
    dotColor = "var(--yellow, #eab308)";
    dotStatus = "warning";
  }

  const activity = heartbeat?.activity || (status?.state === "running" ? "EVALUATING" : "IDLE");
  const stats = heartbeat?.stats || {
    broker_connected: status?.broker !== null,
    market_data_fresh: true,
    scanner_ok: true,
    market_regime: "NORMAL",
    watchlist_count: status?.watchlist?.length || 0,
    universe_count: status?.watchlist?.length || 0,
    signals_evaluated_today: 0,
    evaluations_last_sec: 0,
    orders_today: 0,
    replay_queue_depth: 0,
    latency_ms: 15,
  };

  const timestamps = heartbeat?.timestamps || {};
  const lastEvent = heartbeat?.event;

  return (
    <div
      className="heartbeat-container"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title="Telemetry System Heartbeat"
    >
      <div className="heartbeat-bar">
        {/* Status Dot */}
        <span className={`hb-dot ${dotStatus} ${pulse ? `pulse-${pulse}` : ""}`} style={{ backgroundColor: dotColor }} />

        {/* Optional Micro Heart Icon */}
        {styleMode === "heart" && (
          <span className={`hb-heart ${pulse ? "beat" : "resting"}`}>
            ❤️
          </span>
        )}

        {/* Minimalist EKG Waveform SVG */}
        {(styleMode === "ekg" || styleMode === "heart") && (
          <svg className={`hb-ekg ${pulse ? `spike-${pulse}` : "resting"}`} viewBox="0 0 54 16">
            <path
              d={
                pulse === "large"
                  ? "M 0 8 L 12 8 L 16 2 L 20 14 L 24 0 L 28 16 L 32 8 L 54 8"
                  : pulse === "medium"
                  ? "M 0 8 L 14 8 L 18 3 L 22 13 L 26 5 L 30 8 L 54 8"
                  : pulse === "small"
                  ? "M 0 8 L 16 8 L 20 5 L 24 11 L 28 8 L 54 8"
                  : "M 0 8 L 54 8"
              }
              fill="none"
              stroke={dotColor}
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        )}

        <span className="hb-label mono">{activity}</span>
      </div>

      {/* Hover Telemetry Card */}
      {hovered && (
        <div className="hb-card">
          <div className="hb-card-head">
            <div className="hb-card-title">
              <span className="hb-dot inline" style={{ backgroundColor: dotColor }} />
              <span>Engine {health === "NORMAL" ? "Healthy" : health}</span>
            </div>
            <span className="hb-badge mono">{activity}</span>
          </div>

          <div className="hb-card-grid">
            <div className="hb-item">
              <span className="hb-k">Broker</span>
              <span className={`hb-v ${stats.broker_connected ? "pos" : "neg"}`}>
                {stats.broker_connected ? "Connected" : "Disconnected"}
              </span>
            </div>
            <div className="hb-item">
              <span className="hb-k">Market Data</span>
              <span className={`hb-v ${stats.market_data_fresh ? "pos" : "neg"}`}>
                {stats.market_data_fresh ? "Live Feed" : "Stale"}
              </span>
            </div>
            <div className="hb-item">
              <span className="hb-k">Market Regime</span>
              <span className="hb-v mono bold">{stats.market_regime}</span>
            </div>
            <div className="hb-item">
              <span className="hb-k">Latency</span>
              <span className="hb-v mono">{stats.latency_ms} ms</span>
            </div>
            <div className="hb-item">
              <span className="hb-k">Watchlist / Universe</span>
              <span className="hb-v mono">{stats.watchlist_count} / {stats.universe_count}</span>
            </div>
            <div className="hb-item">
              <span className="hb-k">Replay Queue</span>
              <span className="hb-v mono">{stats.replay_queue_depth}</span>
            </div>
            <div className="hb-item">
              <span className="hb-k">Signals Evaluated Today</span>
              <span className="hb-v mono">{stats.signals_evaluated_today}</span>
            </div>
            <div className="hb-item">
              <span className="hb-k">Orders Today</span>
              <span className="hb-v mono">{stats.orders_today}</span>
            </div>
          </div>

          {/* Last Event Detail */}
          {lastEvent && (
            <div className="hb-event-box">
              <div className="hb-event-head">
                <span className="hb-k">Last Event</span>
                <span className="hb-ago mono">{formatAgo(heartbeat?.ts)}</span>
              </div>
              <div className="hb-event-body">
                <span className="hb-event-title">{lastEvent.title}</span>
                {lastEvent.symbol && <span className="hb-chip mono">{lastEvent.symbol}</span>}
                {lastEvent.strategy && <span className="hb-chip muted">{lastEvent.strategy}</span>}
              </div>
              <div className="hb-event-detail faint">{lastEvent.detail}</div>
            </div>
          )}

          {/* Subsystem Timestamps */}
          <div className="hb-subsystems">
            <div className="hb-sub-k">Subsystem Timestamps</div>
            <div className="hb-sub-row"><span className="faint">Last Scan:</span> <span className="mono">{formatAgo(timestamps.last_scan)}</span></div>
            <div className="hb-sub-row"><span className="faint">Last Strategy:</span> <span className="mono">{formatAgo(timestamps.last_strategy)}</span></div>
            <div className="hb-sub-row"><span className="faint">Last Order:</span> <span className="mono">{formatAgo(timestamps.last_order)}</span></div>
          </div>
        </div>
      )}
    </div>
  );
}
