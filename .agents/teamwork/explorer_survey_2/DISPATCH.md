## 2026-11-11T13:47:25Z
You are an Explorer subagent for the AlphaBot / AlphaDesk systematic equity trading platform hardening initiative.

## Identity & Directories
- Archetype: teamwork_preview_explorer
- Role: Strategy Regime Explorer
- Working directory: c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\explorer_survey_2
- Original Request: c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\ORIGINAL_REQUEST.md

You MUST read c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\ORIGINAL_REQUEST.md before starting.

## Scope & Mission: Survey R2 & R3 (Strategy, Dynamic Allocation, Regime Unification & Cold-Start)
Investigate the codebase thoroughly and report exact architectural findings for:
1. R2: Strategy & Portfolio Dynamic Allocation
   - Dynamic risk scaling: locate strategy risk allocation, portfolio sizing, and performance tracking. How are rolling expectancy and Sharpe ratio calculated or tracked? How to scale risk dynamically, and enforce the 50% risk cap on Momentum until rolling expectancy turns positive?
   - Sector ETF health checks: inspect market scanner (`market_scanner.py` or related). Where are sector ETFs defined or tracked? How should breakdown detection be implemented to penalize or suppress breakout setups in equities whose sector ETF is breaking down?
   - ORB Core Protection: Locate all Opening Range Breakout (ORB) strategy code, files, entry rules, timing, stop/target geometry. Document exact boundaries and verify how to ensure ORB core remains 100% untouched.
2. R3: Market Regime Unification & Cold-Start Guard
   - Unified 6-regime `RegimeMatrix`: locate `RegimeMatrix` implementation. Trace where and how it must be wired directly into `SignalPipeline.process()` and `engine_evaluation.py`. How does it dictate strategy eligibility, directional trade allowance (`allow_long`, `allow_short`), and risk scaling?
   - Opening cold-start bar starvation protection: inspect market open bar ingestion and signal generation before 09:45 AM ET. How are intraday bar counts tracked? How to suppress candidate signals when fewer than 15 intraday bars are present, tagging them with `warmup_pending`?
3. Existing Tests:
   - Identify all existing tests covering strategies, portfolio risk allocation, market scanner, regime matrix, and signal pipeline.

## Deliverables
- Write your detailed findings to `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\explorer_survey_2\survey_r2_r3.md`.
- Write your completion handoff to `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\explorer_survey_2\handoff.md`.
- Update `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\explorer_survey_2\progress.md` with your progress and liveness heartbeat.
- Send a completion message via send_message to the orchestrator when finished.


## 2026-11-11T14:09:38Z
**Context**: Urgent Operator Directive (Accelerate for Market Open)
**Content**: Operator has issued an urgent directive: implementation across R1-R5 must complete within 15-20 minutes before market open. Please wrap up your findings immediately, write survey_r2_r3.md and handoff.md with the key file paths, line numbers, and implementation plan for R2 & R3 (dynamic risk scaling, sector ETFs, ORB protection boundaries, RegimeMatrix wiring, cold-start bar check), and send completion message now.
**Action**: Deliver handoff.md immediately.
