from __future__ import annotations

import collections
import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from app.db.database import Database
from app.core.logging import applog
from app.strategies.memory import _canonical_realized_r
from app.config import DATA_DIR


def generate_eod_reflection_report(db: Database, now: datetime | None = None, engine: Any | None = None) -> str:
    """Analyzes the performance of Champion vs active Challengers and generates a comparative EOD reflection report.
    If a Challenger satisfies statistical promotion rules, automatically promotes it to production.
    """
    if now is None:
        now = datetime.now(UTC)

    if not db:
        return "# EOD Reflection Report\n\nNo database instance available for reflection."

    try:
        # Fetch last 5000 signals to compare
        records = db.get_shadow_signals(limit=5000)
    except Exception as exc:
        applog.error("reflection", f"Failed to retrieve shadow signals for EOD reflection: {exc}")
        return "# EOD Reflection Report\n\nFailed to retrieve shadow signals from database."

    # Group records by candidate_id to build matched cohorts
    candidate_map = collections.defaultdict(dict)
    for r in records:
        cand_id = r.get("candidate_id")
        pol_id = r.get("policy_id") or "champion"
        if cand_id:
            candidate_map[cand_id][pol_id] = r

    # Determine unique policies present in the records
    policies = set()
    for r in records:
        if r.get("policy_id"):
            policies.add(r["policy_id"])
    policies.discard("champion")

    # Helper function to get config fingerprint
    import hashlib
    import json
    def get_config_fingerprint(snapshot: dict) -> str:
        cleaned = {k: v for k, v in snapshot.items() if k not in ("snapshot_at", "snapshot_version")}
        serialized = json.dumps(cleaned, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    # Get active champion config fingerprint
    def get_champion_config_fingerprint(db_inst) -> str:
        snapshot = db_inst.get_setting("active_policy_snapshot")
        if not snapshot:
            from app.config import get_settings
            settings = get_settings()
            snapshot = {
                "policy_id": "champion",
                "ml_threshold": settings.ml_confidence_threshold,
                "score_threshold_offset": 0.0,
                "risk_config": settings.risk_config().model_dump()
            }
        return get_config_fingerprint(snapshot)

    champion_config_fp = get_champion_config_fingerprint(db)

    # 1. Global Champion Stats (for the baseline report view)
    champ_global_total = 0
    champ_global_approved = 0
    champ_global_rejected = 0
    champ_global_wins = 0
    champ_global_total_r = 0.0

    for cand_id, pmap in candidate_map.items():
        if "champion" in pmap:
            r = pmap["champion"]
            champ_global_total += 1
            if r.get("final_decision") == "pipeline_approved":
                champ_global_approved += 1
                strategy = r.get("strategy")
                outcomes = r.get("outcomes")
                if outcomes:
                    realized_r = _canonical_realized_r(strategy, outcomes)
                    if realized_r is not None:
                        champ_global_total_r += realized_r
                        if realized_r > 0.0:
                            champ_global_wins += 1
            else:
                champ_global_rejected += 1

    champ_global_win_rate = (champ_global_wins / champ_global_approved * 100.0) if champ_global_approved > 0 else 0.0
    champ_global_expectancy = (champ_global_total_r / champ_global_approved) if champ_global_approved > 0 else 0.0

    # Build markdown report header
    report = []
    report.append("# EOD Policy Performance Reflection Report")
    report.append(f"Generated at: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
    report.append("| Policy ID | Role | Candidates | Approved | Vetoed | Win Rate | Expectancy (R) | Total Realized (R) | Matched Champ Expectancy | Edge Over Champ |")
    report.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

    # Add Champion global baseline row
    report.append(
        f"| **champion** | Champion | {champ_global_total} | {champ_global_approved} | {champ_global_rejected} | "
        f"{champ_global_win_rate:.2f}% | {champ_global_expectancy:+.4f}R | {champ_global_total_r:+.2f}R | {champ_global_expectancy:+.4f}R | +0.0000R |"
    )

    # 2. Challenger Matched Cohort Analysis
    best_policy = None
    best_expectancy = -999.0
    best_edge = -999.0
    best_nominee_stats = None

    # Load active challenger configs to calculate config fingerprints
    from app.strategies.challenger import get_active_challengers
    challengers = {c.policy_id: c for c in get_active_challengers()}

    for policy_id in sorted(policies):
        # Find matched candidates (opportunites evaluated by both Challenger and Champion)
        matched_cands = [
            cand_id for cand_id, pmap in candidate_map.items()
            if policy_id in pmap and "champion" in pmap
        ]
        
        if not matched_cands:
            continue

        # Challenger matched stats
        chall_total = 0
        chall_approved = 0
        chall_rejected = 0
        chall_wins = 0
        chall_total_r = 0.0

        for cand_id in matched_cands:
            r = candidate_map[cand_id][policy_id]
            chall_total += 1
            if r.get("final_decision") == "pipeline_approved":
                chall_approved += 1
                strategy = r.get("strategy")
                outcomes = r.get("outcomes")
                if outcomes:
                    realized_r = _canonical_realized_r(strategy, outcomes)
                    if realized_r is not None:
                        chall_total_r += realized_r
                        if realized_r > 0.0:
                            chall_wins += 1
            else:
                chall_rejected += 1

        chall_win_rate = (chall_wins / chall_approved * 100.0) if chall_approved > 0 else 0.0
        chall_expectancy = (chall_total_r / chall_approved) if chall_approved > 0 else 0.0

        # Champion matched stats specifically on the same matched cohort
        champ_matched_approved = 0
        champ_matched_total_r = 0.0

        for cand_id in matched_cands:
            r = candidate_map[cand_id]["champion"]
            if r.get("final_decision") == "pipeline_approved":
                champ_matched_approved += 1
                strategy = r.get("strategy")
                outcomes = r.get("outcomes")
                if outcomes:
                    cr = _canonical_realized_r(strategy, outcomes)
                    if cr is not None:
                        champ_matched_total_r += cr

        champ_matched_expectancy = (champ_matched_total_r / champ_matched_approved) if champ_matched_approved > 0 else 0.0
        edge = chall_expectancy - champ_matched_expectancy

        report.append(
            f"| **{policy_id}** | Challenger | {chall_total} | {chall_approved} | {chall_rejected} | "
            f"{chall_win_rate:.2f}% | {chall_expectancy:+.4f}R | {chall_total_r:+.2f}R | {champ_matched_expectancy:+.4f}R | {edge:+.4f}R |"
        )

        # Statistical nomination criteria check (matched approved count >= 15 and edge >= +0.15R)
        if chall_approved >= 15 and edge >= 0.15:
            # E1 Loop Safety: Check if policy is in cooling-off period after rejection
            in_cooling = False
            last_rejection = db.get_last_policy_rejection(policy_id)
            if last_rejection:
                rejected_at = last_rejection["rejected_at"]
                if rejected_at.tzinfo is None:
                    rejected_at = rejected_at.replace(tzinfo=UTC)
                if (now - rejected_at).total_seconds() < 7 * 86400:
                    in_cooling = True
                    applog.info("reflection", f"Skipping auto-nomination for policy {policy_id} because it is within 7-day cooling-off period after rejection.")

            if not in_cooling and chall_expectancy > best_expectancy:
                best_expectancy = chall_expectancy
                best_edge = edge
                best_policy = policy_id
                
                # Fingerprints calculations
                sorted_cands = sorted(list(matched_cands))
                cohort_str = ",".join(sorted_cands)
                cohort_fp = hashlib.sha256(cohort_str.encode("utf-8")).hexdigest()[:16]
                
                c_obj = challengers.get(policy_id)
                config_fp = get_config_fingerprint(c_obj.to_snapshot()) if c_obj else "unknown_config"
                
                best_nominee_stats = {
                    "approved_count": chall_approved,
                    "expectancy": chall_expectancy,
                    "edge_over_champion": edge,
                    "champion_expectancy": champ_matched_expectancy,
                    "cohort_fingerprint": cohort_fp,
                    "config_fingerprint": config_fp,
                    "champion_config_fingerprint": champion_config_fp,
                }

    report.append("")
    report.append("## Promotion Analysis & Nominations")

    if best_policy and best_nominee_stats:
        stats = best_nominee_stats
        edge = stats["edge_over_champion"]
        champ_matched_exp = stats["champion_expectancy"]
        report.append(
            f"> [!IMPORTANT]\n"
            f"> **PROMOTION CANDIDATE NOMINATED — Human Approval Required**\n"
            f"> Policy `{best_policy}` has satisfied the statistical nomination criteria:\n"
            f"> - Simulated Trades: {stats['approved_count']} (Required: >= 15)\n"
            f"> - Expectancy: {stats['expectancy']:+.4f}R (Matched Champion: {champ_matched_exp:+.4f}R, Edge: {edge:+.4f}R, Required: >= +0.15R)\n"
            f"> - Cohort Fingerprint: `{stats['cohort_fingerprint']}`\n"
            f"> - Config Fingerprint: `{stats['config_fingerprint']}`\n"
            f"> \n"
            f"> **No production configuration has been changed.**\n"
            f"> Review the shadow performance dashboard and use `POST /strategies/promote` to approve."
        )

        try:
            db.save_policy_nominee({
                "policy_id": best_policy,
                "nominated_at": now.isoformat(),
                "approved_count": stats["approved_count"],
                "expectancy": round(stats["expectancy"], 4),
                "edge_over_champion": round(edge, 4),
                "champion_expectancy": round(champ_matched_exp, 4),
                "status": "pending",
                "cohort_fingerprint": stats["cohort_fingerprint"],
                "config_fingerprint": stats["config_fingerprint"],
                "champion_config_fingerprint": stats["champion_config_fingerprint"],
            })
            applog.info(
                "reflection",
                f"Nomination recorded: policy {best_policy!r} nominated for promotion review. "
                f"Expectancy: {stats['expectancy']:+.4f}R (edge: {edge:+.4f}R over Matched Champion)."
            )
        except Exception as exc:
            applog.error("reflection", f"Failed to save policy nomination for {best_policy}: {exc}")

        # Fire a warning-severity UI insight
        try:
            publish = getattr(engine, "publish", None) if engine is not None else None
            if publish:
                publish("insight", {
                    "id": f"nomination-{best_policy}-{now.timestamp()}",
                    "ts": now.isoformat(),
                    "severity": "warning",
                    "title": f"Promotion Candidate Nominated: {best_policy}",
                    "detail": (
                        f"Policy '{best_policy}' qualified for promotion review on matched cohort. "
                        f"Expectancy: {stats['expectancy']:+.4f}R vs Matched Champ {champ_matched_exp:+.4f}R "
                        f"(edge: {edge:+.4f}R) over {stats['approved_count']} trades. "
                        f"No production config changed. Use POST /strategies/promote to approve."
                    ),
                })
        except Exception as exc:
            applog.error("reflection", f"Failed to publish nomination insight for {best_policy}: {exc}")
    else:
        # Fallback recommendations logic for manual review if rules aren't met
        manual_best = None
        manual_expectancy = -999.0
        manual_champ_exp = 0.0
        for policy_id in sorted(policies):
            matched_cands = [
                cand_id for cand_id, pmap in candidate_map.items()
                if policy_id in pmap and "champion" in pmap
            ]
            if not matched_cands:
                continue
            chall_approved = [
                candidate_map[cand_id][policy_id] for cand_id in matched_cands
                if candidate_map[cand_id][policy_id].get("final_decision") == "pipeline_approved"
            ]
            if len(chall_approved) < 2:
                continue
            chall_total_r = sum(_canonical_realized_r(r.get("strategy"), r.get("outcomes")) or 0.0 for r in chall_approved if r.get("outcomes"))
            expectancy = chall_total_r / len(chall_approved)

            champ_approved = [
                candidate_map[cand_id]["champion"] for cand_id in matched_cands
                if candidate_map[cand_id]["champion"].get("final_decision") == "pipeline_approved"
            ]
            champ_total_r = sum(_canonical_realized_r(r.get("strategy"), r.get("outcomes")) or 0.0 for r in champ_approved if r.get("outcomes"))
            champ_exp = champ_total_r / len(champ_approved) if len(champ_approved) > 0 else 0.0

            if expectancy > manual_expectancy:
                manual_expectancy = expectancy
                manual_best = policy_id
                manual_champ_exp = champ_exp

        if manual_best and manual_expectancy > manual_champ_exp:
            report.append(
                f"> [!NOTE]\n"
                f"> **Challenger Outperformance (Manual Review Required):**\n"
                f"> Policy `{manual_best}` achieved higher expectancy ({manual_expectancy:+.4f}R) than Matched Champion `{manual_champ_exp:+.4f}R` over matched trades.\n"
                f"> **Recommendation:** Consider promoting `{manual_best}` to production manually after verification."
            )
        else:
            report.append(
                f"> [!NOTE]\n"
                f"> **Champion Remains Undefeated:**\n"
                f"> Champion expectancy ({champ_global_expectancy:+.4f}R) is superior to or equal to all qualified challenger policies.\n"
                f"> **Recommendation:** Keep current Champion policy active. Continue monitoring walk-forward testing."
            )

    # 3. Portfolio-level Virtual Simulation Comparison
    try:
        from app.portfolio.simulator import VirtualPortfolioSimulator, SimulationConfig
        from app.config import get_settings
        
        settings = get_settings()
        risk_cfg = settings.risk_config()
        sim_cfg = SimulationConfig(
            starting_capital=settings.paper_starting_cash or 100000.0,
            risk_per_trade_pct=risk_cfg.risk_per_trade_pct,
            max_position_pct=risk_cfg.max_position_pct,
            max_open_positions=risk_cfg.max_open_positions,
            max_positions_per_sector=risk_cfg.max_positions_per_sector,
            max_correlation=getattr(risk_cfg, "max_correlation_penalty", 0.75),
            slippage_bps=getattr(settings, "paper_slippage_bps", 3.0),
            commission_per_share=getattr(settings, "paper_commission_per_share", 0.005),
        )
        
        sim = VirtualPortfolioSimulator(sim_cfg)
        
        report.append("\n## Portfolio-Level Virtual Simulation Comparison")
        report.append(
            f"Simulated under identical risk and capital constraints: "
            f"Starting Capital: ${sim_cfg.starting_capital:,.2f} | "
            f"Max Open Positions: {sim_cfg.max_open_positions} | "
            f"Risk/Trade: {sim_cfg.risk_per_trade_pct}% | "
            f"Slippage: {sim_cfg.slippage_bps} bps\n"
        )
        report.append(
            "| Policy ID | Return | Max Drawdown | Win Rate | Profit Factor | Sharpe | Sortino | CVaR 95% | Turnover | Trades |"
        )
        report.append(
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
        )
        
        for policy_id in sorted(list(policies) + ["champion"]):
            policy_sigs = [
                r for r in records 
                if (r.get("policy_id") or "champion") == policy_id 
                and r.get("final_decision") == "pipeline_approved"
            ]
            
            sim_res = sim.run(policy_sigs)
            m = sim_res.metrics
            
            ret_str = f"{m['total_return_pct']:+.2f}%"
            dd_str = f"{m['max_drawdown_pct']:.2f}%"
            wr_str = f"{m['win_rate_pct']:.1f}%"
            pf_str = f"{m['profit_factor']:.2f}" if m['profit_factor'] is not None else "N/A"
            sh_str = f"{m['sharpe']:.2f}" if m['sharpe'] is not None else "N/A"
            so_str = f"{m['sortino']:.2f}" if m['sortino'] is not None else "N/A"
            cvar_str = f"{m['cvar_95']:.4f}" if m['cvar_95'] is not None else "N/A"
            to_str = f"{m['turnover']:.2f}"
            
            report.append(
                f"| **{policy_id}** | {ret_str} | {dd_str} | {wr_str} | {pf_str} | {sh_str} | {so_str} | {cvar_str} | {to_str} | {m['total_trades']} |"
            )
    except Exception as e:
        applog.error("reflection", f"Failed to run portfolio simulation comparison for reflection: {e}")

    report_text = "\n".join(report)

    # Save to file
    try:
        report_dir = DATA_DIR / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / f"reflection_{now.strftime('%Y%m%d')}.md"
        report_file.write_text(report_text, encoding="utf-8")
        applog.info("reflection", f"EOD reflection report compiled and saved to: {report_file.as_posix()}")
    except Exception as exc:
        applog.error("reflection", f"Failed to save reflection report file: {exc}")

    return report_text
