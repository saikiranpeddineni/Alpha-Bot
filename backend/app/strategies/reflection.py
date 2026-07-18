from __future__ import annotations

import collections
import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from app.db.database import Database
from app.core.logging import applog
from app.strategies.memory import _canonical_realized_r


def generate_eod_reflection_report(db: Database, now: datetime | None = None, engine: Any | None = None) -> str:
    """Analyzes the performance of Champion vs active Challengers and generates a comparative EOD reflection report.
    If a Challenger satisfies statistical promotion rules, automatically promotes it to production.
    """
    if now is None:
        now = datetime.now(UTC)

    try:
        # Fetch last 5000 signals to compare
        records = db.get_shadow_signals(limit=5000)
    except Exception as exc:
        applog.error("reflection", f"Failed to retrieve shadow signals for EOD reflection: {exc}")
        return "# EOD Reflection Report\n\nFailed to retrieve shadow signals from database."

    # Group statistics by policy_id
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
        strategy = r.get("strategy")

        stats = policy_stats[policy_id]
        stats["total"] += 1
        
        if final_decision == "pipeline_approved":
            stats["approved"] += 1
            if outcomes:
                # Use strategy-specific horizon for canonical realized R (Fix 8)
                realized_r = _canonical_realized_r(strategy, outcomes)
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
    report.append(f"Generated at: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
    report.append("| Policy ID | Role | Candidates | Approved | Vetoed | Win Rate | Expectancy (R) | Total Realized (R) |")
    report.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

    champ_stats = policy_stats.get("champion")
    champ_expectancy = 0.0
    if champ_stats and champ_stats["approved"] > 0:
        champ_expectancy = champ_stats["total_r"] / champ_stats["approved"]

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

        # Statistical promotion candidate check:
        # Require at least 15 trades (approved candidates) and expectancy >= Champion expectancy + 0.15R
        if policy_id != "champion" and approved >= 15:
            if expectancy >= champ_expectancy + 0.15:
                if expectancy > best_expectancy:
                    best_expectancy = expectancy
                    best_policy = policy_id

    report.append("")
    report.append("## Promotion Analysis & Nominations")

    if best_policy:
        stats = policy_stats[best_policy]
        edge = best_expectancy - champ_expectancy
        report.append(
            f"> [!IMPORTANT]\n"
            f"> **PROMOTION CANDIDATE NOMINATED — Human Approval Required**\n"
            f"> Policy `{best_policy}` has satisfied the statistical nomination criteria:\n"
            f"> - Simulated Trades: {stats['approved']} (Required: >= 15)\n"
            f"> - Expectancy: {best_expectancy:+.4f}R (Champion: {champ_expectancy:+.4f}R, Edge: {edge:+.4f}R, Required: >= +0.15R)\n"
            f"> \n"
            f"> **No production configuration has been changed.**\n"
            f"> Review the shadow performance dashboard and use `POST /strategies/promote` to approve."
        )

        # Persist the nomination to DB for the dashboard to surface (no production config touched)
        try:
            db.save_policy_nominee({
                "policy_id": best_policy,
                "nominated_at": now.isoformat(),
                "approved_count": stats["approved"],
                "expectancy": round(best_expectancy, 4),
                "edge_over_champion": round(edge, 4),
                "champion_expectancy": round(champ_expectancy, 4),
                "status": "pending",
            })
            applog.info(
                "reflection",
                f"Nomination recorded: policy {best_policy!r} nominated for promotion review. "
                f"Expectancy: {best_expectancy:+.4f}R (edge: {edge:+.4f}R over Champion)."
            )
        except Exception as exc:
            applog.error("reflection", f"Failed to save policy nomination for {best_policy}: {exc}")

        # Fire a warning-severity UI insight for operator review (no config mutation)
        try:
            publish = getattr(engine, "publish", None) if engine is not None else None
            if publish:
                publish("insight", {
                    "id": f"nomination-{best_policy}-{now.timestamp()}",
                    "ts": now.isoformat(),
                    "severity": "warning",
                    "title": f"Promotion Candidate Nominated: {best_policy}",
                    "detail": (
                        f"Policy '{best_policy}' has qualified for promotion review. "
                        f"Expectancy: {best_expectancy:+.4f}R vs Champion {champ_expectancy:+.4f}R "
                        f"(edge: {edge:+.4f}R) over {stats['approved']} simulated trades. "
                        f"No production config changed. Use POST /strategies/promote to approve."
                    ),
                })
        except Exception as exc:
            applog.error("reflection", f"Failed to publish nomination insight for {best_policy}: {exc}")
    else:
        # Fallback recommendations logic for manual review if rules aren't met
        manual_best = None
        manual_expectancy = -999.0
        for policy_id, stats in policy_stats.items():
            if policy_id == "champion" or stats["approved"] < 2:
                continue
            expectancy = stats["total_r"] / stats["approved"]
            if expectancy > manual_expectancy:
                manual_expectancy = expectancy
                manual_best = policy_id

        if manual_best and manual_expectancy > champ_expectancy:
            report.append(
                f"> [!NOTE]\n"
                f"> **Challenger Outperformance (Manual Review Required):**\n"
                f"> Policy `{manual_best}` achieved higher expectancy ({manual_expectancy:+.4f}R) than Champion `{champ_expectancy:+.4f}R` over {policy_stats[manual_best]['approved']} trades.\n"
                f"> **Recommendation:** Consider promoting `{manual_best}` to production manually after verification."
            )
        else:
            report.append(
                f"> [!NOTE]\n"
                f"> **Champion Remains Undefeated:**\n"
                f"> Champion expectancy ({champ_expectancy:+.4f}R) is superior to or equal to all qualified challenger policies.\n"
                f"> **Recommendation:** Keep current Champion policy active. Continue monitoring walk-forward testing."
            )

    report_text = "\n".join(report)

    # Save to file
    try:
        report_dir = Path("backend/data/reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / f"reflection_{now.strftime('%Y%m%d')}.md"
        report_file.write_text(report_text, encoding="utf-8")
        applog.info("reflection", f"EOD reflection report compiled and saved to: {report_file.as_posix()}")
    except Exception as exc:
        applog.error("reflection", f"Failed to save reflection report file: {exc}")

    return report_text
