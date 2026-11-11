# Survey Report: ML Model Reliability (R4), Telemetry Async Queue (R5), and Test Suite Baseline

**Author**: Explorer Subagent (`explorer_survey_3` - ML & Test Baseline Explorer)  
**Date**: 2026-11-11  
**Target Branch**: `Alphadesk_002`  
**Working Directory**: `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master`  
**Reference Request**: `.agents/teamwork/ORIGINAL_REQUEST.md`

---

## 1. Executive Summary

This survey delivers the architectural investigation, empirical verification, and engineering design for **R4 (Machine Learning Model Reliability & Calibration)**, **R5 (Telemetry & Shadow Ledger Asynchronous Queue)**, and the **Test Suite & Git Environment Baseline**.

### Key Findings & Milestones:
1. **Test Suite Baseline & Git Environment**:
   - **Active Git Branch**: Verified as `Alphadesk_002` with 0 uncommitted code changes (working directory clean, only `.agents/teamwork/` untracked).
   - **Baseline Test Execution**: `963` total tests collected; **`962` passed, `1` skipped** in 468.89s (7m 48s) using `.venv\Scripts\python.exe -m pytest -q`.
   - **Skipped Test**: `backend/tests/test_pis_soak_stress.py::test_pis_soak_stress` explicitly marked `@pytest.mark.skip(reason="Legacy Architecture removed during PIS refactor")`.
2. **R4: Machine Learning Model Reliability & Calibration**:
   - **Probability Clustering Root Cause**: In `MetaLabelEngine.train` (`backend/app/ai/prediction.py:1458-1466`), the final production model (`clf_full`) is saved to disk **completely uncalibrated**. Uncalibrated gradient boosted trees produce sharp probability bunching in the 0.48–0.52 region, causing severe threshold fragility. In October 2026 forensics, this caused 62 winning setups (avg +2.34R) to be rejected.
   - **Calibration Solution**: Wrap `clf_full` with `CalibratedClassifierCV` using cross-validation (`cv=3`) with `method="isotonic"` (for $N \ge 1000$) or `method="sigmoid"` (for $N < 1000$), preserving `sample_weight=w_all`.
   - **Class Weighting Gaps**: `HistGradientBoostingClassifier` in `_make_classifier` (`prediction.py:241`), `_fit_hgb()` in `trainer.py:645`, and `train_adaptive()` in `model.py:653` do **not** set `class_weight='balanced'`. XGBoost uses a static `scale_pos_weight=1.5`. Only LightGBM set `class_weight="balanced"`.
   - **Class Balance Metadata**: Specialist models currently only record long-side counts (`n_positive`, `n_negative`), omitting short-side class balance, class weights, and imbalance ratios. MetaLabel models record raw counts but omit explicit imbalance ratios and calibration parameters from audit logs.
3. **R5: Telemetry & Shadow Ledger Asynchronous Queue**:
   - **SQLite Lock Contention**: Identified dual contention points:
     1. In `shadow_ledger.py`: The background writer thread loops `con.executemany` while the periodic asyncio `resolver_loop` executes concurrent `UPDATE` statements on the same SQLite file. In SQLite WAL mode, only one write transaction is permitted at any instant; concurrent write attempts trigger `sqlite3.OperationalError: database is locked`.
     2. In `engine.py`: The live engine tick loop executes synchronous `save_equity` and heartbeat settings writes via `asyncio.to_thread` while background order and fill reconciliation transactions touch `alphadesk.db`.
   - **Unbounded Queue Risk**: `ShadowLedger._queue` is currently an unbounded `queue.Queue()`. Stalled writes cause endless retries without TTL expiration, risking queue buildup and database thrashing.
   - **30-Second TTL Drop Guard Design**: Formulated a dedicated `AsyncWriteQueue` with a bounded buffer (`maxsize=5000`), monotonic timestamping (`enqueued_at = time.monotonic()`), and an explicit 30-second drop guard that purges stale telemetry/forensics before SQLite submission under lock contention.

---

## 2. Test Suite Baseline & Git Environment Audit

### 2.1 Git Repository Status
- **Current Branch**: `Alphadesk_002` (Verified via `git branch -a` and `git status`).
- **Remote Tracking**: `origin/Alphadesk_002` is up to date.
- **Working Tree**: Completely clean; untracked files are strictly isolated to `.agents/teamwork/`.
- **Policy Compliance**: Confirms zero commits directly to `master`/`main`; all subsequent work will commit exclusively to `Alphadesk_002`.

### 2.2 Pytest Execution & Test Counts
- **Execution Command**:
  ```powershell
  .\.venv\Scripts\python.exe -m pytest -q
  ```
- **Test Collection**: **`963` tests collected** across 59 test modules in `backend/tests/`.
- **Test Results**:
  - **Passed**: `962`
  - **Skipped**: `1`
  - **Failed**: `0`
  - **Errors**: `0`
  - **Runtime**: `468.89 seconds` (~7 min 48 sec)
- **Skipped Test Details**:
  - **File**: `backend/tests/test_pis_soak_stress.py:20`
  - **Test Name**: `test_pis_soak_stress`
  - **Reason**: `@pytest.mark.skip(reason="Legacy Architecture removed during PIS refactor")`
- **Notice on Acceptance Criteria**: The original request notes "Existing 952 backend test suite cases continue to pass". The test suite currently contains 963 tests (962 passing), representing all 952 baseline tests plus 11 recent regression tests, all 100% green.

---

## 3. R4: Machine Learning Model Reliability & Calibration

### 3.1 Architecture Overview of ML Pipeline
The platform's ML subsystem spans four primary components:
1. **Global Model (`SignalModel`)** (`backend/app/ai/model.py`):
   - Bidirectional classifier (long & short models).
   - Trained via `WalkForwardValidator` in `backend/app/ai/prediction.py`.
   - Stored as `signal_model_long.joblib` and `signal_model_short.joblib` (atomic deployment via `GenerationStore` in `generation.py`).
2. **Specialist Models** (`backend/app/ai/trainer.py:_fit_specialist`):
   - Per-symbol classifiers fitted daily on 180-day 1-minute data from `bars_cache.db`.
   - Promoted only if candidate AUC beats incumbent by $\ge 0.002$ and exceeds `min_specialist_auc` (0.53).
   - Saved as `data/models/specialists/<SYMBOL>.pkl` via `AtomicSpecialistStore`.
3. **MetaLabel Classifiers (`MetaLabelEngine`)** (`backend/app/ai/prediction.py`):
   - Strategy-specific meta-filters evaluating $P(\text{trade profitable} \mid \text{entry features})$.
   - Saved as `data/models/meta/meta_{strategy}_{side}.joblib`.
   - If meta probability < threshold (default 0.48–0.52), trade setup is rejected before reaching the Trade Score engine.
4. **Adaptive Online Models** (`backend/app/ai/model.py:train_adaptive`):
   - Fine-tunes daily models per regime (`adaptive_{regime}_long.joblib`).

---

### 3.2 Probability Clustering Vulnerability & Analysis
In tree-based gradient boosted models (LightGBM, XGBoost, HistGradientBoosting), the raw `predict_proba` values do not represent true empirical probabilities. Because the objective minimizes log-loss or gradient loss across discrete leaf partitions, predicted probabilities cluster sharply around the prior base rate (typically 0.48–0.52 in financial time series).

#### Why Clustering Breaks Trading:
- MetaLabel filtering enforces a cutoff: `approved = prob >= threshold`.
- When probabilities cluster between 0.48 and 0.52, an infinitesimal shift in input noise or leaf assignment causes setups with 65% real win-rate to output `prob = 0.479` and get discarded, while poor setups output `prob = 0.481` and pass.
- In `backend/app/ai/prediction.py:1033-1044`, comment history confirms that an uncalibrated meta-model rejected 62 winners with average $+2.34\text{R}$ because their predictions hovered in the 0.48–0.50 range.

#### Current Calibration Audit Across Modules:
| Model Component | File & Lines | Calibrated? | Calibration Method | Issues Identified |
|---|---|---|---|---|
| **Global WFV Model** | `prediction.py:627-633` | **Yes** | `CalibratedClassifierCV(method="isotonic", cv=3)` | Holdout evaluation in `trainer.py:489` uses calibrated model. |
| **Specialist Promoted** | `trainer.py:737-754` | **Partial** | `CalibratedClassifierCV(method="sigmoid", cv=3)` | Only promoted sides get calibrated. Fallback to raw on exception. |
| **MetaLabel Engine (Saved)** | `prediction.py:1458-1466` | **NO (VULNERABILITY)** | **None** | `clf_full` is fitted on `(X_all, y_all)` and dumped to joblib **without** `CalibratedClassifierCV`! |
| **MetaLabel Engine (Validation)** | `prediction.py:1380-1393` | **NO** | **None** | Validation AUC is computed on raw uncalibrated probabilities (`predict_proba_safe(clf, X_va)`). |
| **Adaptive Models** | `model.py:653, 711` | **NO** | **None** | Fitted raw without calibration. |

---

### 3.3 Class Weighting & Imbalance Audit
Financial setup datasets have severe class imbalance:
- In trend-following and breakout setups (e.g., ORB, BB Squeeze), winning bars occur in only 10%–25% of candidates.
- Unweighted models optimize accuracy by predicting the majority class (loss/no-action), suppressing trade signals.

#### Current Codebase Inspection:
1. `_make_classifier` (`backend/app/ai/prediction.py:200-248`):
   ```python
   # Line 206: LGBMClassifier has class_weight="balanced" -> OK
   return LGBMClassifier(..., class_weight="balanced", ...)

   # Line 224: XGBClassifier has scale_pos_weight=1.5 -> DEFECT!
   # Static 1.5 does not reflect true class imbalance ratio!

   # Line 241: HistGradientBoostingClassifier -> DEFECT!
   # class_weight="balanced" is NOT passed!
   ```
2. `_fit_hgb()` in Specialist Trainer (`backend/app/ai/trainer.py:645-649`):
   ```python
   def _fit_hgb() -> HistGradientBoostingClassifier:
       return HistGradientBoostingClassifier(
           max_iter=150, learning_rate=0.05, max_depth=3, min_samples_leaf=100,
           l2_regularization=5.0, early_stopping=True, random_state=42,
           # MISSING: class_weight="balanced"
       )
   ```
3. `train_adaptive` (`backend/app/ai/model.py:653, 683`):
   ```python
   # MISSING: class_weight="balanced"
   HistGradientBoostingClassifier(max_iter=100, learning_rate=0.06, max_depth=4, early_stopping=True, random_state=42)
   ```

---

### 3.4 Class Balance Metadata & Provenance Gaps
1. **Specialist Model Artifacts** (`backend/app/ai/trainer.py:766-785`):
   - The current artifact dictionary stores:
     ```python
     "n_samples": len(X_l),
     "n_positive": int(y_l.sum()),
     "n_negative": int((y_l == 0).sum()),
     "n_train": split_l,
     "n_validation": len(X_l) - split_l,
     "base_rate": round(float(y_l.mean()), 4),
     ```
   - **Gaps**:
     - Only records counts for the **long** side (`y_l`), even though specialists fit both long and short!
     - Does not record short-side positive/negative counts or short base rate.
     - Does not record `class_weight="balanced"`.
     - Does not record the imbalance ratio ($N_{\text{neg}} / N_{\text{pos}}$).
     - Does not log class balance in the primary fit log (`applog.info("trainer", ...)` at line 719).
2. **MetaLabel Model Provenance** (`backend/app/ai/prediction.py:1468-1491`):
   - Current provenance records `n_positive` and `n_negative`.
   - **Gaps**:
     - Omits `class_weight: "balanced"`.
     - Omits `imbalance_ratio`.
     - Omits `calibration_method` ("isotonic" / "sigmoid") and `calibration_cv`.
     - Does not log class balance metrics in `applog.info("meta", ...)` during training.

---

### 3.5 Proposed Engineering Design for R4

#### Step 1: Enforce `class_weight='balanced'` in all Classifiers
1. **In `backend/app/ai/prediction.py:_make_classifier`**:
   - For `HistGradientBoostingClassifier`: Add `class_weight="balanced"`.
   - For `XGBClassifier`: Compute or accept `scale_pos_weight` dynamically, or default to balanced scaling.
2. **In `backend/app/ai/trainer.py:_fit_hgb`**:
   - Add `class_weight="balanced"`.
3. **In `backend/app/ai/model.py:train_adaptive`**:
   - Add `class_weight="balanced"` to default estimators.

#### Step 2: Integrate `CalibratedClassifierCV` into MetaLabel Serialization
In `MetaLabelEngine.train` (`backend/app/ai/prediction.py:1458-1466`):
```python
# Before saving, calibrate probabilities to eliminate clustering:
calib_method = "isotonic" if len(X_all) >= 1000 else "sigmoid"
from sklearn.calibration import CalibratedClassifierCV
calibrated_clf = CalibratedClassifierCV(estimator=clf_full, method=calib_method, cv=3)
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    calibrated_clf.fit(X_all, y_all, sample_weight=w_all)
```
Store `calibrated_clf` as `"model"` in `provenance`.

#### Step 3: Record and Log Full Class Balance Metadata
1. **In `MetaLabelEngine.train`**:
   Add to `provenance`:
   ```python
   "class_weight": "balanced",
   "imbalance_ratio": round(n_neg / max(1, n_pos), 4),
   "calibration_method": calib_method,
   "calibration_cv": 3,
   ```
   Log via `applog.info("meta", ...)`:
   `[meta_train_class_balance] strategy={strategy} side={side} n_pos={n_pos} n_neg={n_neg} imbalance_ratio={ratio:.2f} class_weight=balanced calibration={calib_method}`.
2. **In `_fit_specialist` (`trainer.py`)**:
   Add to `artifact`:
   ```python
   "n_positive_long": int(y_l.sum()),
   "n_negative_long": int((y_l == 0).sum()),
   "base_rate_long": round(float(y_l.mean()), 4),
   "imbalance_ratio_long": round(int((y_l == 0).sum()) / max(1, int(y_l.sum())), 2),
   "n_positive_short": int(y_s.sum()),
   "n_negative_short": int((y_s == 0).sum()),
   "base_rate_short": round(float(y_s.mean()), 4),
   "imbalance_ratio_short": round(int((y_s == 0).sum()) / max(1, int(y_s.sum())), 2),
   "class_weight": "balanced",
   "calibration_method_long": "sigmoid" if promote_long else "none",
   "calibration_method_short": "sigmoid" if promote_short else "none",
   ```

---

## 4. R5: Telemetry & Shadow Ledger Asynchronous Queue

### 4.1 Telemetry & SQLite Database Write Architecture
The system writes to three SQLite databases:
1. `alphadesk.db` (Primary Trading State):
   - Managed by `Database` (`backend/app/db/database.py`).
   - Tables: `orders`, `trades`, `equity_snapshots`, `backtest_runs`, `app_settings`, `broker_fill_progress`, `portfolio_checkpoints`, `pis_profit_memory`.
   - SQLite WAL mode enabled with `PRAGMA synchronous=NORMAL`, `PRAGMA busy_timeout=30000`.
   - Connection pool: `pool.QueuePool(pool_size=15, max_overflow=25)`.
2. `shadow_ledger.db` (Signal Forensics):
   - Managed by `ShadowLedger` (`backend/app/core/shadow_ledger.py`).
   - Table: `shadow_entries`.
   - Records every rejected signal across all gates (`pipeline`, `meta_label`, `trade_score`, `portfolio_or_risk`, `behavioral`).
3. `bars_cache.db` (Market Data Cache):
   - Managed by `BarStore` (`backend/app/data/store.py`).

---

### 4.2 SQLite Lock Contention Analysis

#### Contention Point 1: Shadow Ledger Writer Thread vs Resolver Loop
- **The Mechanism**:
  - `shadow_ledger.record()` pushes rejected candidate dicts into `self._queue`.
  - A dedicated background thread (`_writer_loop`) drains batches of up to 200 rows and executes `con.executemany(sql, batch)` followed by `con.commit()`.
  - Concurrently, the periodic `resolver_loop` (`shadow_ledger.py:698-711`) wakes up every 60 seconds and calls `resolve_pending(data_manager)`.
  - `resolve_pending` opens a separate connection via `self._connect()` and executes multiple `UPDATE shadow_entries SET resolved=1, ... WHERE id=?` queries and commits them.
- **The Lock Failure**:
  - In SQLite (even with WAL mode), only **one write transaction** can hold the exclusive lock at any moment.
  - When `resolve_pending` commits an update batch, the writer thread's `con.commit()` fails with `sqlite3.OperationalError: database is locked`.
  - Look at lines 332-350 in `shadow_ledger.py`:
    ```python
    while not committed and not self._stop.is_set():
        try:
            con.executemany(sql, batch)
            con.commit()
            committed = True
        except sqlite3.OperationalError as e:
            with suppress(sqlite3.Error):
                con.rollback()
            attempt += 1
            self._stop.wait(min(5.0, 0.2 * attempt))
    ```
  - While retrying, the writer thread is blocked. Incoming records accumulate in `self._queue`.

#### Contention Point 2: Unbounded Queue & Stale Forensics Accumulation
- `self._queue` is currently initialized as `queue.Queue()` (unbounded size).
- There is **no time-to-live (TTL) expiration** on queued rows.
- If lock contention persists for 30–60 seconds during high market volatility:
  - Thousands of rejected signals pile up in memory.
  - When the lock finally clears, the writer thread executes massive batch inserts for events that occurred minutes ago, keeping the SQLite database locked even longer and starving other readers/writers.

#### Contention Point 3: Live Engine Tick Loop Telemetry & Equity Contention
- In `backend/app/core/engine.py:2851`:
  ```python
  await asyncio.to_thread(self.db.save_equity, account.equity, account.cash, self.mode, now)
  ```
- In `backend/app/core/engine.py:6084`:
  ```python
  await asyncio.to_thread(self.db.set_setting, "heartbeat_write_check", now.isoformat())
  ```
- If the tick loop executes `save_equity` while an order execution or fill is being committed in another thread, SQLite locks can delay the execution event loop tick! Telemetry and analytics must never contend with order execution.

---

### 4.3 Design of the Asynchronous Non-Blocking Write Queue with 30-Second TTL Drop Guard

To eliminate lock contention and guarantee non-blocking execution, we design an **`AsyncWriteQueue`** specifically tailored for telemetry and shadow ledger events.

```
       [Engine / Pipeline / Telemetry]
                     |
         record() / enqueue() [Non-blocking put_nowait]
                     v
       ==============================
       |      AsyncWriteQueue       |
       |  Bounded Buffer (max=5000) |
       |  Items tagged: enqueue_ts  |
       ==============================
                     |
             Worker Loop Drain
                     |
                     v
          [TTL Check: age > 30s?]
             /                \
        YES (Age > 30s)    NO (Age <= 30s)
           /                    \
       DROP ITEM           Retain in Batch
   (dropped_ttl_count++)         |
                                 v
                       con.executemany(...)
                       con.commit()
```

#### Core Architectural Specifications:
1. **Item Enqueue Contract**:
   - Each queued event is wrapped as `QueueItem(payload: dict, enqueued_at: float = time.monotonic())`.
   - `enqueue(payload)` is strictly non-blocking: uses `queue.put_nowait()`.
   - If the queue is at capacity (`maxsize=5000`), drops oldest or new item and increments `dropped_capacity_count`.
2. **30-Second TTL Drop Guard**:
   - When the worker dequeues a batch, it evaluates:
     ```python
     now = time.monotonic()
     valid_batch = []
     for item in raw_batch:
         if now - item.enqueued_at > 30.0:
             dropped_ttl_count += 1
         else:
             valid_batch.append(item.payload)
     ```
   - If SQLite lock contention causes a batch to fail on commit, before sleeping and retrying, the worker filters out any items whose `now - enqueued_at > 30.0`.
   - Stale records expire silently (with metrics logging) rather than creating a permanent backlog.
3. **Database Concurrency Isolation**:
   - In `shadow_ledger.py`, writer thread uses `PRAGMA busy_timeout=5000` and exponential backoff with jitter on `OperationalError`.
   - If `attempt >= 6` (~15s) and TTL expires, the batch is safely dropped, freeing the writer thread.
4. **Observable Metrics & Diagnostics**:
   - Expose queue health in `status()` and telemetry payloads:
     - `queue_depth`: Current pending items.
     - `total_enqueued`: Cumulative events received.
     - `total_written`: Cumulative rows committed.
     - `dropped_ttl_count`: Items dropped due to 30s expiration.
     - `dropped_capacity_count`: Items dropped due to full buffer.
     - `lock_contention_events`: Count of `sqlite3.OperationalError` caught.

---

## 5. Implementation Blueprints & File Reference Matrix

### 5.1 File Reference Matrix

| Requirement | Affected File | Lines | Current Behavior | Target Modification |
|---|---|---|---|---|
| **R4: ML Calibration** | `backend/app/ai/prediction.py` | 1458–1470 | `clf_full.fit(X_all, y_all)` saved raw without calibration | Wrap in `CalibratedClassifierCV(cv=3, method=calib_method)` |
| **R4: ML Calibration** | `backend/app/ai/trainer.py` | 737–754 | Only promoted specialists calibrated with sigmoid | Retain sigmoid/isotonic selection and verify fallback |
| **R4: Class Weighting** | `backend/app/ai/prediction.py` | 240–248 | `HistGradientBoostingClassifier` lacks `class_weight` | Add `class_weight="balanced"` |
| **R4: Class Weighting** | `backend/app/ai/trainer.py` | 645–650 | `_fit_hgb()` lacks `class_weight` | Add `class_weight="balanced"` |
| **R4: Metadata Logging** | `backend/app/ai/prediction.py` | 1468–1495 | Provenance lacks imbalance ratio & calibration info | Add `imbalance_ratio`, `class_weight`, `calibration_method` |
| **R4: Metadata Logging** | `backend/app/ai/trainer.py` | 766–785 | Specialist artifact only records long-side counts | Add short-side counts, base rate, imbalance ratios |
| **R5: Async Write Queue** | `backend/app/core/shadow_ledger.py` | 146, 254, 307–352 | Unbounded `queue.Queue()`, infinite retry on lock | Implement 30s TTL drop guard & bounded queue |
| **R5: Telemetry Writes** | `backend/app/core/engine.py` | 2851, 6084 | Direct `save_equity` calls in tick loop | Route telemetry/equity via async non-blocking write queue |

---

## 6. Risk Assessment & Verification Strategy

### 6.1 Risk Assessment
- **R4 Calibration Risk**:
  - *Risk*: `CalibratedClassifierCV` increases training time by ~3x (due to 3-fold cross validation).
  - *Mitigation*: Daily training runs in background post-market (16:15 ET). Subsampling guard (`MAX_META_SAMPLES = 1_000_000`) already protects execution time.
- **R5 TTL Drop Guard Risk**:
  - *Risk*: Under extreme prolonged SQLite lock (e.g. >30s), shadow forensics records will be dropped.
  - *Mitigation*: This is the exact desired fail-safe: dropping non-critical forensics prevents memory exhaustion and protects the primary trading loop. Primary order execution in `alphadesk.db` is never affected.

### 6.2 Independent Verification Method
1. **Test Suite Baseline**:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   ```
   Must continue to pass 962 tests (with 1 skipped).
2. **ML Calibration Unit Test**:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest backend/tests/test_ml_calibration.py -v
   ```
3. **Shadow Ledger TTL & Queue Test**:
   Create a unit test simulating a 35-second lock on `shadow.db`:
   - Enqueue 100 items.
   - Hold lock for 35s.
   - Verify items are dropped via TTL guard and `dropped_ttl_count == 100`.
   - Verify zero unhandled exceptions on the event loop.
