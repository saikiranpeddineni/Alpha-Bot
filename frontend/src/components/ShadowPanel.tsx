// Shadow Analysis — every rejected entry signal, with full forward-return detail.
// Consumes GET /api/shadow-ledger (live). No high-level rollups only: every row
// shows the exact prices at +5/15/30/60m, MFE/MAE and the precise gate reason.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { fmtPct, fmtTime, pnlClass } from "../format";
import type { ShadowEntry, ShadowGateSummary } from "../types";

const GATE_LABEL: Record<string, string> = {
  gated_tick: "Data/clock gate",
  pipeline: "AI pipeline",
  opportunity: "Opportunity economics",
  strategy_overlap: "Pipeline conflict",
  portfolio_or_risk: "Portfolio/risk",
  reconciliation: "Reconciliation",
  behavioral: "Loop detector",
  broker_rejection: "Broker rejection",
};

const VERDICT_STYLE: Record<string, string> = {
  MISSED_WINNER: "var(--red)",
  AVOIDED_LOSER: "var(--green)",
  NEUTRAL: "var(--muted)",
  NO_BARS: "var(--muted)",
};

function SideTag({ action }: { action: string | null }) {
  const short = (action ?? "").toUpperCase().endsWith("SHORT") ? "short" : "long";
  return <span className={`side-tag ${short}`}>{short}</span>;
}

function GateTag({ gate }: { gate: string }) {
  return <span className="muted">{GATE_LABEL[gate] ?? gate}</span>;
}

function ReasonCell({ text }: { text: string | null }) {
  if (!text) return <span className="faint">—</span>;
  const shown = text.length > 90 ? `${text.slice(0, 90)}…` : text;
  return <span title={text} className="muted">{shown}</span>;
}

export function ShadowPanel() {
  const [entries, setEntries] = useState<ShadowEntry[]>([]);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [globalMissed, setGlobalMissed] = useState<ShadowEntry[]>([]);
  const [globalGateStats, setGlobalGateStats] = useState<ShadowGateSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "MISSED_WINNER" | "AVOIDED_LOSER" | "pending">("all");
  const [auto, setAuto] = useState(true);

  const load = useCallback(() => {
    api
      .shadowLedger(2000)
      .then(d => {
        setEntries(d.entries);
        setSummary(d.summary);
        setGlobalMissed(d.missed_winners);
        setGlobalGateStats(d.gate_summary ?? []);
        setError(null);
        setLoading(false);
      })
      .catch(e => {
        setError((e as Error).message ?? "failed to load shadow ledger");
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    load();
    if (!auto) return;
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load, auto]);

  const resolved = useMemo(() => entries.filter(e => e.resolved === 1), [entries]);
  const missed = useMemo(() => globalMissed, [globalMissed]);
  const avoided = useMemo(
    () => resolved.filter(e => e.verdict === "AVOIDED_LOSER").sort((a, b) => (a.fav_pct_60m ?? 0) - (b.fav_pct_60m ?? 0)),
    [resolved],
  );
  const pending = entries.filter(e => e.resolved !== 1);

  const gateStats = useMemo(() => {
    if (globalGateStats.length > 0) return globalGateStats;
    const m = new Map<string, ShadowGateSummary>();
    for (const e of entries) {
      const g = m.get(e.gate) ?? { gate: e.gate, total: 0, missed: 0, avoided: 0, avg_forward_move: null };
      g.total += 1;
      if (e.verdict === "MISSED_WINNER") g.missed += 1;
      if (e.verdict === "AVOIDED_LOSER") g.avoided += 1;
      m.set(e.gate, g);
    }
    return [...m.values()];
  }, [entries, globalGateStats]);

  const totalRejections = gateStats.reduce((sum, gate) => sum + gate.total, 0);
  const resolvedCount = Object.entries(summary)
    .filter(([verdict]) => verdict !== "PENDING")
    .reduce((sum, [, count]) => sum + count, 0);
  const missedCount = summary.MISSED_WINNER ?? missed.length;
  const avoidedCount = summary.AVOIDED_LOSER ?? avoided.length;
  const pendingCount = Math.max(0, totalRejections - resolvedCount);
  const allDetails = useMemo(() => {
    const unique = new Map<number, ShadowEntry>();
    for (const entry of [
      ...missed,
      ...avoided,
      ...resolved.filter(e => e.verdict === "NEUTRAL" || e.verdict === "NO_BARS"),
      ...pending,
    ]) {
      unique.set(entry.id, entry);
    }
    return [...unique.values()];
  }, [missed, avoided, resolved, pending]);

  const shown =
    filter === "all"
      ? allDetails
      : filter === "pending"
        ? pending
        : filter === "MISSED_WINNER"
          ? missed
          : resolved.filter(e => e.verdict === filter);

  return (
    <div style={{ padding: 10, display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Chip label="Rejections" value={String(totalRejections || entries.length)} />
        <Chip label="Resolved" value={String(resolvedCount || resolved.length)} />
        <Chip label="Missed winners" value={String(missedCount)} color="var(--red)" />
        <Chip label="Avoided losers" value={String(avoidedCount)} color="var(--green)" />
        <Chip label="Pending resolution" value={String(pendingCount || pending.length)} />
        <span style={{ flex: 1 }} />
        <label className="faint" style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} /> auto 30s
        </label>
        <button className="btn sm" onClick={load}>Refresh</button>
      </div>

      {loading && <div className="empty-state">Loading shadow ledger…</div>}
      {error && <div className="empty-state" style={{ color: "var(--red)" }}>Error: {error}</div>}

      {!loading && !error && (
        <>
          <table className="grid">
            <thead>
              <tr>
                <th>Gate</th>
                <th className="r">Rejections</th>
                <th className="r">Missed</th>
                <th className="r">Avoided</th>
                <th className="r">Avg forward move</th>
              </tr>
            </thead>
            <tbody>
              {gateStats.map(g => (
                <tr key={g.gate}>
                  <td><GateTag gate={g.gate} /></td>
                  <td className="r num">{g.total}</td>
                  <td className="r num">{g.missed || ""}</td>
                  <td className="r num">{g.avoided || ""}</td>
                  <td className={`r num ${pnlClass(g.avg_forward_move)}`}>
                    {g.avg_forward_move != null ? fmtPct(g.avg_forward_move) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="row" style={{ gap: 8 }}>
            {(["all", "MISSED_WINNER", "AVOIDED_LOSER", "pending"] as const).map(f => (
              <button key={f} className={`btn sm ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
                {f === "all" ? "All" : f === "pending" ? "Pending" : f.replace("_", " ").toLowerCase()}
              </button>
            ))}
          </div>

          <div style={{ maxHeight: 420, overflow: "auto" }}>
            <table className="grid">
              <thead>
                <tr>
                  <th>Time (ET)</th>
                  <th>Sym</th>
                  <th>Side</th>
                  <th>Strategy</th>
                  <th>Gate</th>
                  <th className="r">Entry</th>
                  <th className="r">+5m</th>
                  <th className="r">+15m</th>
                  <th className="r">+30m</th>
                  <th className="r">+60m</th>
                  <th className="r">Fwd %</th>
                  <th className="r">MFE%</th>
                  <th className="r">MAE%</th>
                  <th className="r">R</th>
                  <th className="r">ML</th>
                  <th className="r">Meta</th>
                  <th className="r">Score</th>
                  <th>Verdict</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(e => (
                  <tr key={`${e.id}-${e.candidate_id ?? ""}`}>
                    <td className="num faint" title={`Recorded ${fmtTime(e.created_at)}`}>
                      {fmtTime(e.decision_bar_timestamp_utc ?? e.created_at)}
                    </td>
                    <td><b>{e.symbol}</b></td>
                    <td><SideTag action={e.action} /></td>
                    <td className="muted">{e.strategy}</td>
                    <td><GateTag gate={e.gate} /></td>
                    <td className="r num">{e.signal_price?.toFixed(2) ?? "—"}</td>
                    <td className="r num faint">{e.px_5m?.toFixed(2) ?? ""}</td>
                    <td className="r num faint">{e.px_15m?.toFixed(2) ?? ""}</td>
                    <td className="r num faint">{e.px_30m?.toFixed(2) ?? ""}</td>
                    <td className="r num faint">{e.px_60m?.toFixed(2) ?? ""}</td>
                    <td className={`r num ${pnlClass(e.fav_pct_60m)}`}>{fmtPct(e.fav_pct_60m)}</td>
                    <td className="r num">{fmtPct(e.mfe_pct_60m)}</td>
                    <td className="r num faint">{fmtPct(e.mae_pct_60m)}</td>
                    <td className={`r num ${pnlClass(e.r_fav_60m)}`}>{e.r_fav_60m?.toFixed(2) ?? ""}</td>
                    <td className="r num faint">{e.ml_probability != null ? `${(e.ml_probability * 100).toFixed(0)}%` : ""}</td>
                    <td className="r num faint">{e.meta_prob != null ? `${(e.meta_prob * 100).toFixed(0)}%` : ""}</td>
                    <td className="r num faint">{e.trade_score?.toFixed(1) ?? ""}</td>
                    <td style={{ color: VERDICT_STYLE[e.verdict ?? ""] ?? undefined }}>{(e.verdict ?? "pending").replace("_", " ")}</td>
                    <td><ReasonCell text={e.reason} /></td>
                  </tr>
                ))}
                {shown.length === 0 && (
                  <tr><td colSpan={19} className="empty-state">No entries for this filter yet</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="faint">
            Forward window: 60 minutes after the decision. R = favorable move in multiples of the signal's
            initial risk (entry→stop). Pending rows resolve automatically once 60 bars exist.
          </div>
        </>
      )}
    </div>
  );
}

function Chip({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <span style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "4px 10px", fontSize: 13 }}>
      <span className="faint">{label}: </span>
      <strong style={color ? { color } : undefined}>{value}</strong>
    </span>
  );
}
