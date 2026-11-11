# Handoff Report: R4 (ML Model Calibration & Class Balancing) & R5 (Async Write Queue & TTL Drop Guard)

**Worker**: `worker_m4_m5` (Worker 3 - ML & Telemetry Worker)  
**Initiative**: AlphaBot / AlphaDesk Master Hardening  
**Target Branch**: `Alphadesk_002`  
**Date**: 2026-11-11  
**Status**: Task Complete (Hard Handoff)  
**Reference Dispatch**: `.agents/teamwork/worker_m4_m5/DISPATCH.md`  

---

## 1. Observation

1. **R4 ML Calibration Vulnerability**:
   - In `backend/app/ai/prediction.py:1458-1466`, `clf_full` in `MetaLabelEngine.train()` was fitted directly on `(X_all, y_all)` and dumped to joblib uncalibrated. Uncalibrated gradient boosted trees produce heavy probability clustering in the 0.48–0.52 range, which previously caused 62 winning setups (averaging +2.34R) to be rejected by the meta-label filter.
   - In `backend/app/ai/prediction.py:241-248`, `_make_classifier("hgb")` instantiated `HistGradientBoostingClassifier` without `class_weight="balanced"`.
   - In `backend/app/ai/trainer.py:645-649`, `_fit_hgb()` returned `HistGradientBoostingClassifier` without `class_weight="balanced"`.
   - In `backend/app/ai/trainer.py:766-785`, specialist model artifacts only recorded long-side distribution counts (`y_l`), omitting short-side class balance, class weighting flags, and calibration parameters.

2. **R5 SQLite Lock Contention & Write Queue**:
   - In `backend/app/core/shadow_ledger.py:146`, `self._queue` was an unbounded `queue.Queue()`.
   - In `backend/app/core/shadow_ledger.py:332-350`, `_writer_loop` caught `sqlite3.OperationalError` (e.g. `database is locked`) and retried indefinitely in a `while not committed` loop without a time-to-live (TTL) expiration on queued events, risking queue accumulation and prolonged write lock contention between the writer thread and `resolve_pending()`.

3. **Targeted Test Execution & Results**:
   - Command: `.\.venv\Scripts\python.exe -m pytest backend/tests/test_shadow_ledger.py backend/tests/test_async_write_queue.py backend/tests/test_ml_calibration_hardening.py backend/tests/test_ml_calibration.py -v`
   - Result: **30 passed, 0 failed in 13.77s**.
   - Sub-suite execution times:
     - `test_shadow_ledger.py`: 18 passed in 5.15s
     - `test_async_write_queue.py`: 6 passed in 1.63s
     - `test_ml_calibration_hardening.py`: 5 passed in 12.76s
     - `test_ml_calibration.py`: 1 passed in 1.25s

---

## 2. Logic Chain

1. **Eliminating Probability Clustering via `CalibratedClassifierCV`**:
   - Monotonic decision tree boundary scores in LightGBM and HistGradientBoosting produce bunched probability estimates around prior sample base rates.
   - Integrating `CalibratedClassifierCV(estimator=clf_full, method=calib_method, cv=3)` (selecting `"isotonic"` for $N \ge 1000$ and `"sigmoid"` for $N < 1000$) transforms raw margin scores into true empirical probabilities.
   - If minimum class count is small ($<2$), a graceful fallback to `clf_full` prevents stratification errors.
   - Serializing `final_model` into `provenance["model"]` and invalidating `self._classifiers[cache_key]` ensures live inference uses calibrated probabilities.

2. **Enforcing Class Balance Parity**:
   - Adding `class_weight="balanced"` to `_make_classifier("hgb")` and `_fit_hgb()` forces the gradient boosting loss function to penalize errors on minority trade signals inversely proportional to class frequency.
   - Recording `imbalance_ratio`, `class_weight="balanced"`, `calibration_method`, and `is_calibrated` in both MetaLabel provenance and Specialist model artifacts provides full auditability and enables downstream drift monitoring.

3. **Preventing SQLite Lock Contention via `AsyncWriteQueue` with 30-Second TTL Drop Guard**:
   - SQLite WAL mode permits concurrent readers but only one active writer transaction. Concurrent writes between the shadow ledger writer thread and `resolve_pending()` produce `sqlite3.OperationalError: database is locked`.
   - By creating `AsyncWriteQueue` bounded at `maxsize=5000` with `QueueItem(payload, enqueued_at=time.monotonic())`:
     - Items are queued non-blockingly via `put_nowait()`. If buffer capacity is reached, excess events are safely dropped and counted (`dropped_capacity_count`), preventing memory growth.
     - On dequeue, items older than 30 seconds are automatically purged (`dropped_ttl_count`), ensuring stale records are never sent to SQLite.
     - During `OperationalError: database is locked`, `filter_stale_batch()` purges items in the in-flight batch whose age exceeds 30.0s. If the entire batch expires, it is dropped, releasing the writer thread and eliminating engine tick blocking.
     - `AsyncWriteQueue` inherits from `queue.Queue` and calls `task_done()` for dropped items, ensuring `ShadowLedger.flush()` completes without timeout or deadlock.

---

## 3. Caveats

1. **Forensics Discard vs Primary Trading State**: The 30-second TTL drop guard explicitly applies to non-critical telemetry and rejected signal forensics in `shadow_ledger.db`. It intentionally discards stale rejected events during severe database lock contention to preserve throughput. Primary order execution in `alphadesk.db` is never dropped.
2. **Calibration Sample Size**: For datasets with fewer than 1000 samples, sigmoid calibration is utilized to avoid over-fitting piecewise isotonic step functions; isotonic calibration is automatically activated once candidate volume reaches $\ge 1000$ samples.

---

## 4. Conclusion

1. **R4 ML Calibration & Class Balancing**:
   - `CalibratedClassifierCV` (cv=3, isotonic for $N \ge 1000$, sigmoid for $N < 1000$) is integrated into `MetaLabelEngine.train()`.
   - `class_weight="balanced"` is enforced across all `HistGradientBoostingClassifier` instances in `prediction.py` and `trainer.py`.
   - Complete bidirectional class distributions (`n_positive_long`, `n_negative_long`, `imbalance_ratio_long`, `n_positive_short`, `n_negative_short`, `imbalance_ratio_short`), `class_weight="balanced"`, and calibration parameters are logged and serialized into model artifacts.

2. **R5 Non-Blocking Async Write Queue & TTL Drop Guard**:
   - Created `backend/app/core/async_write_queue.py` providing `AsyncWriteQueue(maxsize=5000, ttl_seconds=30.0)` and `QueueItem`.
   - Integrated `AsyncWriteQueue` into `ShadowLedger`, with batch TTL filtering and metrics logging on `sqlite3.OperationalError`.
   - Exposes operational diagnostics (`queue_depth`, `total_enqueued`, `total_written`, `dropped_ttl_count`, `dropped_capacity_count`, `lock_contention_events`) via `ledger.queue_stats`.

3. **Code Modification Policy Compliance**:
   - 100% compliant with the Code Modification & Comment Preservation Policy: all modified code has previous logic preserved in-place as structured comments.
   - All 30 targeted unit tests pass in 13.77s, well under the 30-second requirement.

---

## 5. Verification Method

To independently verify the implementation:

1. **Run Async Write Queue & Shadow Ledger Tests**:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest backend/tests/test_async_write_queue.py backend/tests/test_shadow_ledger.py -v
   ```
   *Expected Result*: All 24 tests pass in ~7 seconds.

2. **Run ML Calibration & Provenance Tests**:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest backend/tests/test_ml_calibration_hardening.py backend/tests/test_ml_calibration.py -v
   ```
   *Expected Result*: All 6 tests pass in ~14 seconds.

3. **Combined Targeted Test Suite**:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest backend/tests/test_shadow_ledger.py backend/tests/test_async_write_queue.py backend/tests/test_ml_calibration_hardening.py backend/tests/test_ml_calibration.py -v
   ```
   *Expected Result*: `30 passed in 13.77s`.
