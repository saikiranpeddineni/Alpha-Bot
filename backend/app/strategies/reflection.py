from __future__ import annotations

import collections
import json
from datetime import datetime, UTC
from pathlib import Path
from app.db.database import Database
from app.core.logging import applog


def generate_eod_reflection_report(db: Database) -> str:
    """Analyzes the performance of Champion vs active Challengers and generates a comparative EOD reflection report.
    
    Returns the markdown report content.
    """
    try:
        # Fetch last 5000 signals to compare
        records = db.get_shadow_signals(limit=5000)
    except Exception as exc:
        applog.error("reflection", f"Failed to retrieve shadow signals for EOD reflection: {exc}")
        return "# EOD Reflection Report\n\nFailed to retrieve shadow signals from database."

    # Group statistics by policy_id
    # Each stats dict holds:
    # { "total": int, "approved": int, "rejected": int, "wins": int, "total_r": float }
    policy_stats = collections.defaultdict(lambda: {
        "total": 0,
        "approved": 0,
        "rejected": 0,
        "wins": 0,
        "total_r": 0.0,
    })

    for r in records:
        policy_id = r.get("policy_id") or "unknown"
        final_decision = r.get("final_decision")
        outcomes = r.get("outcomes")

        stats = policy_stats[policy_id]
        stats["total"] += 1
        
        if final_decision == "pipeline_approved":
            stats["approved"] += 1
            # If the trade is approved, we attribute the actual outcome
            if outcomes:
                realized_r = 0.0
                if "15m" in outcomes:
                    realized_r = outcomes["15m"].get("realized_r", 0.0)
                elif "close" in outcomes:
                    realized_r = outcomes["close"].get("realized_r", 0.0)

                stats["total_r"] += realized_r
                if realized_r > 0.0:
                    stats["wins"] += 1
        else:
            stats["rejected"] += 1

    if not policy_stats:
        return "# EOD Reflection Report\n\nNo active policies or candidate records found."

    # Build markdown comparison table
    report = []
    report.append("# EOD Policy Performance Reflection Report")
    report.append(f"Generated at: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
    report.append("| Policy ID | Role | Candidates | Approved | Vetoed | Win Rate | Expectancy (R) | Total Realized (R) |")
    report.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

    # Find the best policy based on expectancy
    best_policy = None
    best_expectancy = -999.0

    for policy_id, stats in sorted(policy_stats.items()):
        total = stats["total"]
        approved = stats["approved"]
        rejected = stats["rejected"]
        wins = stats["wins"]
        total_r = stats["total_r"]
        
        win_rate = (wins / approved * 100.0) if approved > 0 else 0.0
        expectancy = (total_r / approved) if approved > 0 else 0.0
        
        role = "Champion" if policy_id == "champion" else "Challenger"

        report.append(
            f"| **{policy_id}** | {role} | {total} | {approved} | {rejected} | {win_rate:.2f}% | {expectancy:+.4f}R | {total_r:+.2f}R |"
        )

        if approved >= 2: # Require at least 2 trades to qualify for promotion comparison
            if expectancy > best_expectancy:
                best_expectancy = expectancy
                best_policy = policy_id

    report.append("")
    report.append("## Promotion Analysis & Recommendations")
    
    if best_policy and best_policy != "champion" and best_expectancy > 0.0:
        # Check champion performance
        champ_stats = policy_stats.get("champion")
        champ_expectancy = 0.0
        if champ_stats and champ_stats["approved"] > 0:
            champ_expectancy = champ_stats["total_r"] / champ_stats["approved"]

        if best_expectancy > champ_expectancy:
            report.append(
                f"> [!IMPORTANT]\n"
                f"> **Challenger Outperformance Detected!**\n"
                f"> Policy `{best_policy}` achieved higher expectancy ({best_expectancy:+.4f}R) than Champion `{champ_expectancy:+.4f}R`.\n"
                f"> **Recommendation:** Consider promoting `{best_policy}` to production after manual review of its trade logs."
            )
        else:
            report.append(
                f"> [!NOTE]\n"
                f"> **Champion Remains Undefeated:**\n"
                f"> Champion expectancy ({champ_expectancy:+.4f}R) is superior to or equal to all challenger policies.\n"
                f"> **Recommendation:** Keep current Champion policy active. Continue monitoring walk-forward testing."
            )
    else:
        report.append(
            f"> [!NOTE]\n"
            f"> **Baseline Stable:**\n"
            f"> Insufficient qualified candidate trade data (minimum 2 trades per policy) to recommend promotion.\n"
            f"> **Recommendation:** Keep current configurations active to collect more contextual data."
        )

    report_text = "\n".join(report)

    # Save to file
    try:
        report_dir = Path("backend/data/reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / f"reflection_{datetime.now(UTC).strftime('%Y%m%d')}.md"
        report_file.write_text(report_text, encoding="utf-8")
        applog.info("reflection", f"EOD reflection report compiled and saved to: {report_file.as_posix()}")
    except Exception as exc:
        applog.error("reflection", f"Failed to save reflection report file: {exc}")

    return report_text
