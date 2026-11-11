# Original User Request

## Initial Request — 2026-11-11T13:42:53Z

Evolve the existing AlphaBot / AlphaDesk systematic equity trading platform by resolving all remaining production, execution, regime, ML, and architectural vulnerabilities identified in the master audit.

Working directory: `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master`
Integrity mode: development

## Requirements

### R1. Execution Engine & Broker SRE Hardening
- Implement bi-directional orphan auto-adopt/drain reconciliation in the engine tick loop, firing after 3 confirmed mismatch ticks with explicit audit event logging (`WARN_ORPHAN_ADOPTED` / `WARN_ORPHAN_DRAINED`).
- Enforce synchronous broker acknowledgement on trailing stop replacements before advancing in-memory PIS protection floors, rolling back on API timeouts.
- Implement per-symbol `last_bar_received_at` watchdog marking feeds `DATA_STALE` after 90 seconds and transitioning the engine into `SAFE_MODE` if all feeds stall for > 120 seconds during market hours.

### R2. Strategy & Portfolio Dynamic Allocation
- Dynamically scale strategy risk allocation based on rolling expectancy and Sharpe ratio, maintaining the 50% risk cap on Momentum until rolling expectancy turns positive.
- Implement sector ETF health checks in the market scanner, penalizing or suppressing breakout setups in equities whose sector ETF is breaking down.
- Maintain protected baseline status for ORB (Opening Range Breakout) — core entry logic must never be modified.

### R3. Market Regime Unification & Cold-Start Guard
- Wire the unified 6-regime `RegimeMatrix` directly into `SignalPipeline.process()` and `engine_evaluation.py` to dictate strategy eligibility, directional trade allowance (`allow_long`, `allow_short`), and risk scaling.
- Enforce opening cold-start bar starvation protection: suppress candidate signals before 09:45 AM ET for symbols with fewer than 15 intraday bars with a `warmup_pending` flag.

### R4. Machine Learning Model Reliability & Calibration
- Integrate `CalibratedClassifierCV` (isotonic/sigmoid) into the model training and serialization pipeline to eliminate probability clustering.
- Record class balance metadata and enforce balanced weighting (`class_weight='balanced'`) during MetaLabel and specialist training.

### R5. Architecture & Concurrency Technical Debt
- Implement an asynchronous non-blocking write queue for telemetry and shadow ledger events with a 30-second TTL drop guard to prevent SQLite lock contention.
- Deconstruct the monolithic `engine.py` by extracting `order_router.py` and `reconciliation.py` service boundaries while maintaining 100% backward compatibility.

## Acceptance Criteria

### Test & Regression Guardrails
- [ ] Existing 952 backend test suite cases continue to pass with 0 regressions.
- [ ] New unit tests pass for regime matrix integration, orphan reconciliation latch, stop ratchet confirmation, websocket stall watchdog, and async write queue.
- [ ] No git commits are made directly to `master` or `main`; all changes committed to active branch `Alphadesk_002`.
- [ ] Code Modification & Comment Preservation Policy strictly applied to all touched code (old code commented out with structured headers, never deleted).
- [ ] ORB core entry conditions, timing, and stop/target geometry remain completely untouched.


## Follow-up — 2026-11-11T14:03:39Z

URGENT OPERATOR DIRECTIVE:
1. The entire process must complete within the next 15-20 minutes before market open.
2. DO NOT run the full 952 backend test suite now — the operator explicitly instructed that the full test suite can be run after market hours.
3. Accelerate implementation across R1-R5 directly: prioritize fast code changes following the comment preservation policy, run only fast targeted tests (under 30 seconds) verifying the changes, and commit and push to branch Alphadesk_002 immediately.
4. Wrap up and deliver final completion before market open.
