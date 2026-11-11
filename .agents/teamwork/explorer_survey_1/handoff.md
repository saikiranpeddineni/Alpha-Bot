# Handoff Report: R1 & R5 (Execution Engine Explorer)

**Date**: 2026-11-11T14:06:30Z  
**From**: Execution Engine Explorer (`explorer_survey_1`)  
**To**: Orchestrator (`0f504116-8059-460c-a369-364fad59fafa`)  
**Active Git Branch**: `Alphadesk_002`  
**Handoff Type**: Hard (Mission Complete)  

---

## 1. Observation
1. **Engine Monolith**:
   - `backend/app/core/engine.py` contains 7,656 lines.
   - Mixin architecture is already established: `backend/app/core/engine_evaluation.py:105` defines `TradingEngineEvaluationMixin` and `backend/app/core/engine_halts.py` defines `TradingEngineHaltsMixin`.
2. **Reconciliation & Tick Loop**:
   - `engine.py:1258` runs `_tick(self, now=None)`.
   - `engine.py:1298` calls `await self._reconcile_pending_submissions(now=now)`.
   - `engine.py:1308` calls `await self._check_system_heartbeat(now)`.
   - `engine.py:6240–6472` performs Monitor 8/9 broker vs local position reconciliation.
   - `engine.py:6372` increments `self._position_reconciliation_mismatch_streak += 1` on unexplained mismatches (filtered by in-flight orders).
   - `engine.py:6417` checks `if self._position_reconciliation_mismatch_streak >= 3:` but only performs partial reconciliation (liquidation on broker orphans, no explicit `WARN_ORPHAN_ADOPTED` / `WARN_ORPHAN_DRAINED` audit log tags).
3. **Trailing Stop Ratchet & PIS Floor Desynchronization**:
   - `backend/app/pis/position_brain.py:502, 538`: Sets `self._current_stop_floor = target_stop` synchronously during `step()`, before broker communication occurs.
   - `backend/app/core/engine_evaluation.py:915`: Calls `await self._sync_broker_protective_stop(symbol, new_floor)` and updates `_lot.stop_loss = new_floor` only if it returns `True`.
   - `backend/app/brokers/alpaca_hardened.py:388`: Catches `BrokerTimeoutError` and returns `None` without advancing the order.
   - Consequence: On broker API timeout or rejection, `_lot.stop_loss` is not updated, but `PositionBrain._current_stop_floor` is already advanced in memory. On subsequent bars, PIS uses the unconfirmed phantom floor as `previous_floor`.
4. **Market Data Feed Watchdog**:
   - `backend/app/core/engine.py:1666`: `new_closed = await self.data.poll(list(active_symbols))`.
   - `backend/app/core/engine.py:1704`: `self._last_data_bar_ts[sym_upper] = now`.
   - `backend/app/core/engine.py:1738`: Blocks new entries on individual symbols when `elapsed > gap_threshold`, but lacks per-symbol `last_bar_received_at` with 90s `DATA_STALE` status and lacks the global >120s market hours stall transition into `SAFE_MODE`.
5. **Order Routing & Close Operations**:
   - `engine.py:3780–3988` defines `_close_position_guarded`.
   - `engine.py:4034–4114` defines `_reconcile_pending_closes`.
   - `engine.py:4122–4340` defines `_reconcile_positions`.
   - `engine.py:4349–4464` defines `_reconcile_pending_submissions`.
   - `engine.py:4473–4625` defines `manual_order`.
   - `engine.py:4627–4679` defines `close_symbol`.
   - `engine.py:2615–2810` routes entry orders in `_tick`.

---

## 2. Logic Chain
1. From Observation 1, because `TradingEngine` already inherits from `TradingEngineEvaluationMixin` and `TradingEngineHaltsMixin`, extracting `order_router.py` (`TradingEngineOrderRouterMixin`) and `reconciliation.py` (`TradingEngineReconciliationMixin`) preserves the identical class hierarchy and runtime dispatch semantics.
2. From Observation 5, all order execution methods (`_close_position_guarded`, `manual_order`, `close_symbol`, entry order routing) share state via `self` attributes (`self.broker`, `self.db`, `self._pending_close_symbols`, `self.pending_reconciliation_orders`). By using mixins, no state bridging is required and 100% backward compatibility is guaranteed for all 952 existing tests.
3. From Observation 2, position mismatches are already filtered by in-flight orders and tracked via `_position_reconciliation_mismatch_streak`. Implementing the 3-streak bi-directional latch with `WARN_ORPHAN_ADOPTED` and `WARN_ORPHAN_DRAINED` audit log entries provides full SRE auditability and handles both ghost local lots and broker-held split/external orphans.
4. From Observation 3, coupling `PositionBrain._current_stop_floor` advancement directly to the return status of `_sync_broker_protective_stop` (with rollback on `False` or timeout) eliminates phantom in-memory floors and guarantees PIS protection matches broker-verified truth.
5. From Observation 4, tracking `last_bar_received_at` in the tick loop allows precise per-symbol `DATA_STALE` marking at 90s, and triggers engine `SAFE_MODE` when all active feeds stall for > 120s during market hours.

---

## 3. Caveats
- No source files have been modified (strict compliance with read-only Explorer role).
- Backtest/Replay clock mode (`ReplayClock`) intentionally bypasses real-time staleness gates (`_is_replay` guard); this must be preserved during implementation so deterministic replay tests remain unaffected.
- The 952 existing backend test suite cases must be run after implementation to verify zero regressions.

---

## 4. Conclusion
The architectural survey for R1 and R5 is complete with exact line-level findings and implementation designs documented in `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\explorer_survey_1\survey_r1_r5.md`.
The implementation team can immediately execute:
1. Extraction of `backend/app/core/order_router.py` and `backend/app/core/reconciliation.py` as mixins into `TradingEngine`.
2. Bi-directional orphan adopt/drain reconciliation with 3-streak latch and audit events (`WARN_ORPHAN_ADOPTED`, `WARN_ORPHAN_DRAINED`).
3. Synchronous broker trailing stop verification before PIS floor advancement, with automatic rollback on timeout.
4. Per-symbol `last_bar_received_at` watchdog marking feeds `DATA_STALE` at 90s and transitioning the engine into `SAFE_MODE` if all feeds stall > 120s during market hours.

---

## 5. Verification Method
1. **Existing Test Suite Command**:
   ```powershell
   pytest backend/tests/test_engine.py backend/tests/test_orphan_reconciliation.py backend/tests/test_008_fail_closed_reconciliation.py backend/tests/test_011_ambiguous_submit_failures.py backend/tests/test_144_engine_disconnect_fail_closed.py
   ```
2. **Files to Inspect After Implementation**:
   - `backend/app/core/order_router.py` (new)
   - `backend/app/core/reconciliation.py` (new)
   - `backend/app/core/engine.py` (mixins attached, facades re-exported)
   - `backend/app/core/engine_evaluation.py` (trailing stop ack & rollback)
   - `backend/app/pis/position_brain.py` (staged/rollback floor interface)
   - `backend/app/pis/engine_adapter.py` (adapter rollback call)
3. **Invalidation Conditions**:
   - Any break in `TradingEngine` public API methods or attributes (`pending_reconciliation_orders`, `_close_position_guarded`, `_reconcile_positions`).
   - Any regression in the existing 952 backend test suite.
