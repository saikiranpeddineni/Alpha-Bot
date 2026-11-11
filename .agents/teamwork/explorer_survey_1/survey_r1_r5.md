# Architectural Survey: R1 & R5 (Execution Engine, Broker SRE & Deconstruction)

**Timestamp**: 2026-11-11T14:06:00Z  
**Branch**: `Alphadesk_002`  
**Explorer**: Execution Engine Explorer (`explorer_survey_1`)  

---

## Executive Summary
This architectural survey provides the blueprint for implementing:
1. **R1**: Bi-directional orphan auto-adopt/drain reconciliation in the tick loop with 3-streak mismatch confirmation, synchronous trailing stop broker ack with PIS floor rollback on API timeouts, and a 90s/120s market data feed watchdog with `DATA_STALE` and `SAFE_MODE` latching.
2. **R5**: Deconstruction of monolithic `engine.py` (7,656 lines) by cleanly extracting `order_router.py` and `reconciliation.py` service boundaries using the repository's established Mixin pattern (`TradingEngineEvaluationMixin`, `TradingEngineHaltsMixin`), maintaining 100% backward compatibility for all callers and tests.

---

## 1. R1: Execution Engine & Broker SRE Hardening

### 1.1 Orphan Auto-Adopt/Drain Reconciliation in Tick Loop

#### Exact Code Locations
- **Tick Loop Entry Point**: `backend/app/core/engine.py:1258` (`async def _tick(self, now: datetime | None = None)`)
- **Heartbeat & Monitor 8/9**: `backend/app/core/engine.py:5776` (`async def _check_system_heartbeat(self, now: datetime)`)
  - Lines 6240–6258: Fetch `broker_positions = await self.broker.positions()` and compare with `lots = self.portfolio._lots`.
  - Lines 6340–6368: Mismatch calculation filtered by in-flight open orders (`open_orders = await self.broker.open_orders()`).
  - Lines 6370–6382: Increment `self._position_reconciliation_mismatch_streak`.
  - Lines 6417–6454: Existing partial reconciliation triggers at streak >= 3.
  - Lines 6519–6535: Confirmation latch for `SAFE_MODE` when streak >= 2.
- **Position Reconciliation Core**: `backend/app/core/engine.py:4122` (`def _reconcile_positions(self, positions: list[Position])`)

#### How Position Mismatches are Currently Detected
```python
broker_syms = {p.symbol: float(p.qty) for p in broker_positions if hasattr(p, "symbol") and abs(float(p.qty)) > 1e-9}
lots = getattr(self.portfolio, "_lots", {}) if self.portfolio else {}
db_syms = {sym: float(lot.qty) for sym, lot in lots.items() if hasattr(lot, "qty") and abs(float(lot.qty)) > 1e-9}

position_bad = broker_syms != db_syms
mismatched_syms = {s for s in set(broker_syms) | set(db_syms) if abs(broker_syms.get(s, 0.0) - db_syms.get(s, 0.0)) > 1e-6}
in_flight_syms = {getattr(o, "symbol", "").upper() for o in open_orders if getattr(o, "symbol", "")}
unexplained_mismatches = mismatched_syms - in_flight_syms
```

#### Required 3-Confirmed-Mismatch Latch & Explicit Audit Events
When `self._position_reconciliation_mismatch_streak >= 3`:
1. **Direction A (DB Ghost Lots / Absent at Broker)**:
   - Condition: `absent_at_broker = {s for s in unexplained_mismatches if s in db_syms and s not in broker_syms}`
   - Action: Drain stale local lot (`self.portfolio._lots.pop(sym, None)`), clean memory.
   - **Audit Event**:
     ```python
     applog.warning(
         "engine.reconciliation",
         f"WARN_ORPHAN_DRAINED: Local ghost lot {sym} drained from portfolio lots; broker confirmed 0 position."
     )
     ```
2. **Direction B (Broker-Held Orphans / Absent in DB)**:
   - Condition: `orphan_at_broker = {s for s in unexplained_mismatches if s in broker_syms and s not in db_syms}`
   - Policy:
     - **Auto-Adopt**: If position has identifiable entry or is unhedged, construct managed `_OpenLot` with protective stop-loss floor (1.5% default or ATR):
       ```python
       self._reconcile_positions(broker_positions)
       applog.warning(
           "engine.reconciliation",
           f"WARN_ORPHAN_ADOPTED: Broker orphan {orphan_sym} (qty={broker_syms[orphan_sym]}) adopted into active portfolio lots with protective stop {lot.stop_loss}."
       )
       ```
     - **Drain (Liquidation)**: If external/unmanaged or configured to drain, immediately issue guarded liquidation:
       ```python
       await self._close_position_guarded(orphan_sym, reason="orphan_broker_reconciliation_liquidation")
       applog.warning(
           "engine.reconciliation",
           f"WARN_ORPHAN_DRAINED: Broker orphan {orphan_sym} drained via emergency guarded liquidation."
       )
       ```
3. **Exact State Fields**:
   - `_position_reconciliation_mismatch_streak: int`
   - `reconciliation_mismatch: bool`
   - `reconciliation_failed: bool`
   - `orphan_reconciliation_events: list[dict]` (audit history exposed in engine status)

---

### 1.2 Trailing Stop Replacement & PIS Protection Floors

#### Exact Code Locations
- **PIS Decision Evaluation**: `backend/app/core/engine_evaluation.py:889–918`
- **Broker Protective Stop Sync**: `backend/app/core/engine_evaluation.py:1678–1964` (`_sync_broker_protective_stop`)
- **Hardened Broker Exit Leg Replacement**: `backend/app/brokers/alpaca_hardened.py:336–455` (`replace_exit_leg`)
- **Position Brain In-Memory Floor Ratchet**: `backend/app/pis/position_brain.py:502, 538` (`self._current_stop_floor = target_stop`)
- **PIS Engine Adapter**: `backend/app/pis/engine_adapter.py:42–245` (`evaluate_position_exit`)

#### The Architectural Defect
In `PositionBrain.step()` (`backend/app/pis/position_brain.py:502`), `self._current_stop_floor = target_stop` is set **synchronously in memory before** `engine_evaluation.py` ever calls `await self._sync_broker_protective_stop(symbol, new_floor)`.
If `replace_exit_leg` fails, times out (`BrokerTimeoutError`), or is rejected:
- `_lot.stop_loss` is NOT advanced.
- BUT `PositionBrain._current_stop_floor` HAS ALREADY BEEN ADVANCED.
- On subsequent bars, `PositionBrain` passes `previous_floor=self._current_stop_floor`, locking in a phantom stop price that Alpaca never acknowledged.

#### Required Hardening & Rollback Mechanism
1. **Staged Floor Ratchet**:
   - `PositionBrain` must store `_staged_stop_floor = target_stop` and only advance `_current_stop_floor` when explicitly confirmed via `brain.confirm_stop_floor(symbol, confirmed_price)`.
   - Alternatively, `PositionBrain` exposes `rollback_stop_floor(previous_price)`.
2. **Synchronous Broker Confirmation in `_sync_broker_protective_stop`**:
   ```python
   # In engine_evaluation.py:915:
   prev_floor = _lot.stop_loss
   if improves_floor:
       sync_ok = await self._sync_broker_protective_stop(symbol, new_floor)
       if sync_ok:
           _lot.stop_loss = new_floor
           self.pis_adapter.confirm_protection_floor(symbol, new_floor)
       else:
           # Rollback in-memory PIS floor on API failure / timeout
           self.pis_adapter.rollback_protection_floor(symbol, prev_floor)
           applog.warning("engine.exit", f"{symbol}: PIS floor rolled back to {prev_floor} due to unconfirmed broker stop replacement.")
   ```
3. **API Timeout Handling**:
   - `alpaca_hardened.py:388` catches `BrokerTimeoutError` -> returns `None`.
   - `_sync_broker_protective_stop` sees `replacement is None` -> returns `False`, cleanly triggering the rollback.

---

### 1.3 Feed Watchdog: `DATA_STALE` & `SAFE_MODE` Latching

#### Exact Code Locations
- **Bar Ingestion & Gap Gate**: `backend/app/core/engine.py:1666–1760`
  - Line 1666: `new_closed = await self.data.poll(list(active_symbols))`
  - Line 1704: `self._last_data_bar_ts[sym_upper] = now`
- **Data Freshness Monitor in Heartbeat**: `backend/app/core/engine.py:5912–6060` (Monitor 2)
  - Checks SPY/QQQ bar freshness (`fresh = (bar_age_sec <= 600)`)
- **DataManager**: `backend/app/data/manager.py:102–190` (`poll`)

#### Required Watchdog Architecture
1. **Per-Symbol `last_bar_received_at` Tracking**:
   - In `TradingEngine.__init__`: `self.last_bar_received_at: dict[str, datetime] = {}`
   - In `_tick` upon `new_closed`:
     ```python
     for sym, bars in new_closed.items():
         if bars is not None and not bars.empty:
             self.last_bar_received_at[sym.upper()] = now
     ```
2. **Per-Symbol `DATA_STALE` Threshold (90 Seconds)**:
   - For any active symbol during regular market hours (`09:30 - 16:00 ET`):
     - If `(now - last_bar_received_at[sym]).total_seconds() > 90`:
       - Mark symbol state as `DATA_STALE`.
       - Block new entries on that symbol (`self._data_gap_symbols.add(sym)`).
3. **Global Feed Stall Threshold (120 Seconds) -> `SAFE_MODE`**:
   - During regular market hours (after opening grace period of 3 minutes):
     - If `len(active_symbols) > 0` and **ALL** active symbols have `(now - last_bar_received_at[sym]).total_seconds() > 120`:
       - Transition engine:
         ```python
         self.system_health = "SAFE_MODE"
         self.allow_new_entries = False
         applog.error(
             "engine.watchdog",
             "CRITICAL: All market data feeds stalled > 120s during market hours. Transitioning engine to SAFE_MODE."
         )
         ```
     - Auto-recover to `NORMAL` once any feed receives a fresh bar.

---

## 2. R5: Monolithic `engine.py` Deconstruction

### 2.1 Current Architecture of `engine.py`
`backend/app/core/engine.py` is 7,656 lines. It already utilizes the Python Mixin pattern:
- `backend/app/core/engine_evaluation.py` defines `TradingEngineEvaluationMixin`.
- `backend/app/core/engine_halts.py` defines `TradingEngineHaltsMixin`.
- `class TradingEngine(TradingEngineEvaluationMixin, TradingEngineHaltsMixin)` combines them.

### 2.2 Clean Service Extraction Boundaries

#### Service 1: `backend/app/core/order_router.py` (`TradingEngineOrderRouterMixin`)
**Core Responsibilities & Methods to Extract**:
- `_close_position_guarded(symbol, reason, qty, strategy)` (lines 3780–3988)
- `_get_pending_close_order_ids()` (lines 3990–4004)
- `_release_pending_close(symbol, cause)` (lines 4006–4032)
- `_persist_submitted_close_order(order)` (lines 4116–4120)
- `manual_order(req: OrderRequest)` (lines 4473–4625)
- `close_symbol(symbol: str)` (lines 4627–4679)
- `_route_entry_order(decision, sig, df, now)` (extracted helper isolating lines 2615–2810 from `_tick`)

#### Service 2: `backend/app/core/reconciliation.py` (`TradingEngineReconciliationMixin`)
**Core Responsibilities & Methods to Extract**:
- `_reconcile_pending_submissions(now)` (lines 4349–4464)
- `_reconcile_pending_closes(now)` (lines 4034–4114)
- `_reconcile_positions(positions)` (lines 4122–4340)
- `_evaluate_position_reconciliation(broker_positions, lots, open_orders)` (extracted helper isolating lines 6240–6472 from `_check_system_heartbeat`)
- Bi-directional orphan auto-adopt/drain reconciliation logic.

### 2.3 100% Backward Compatibility Strategy
1. **Mixin Inheritance**:
   ```python
   # In backend/app/core/engine.py:
   from app.core.engine_evaluation import TradingEngineEvaluationMixin
   from app.core.engine_halts import TradingEngineHaltsMixin
   from app.core.order_router import TradingEngineOrderRouterMixin
   from app.core.reconciliation import TradingEngineReconciliationMixin

   class TradingEngine(
       TradingEngineEvaluationMixin,
       TradingEngineHaltsMixin,
       TradingEngineOrderRouterMixin,
       TradingEngineReconciliationMixin,
   ):
       ...
   ```
2. **Re-export Facades**:
   `backend/app/core/engine.py` re-exports all extracted symbols, constants, and helper functions:
   ```python
   from app.core.order_router import *  # noqa: F403
   from app.core.reconciliation import *  # noqa: F403
   ```
3. **No Caller Regressions**:
   Any external caller calling `engine._close_position_guarded(...)`, `engine._reconcile_positions(...)`, or accessing `engine.pending_reconciliation_orders` continues to work with zero signature or behavior changes.

---

## 3. Catalog of Existing Test Coverage
All tests covering the engine, order routing, reconciliation, trailing stops, and feeds:
- `backend/tests/test_orphan_reconciliation.py` (orphan reconciliation streak tests)
- `backend/tests/test_008_fail_closed_reconciliation.py` (fail-closed reconciliation gate tests)
- `backend/tests/test_011_ambiguous_submit_failures.py` (ambiguous order reconciliation & persistence)
- `backend/tests/test_144_engine_disconnect_fail_closed.py` (engine disconnect & recovery tests)
- `backend/tests/test_cr146f_heartbeat.py` (heartbeat monitor tests)
- `backend/tests/test_blocking_golive_p0.py` (go-live blocking tests, stop/risk retention)
- `backend/tests/test_engine.py` (core TradingEngine unit test suite)
- `backend/tests/test_pis_intelligent_profit_locking.py` (PIS stop ratchet & profit lock tests)
- `backend/tests/test_alpaca_hardened_restart_reconciliation.py` (Alpaca adapter restart and fill reconciliation)
- `backend/tests/test_engineering_ledger_watchdog.py` (telemetry/ledger watchdog tests)
- `backend/tests/test_gemini_engine_integration.py` (integration tests)
- `backend/tests/test_opportunity_engine.py` (opportunity evaluation engine)
- `backend/tests/test_070_engine_adapter_and_news_hardening.py` (PIS engine adapter tests)

---

## Conclusion
The architectural investigation is complete. The target files, line numbers, failure mechanisms, and design patterns are precisely documented above.
The implementer can proceed immediately with the changes under the Code Modification & Comment Preservation Policy on branch `Alphadesk_002`.
