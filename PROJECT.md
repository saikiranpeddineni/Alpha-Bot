# Project: AlphaBot / AlphaDesk SRE, Strategy, Regime, ML & Architecture Hardening

## Architecture
AlphaBot / AlphaDesk is an event-driven systematic equity trading platform with:
- **Core Execution**: `engine.py` (orchestrating tick loop, order router mixin, reconciliation mixin, state machine, safety modes).
- **Position & SRE Protection**: `position_brain.py` (PIS floors, ratchet stops), broker sync ack & rollback, feed watchdog.
- **Strategies & Scanner**: `strategies/` (ORB protected baseline, Momentum, MeanReversion), `scanner.py` with sector ETF breakdown health checks.
- **Portfolio & Risk**: `portfolio/performance.py` (`StrategyStats` rolling expectancy & Sharpe), `risk/manager.py` (dynamic strategy risk scaling & Momentum cap).
- **Regime Engine**: `regime_matrix.py` (6 unified regimes) wired directly into `SignalPipeline` (`pipeline.py`) and `engine_evaluation.py`.
- **Machine Learning**: `prediction.py` (MetaLabelEngine with `CalibratedClassifierCV`, `class_weight='balanced'`), `trainer.py` (specialists with class balance metadata).
- **Telemetry & Persistence**: `shadow_ledger.py` with non-blocking `AsyncWriteQueue` and 30-second TTL drop guard under SQLite lock contention.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Orphan Auto-Adopt & Drain Latch | 3-confirmed mismatch ticks trigger bi-directional reconciliation with audit logging (`WARN_ORPHAN_ADOPTED` / `WARN_ORPHAN_DRAINED`) | M1 | Survey R1 |
| 2 | Trailing Stop Broker Sync Ack | Synchronous broker ack required before advancing in-memory PIS floors; rollback on timeout/failure | M1 | Survey R1 |
| 3 | Feed Watchdog & Safe Mode | Per-symbol `last_bar_received_at` marks `DATA_STALE` at 90s; triggers `SAFE_MODE` if all feeds stall >120s | M1 | Survey R1 |
| 4 | Dynamic Strategy Risk Allocation | Scale strategy risk by rolling expectancy & Sharpe; maintain 50% cap on Momentum until expectancy > 0 | M2 | Survey R2 |
| 5 | Sector ETF Breakdown Suppression | Scanner evaluates sector ETF breakdown (< -1% with down EMA20) to suppress/penalize long breakout setups | M2 | Survey R2 |
| 6 | ORB Core Protection Verification | Opening Range Breakout core entry rules, timing (15m), and stop/target geometry verified 100% untouched | M2 | Survey R2 |
| 7 | Unified 6-Regime Matrix Wiring | Wire `RegimeMatrix` into `SignalPipeline.process()` and `engine_evaluation.py` for direction & risk scaling | M3 | Survey R3 |
| 8 | Opening Cold-Start Bar Starvation Guard | Suppress candidate signals before 09:45 ET with <15 intraday bars, tag `warmup_pending`, exempt exits | M3 | Survey R3 |
| 9 | ML Model Calibration (CalibratedClassifierCV) | Integrate isotonic/sigmoid calibration in `MetaLabelEngine.train` to eliminate probability clustering | M4 | Survey R4 |
| 10 | ML Class Balance & Balanced Weighting | Enforce `class_weight='balanced'` in HGB/MetaLabel/specialists and log class balance metadata | M4 | Survey R4 |
| 11 | Non-Blocking Async Write Queue | Telemetry & shadow ledger queue with 30s TTL drop guard to prevent SQLite lock contention | M5 | Survey R5 |
| 12 | Monolithic Engine Deconstruction | Extract `order_router.py` and `reconciliation.py` mixins while preserving 100% backward compatibility | M5 | Survey R5 |
| 13 | Fast Targeted Test Suite & Git Push | Run fast targeted test suites (<30s) across all new features, commit & push to `Alphadesk_002` | M6 | Survey R1-R5 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Execution Engine & Broker SRE Hardening | Features 1, 2, 3 | none | IN_PROGRESS (Worker 1) |
| M2 | Strategy & Portfolio Dynamic Allocation | Features 4, 5, 6 | none | IN_PROGRESS (Worker 2) |
| M3 | Market Regime Unification & Cold-Start Guard | Features 7, 8 | none | IN_PROGRESS (Worker 2) |
| M4 | ML Model Calibration & Class Balance | Features 9, 10 | none | IN_PROGRESS (Worker 3) |
| M5 | Architecture & Concurrency Deconstruction | Features 11, 12 | M1 | IN_PROGRESS (Worker 1 & 3) |
| M6 | Targeted Verification & Git Integration | Feature 13 | M1, M2, M3, M4, M5 | PLANNED |

## Interface Contracts
### `engine.py` ↔ `order_router.py` & `reconciliation.py`
- `TradingEngineOrderRouterMixin` provides `_submit_order`, `_cancel_order`, `_replace_order`, `_route_child_bracket`.
- `TradingEngineReconciliationMixin` provides `_reconcile_positions`, `_adopt_orphan_position`, `_drain_orphan_position`, `_check_feed_freshness`.
- `TradingEngine` inherits from both mixins, preserving full attribute access (`self.db`, `self.broker`, `self.positions`).

### `position_brain.py` ↔ `engine_evaluation.py`
- `_sync_broker_protective_stop(symbol, new_stop, old_floor)` returns boolean ack.
- If ack is False/Timeout, call `rollback_protection_floor(symbol, old_floor)`.

### `regime_matrix.py` ↔ `pipeline.py` & `engine_evaluation.py`
- `RegimeMatrix.evaluate(market_context)` returns `RegimeState(regime_type, allow_long, allow_short, risk_scalar)`.
- `SignalPipeline.process()` filters or scales candidate signals by `regime_state`.

### `shadow_ledger.py` ↔ `AsyncWriteQueue`
- `AsyncWriteQueue(maxsize=5000, ttl_seconds=30)` drops expired items when database is locked, logging dropped event counts.

## Code Layout
- `backend/app/core/engine.py`: Core tick loop, feed watchdog, state transitions.
- `backend/app/core/order_router.py`: Extracted order routing mixin.
- `backend/app/core/reconciliation.py`: Extracted position reconciliation & orphan latch mixin.
- `backend/app/core/position_brain.py`: Stop ratchet & floor rollback logic.
- `backend/app/portfolio/performance.py`: `StrategyStats` rolling expectancy & Sharpe.
- `backend/app/risk/manager.py`: Dynamic strategy risk scaling & Momentum cap.
- `backend/app/market_scanner.py`: Sector ETF health check & breakdown penalty.
- `backend/app/strategies/orb.py`: Protected ORB logic (100% untouched core).
- `backend/app/core/regime_matrix.py`: Unified 6-regime classification.
- `backend/app/core/pipeline.py`: Signal filtering & cold-start bar check.
- `backend/app/ai/prediction.py`: MetaLabel calibration & class weights.
- `backend/app/ai/trainer.py`: Specialist training & class balance metadata.
- `backend/app/core/shadow_ledger.py`: Non-blocking async write queue with TTL guard.
