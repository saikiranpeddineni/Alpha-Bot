# Architecture Survey: R2 & R3 (Strategy Allocation, Regimes & Cold-Start)

**Init Date**: 2026-11-11  
**Author**: Explorer Subagent (`explorer_survey_2`)  
**Domain**: Strategy & Portfolio Dynamic Allocation (R2) & Market Regime Unification & Cold-Start Guard (R3)  
**Target Repository**: `AlphaBot / AlphaDesk Systematic Equity Trading Platform`  

---

## 1. Executive Summary

This investigation surveys the current architecture of AlphaBot / AlphaDesk regarding:
1. **R2: Strategy & Portfolio Dynamic Allocation**: Dynamic strategy risk scaling based on rolling expectancy and Sharpe ratio, enforcing the 50% risk cap on Momentum until rolling expectancy turns positive, implementing sector ETF health checks with breakdown detection in `MarketScanner`, and establishing strict isolation boundaries to guarantee 100% immutability of core Opening Range Breakout (ORB) logic.
2. **R3: Market Regime Unification & Cold-Start Guard**: Wiring the existing standalone `RegimeMatrix` into `engine_evaluation.py` and `SignalPipeline.process()` to govern directional allowance (`allow_long`, `allow_short`), strategy eligibility, and risk scaling; and instituting cold-start bar starvation protection before 09:45 AM ET (suppressing signals with `< 15` intraday bars with a `warmup_pending` tag).

---

## 2. R2: Strategy & Portfolio Dynamic Allocation

### 2.1 Strategy Risk Allocation & Performance Tracking Architecture

#### 2.1.1 Current Codebase Locations
1. **`backend/app/portfolio/performance.py` (`StrategyPerformanceTracker`, `StrategyStats`)**:
   - `StrategyStats` (lines 30–104) accumulates per-strategy metrics: `trades`, `wins`, `losses`, `total_pnl`, `gross_profit`, `gross_loss`, `total_r`, `total_hold_minutes`, `max_consec_losses`, and `_recent = deque(maxlen=20)`.
   - Metrics currently computed:
     - `win_rate = wins / trades`
     - `profit_factor = gross_profit / abs(gross_loss)`
     - `avg_r = total_r / trades`
     - `expectancy = win_rate * avg_win + loss_rate * avg_loss` (dollar-denominated expected PnL per trade)
     - `rolling_profit_factor = gp_20 / abs(gl_20)` (over last 20 trades)
   - **Critical Gap**: `StrategyStats` does **not** track rolling expectancy (in R or $) nor rolling Sharpe ratio.
   - `StrategyPerformanceTracker` (lines 107–222) is instantiated in `TradingEngine` at `backend/app/core/engine.py:371` and updated on closed trades at `engine.py:3217`.

2. **`backend/app/portfolio/recent_performance.py` (`RecentPerformanceGovernor`)**:
   - Computes context multipliers (`factor_for(symbol, strategy, now)`) by reading closed trades from the database (`db.get_recent_trade_contexts()`).
   - Uses Bayesian shrinkage on realized R-multiples: `shrunk = (n * mean_r) / (n + k)`.
   - **Design Constraint**: `RecentPerformanceGovernor` is **tighten-only** (`scale <= 1.0`). If `shrunk >= 0`, it does nothing (`continue`), leaving factor at 1.0.

3. **`backend/app/risk/manager.py` (`RiskManager.evaluate_entry`)**:
   - Lines 1305–1317:
     ```python
     strat_risk_multipliers = getattr(cfg, "strategy_risk_multipliers", None) or {}
     strat_mult = float(strat_risk_multipliers.get(strategy.lower(), 1.0))
     if math.isfinite(strat_mult) and strat_mult > 0:
         risk_mult *= strat_mult
     ```
   - Sizing calculation:
     - Kelly path: lines 1361–1376 (`effective_kelly = min(kelly_override, kelly_cap) * risk_mult`, `risk_amount = ctx.equity * effective_kelly`).
     - Standard path: lines 1381–1436 (`effective_risk_pct = cfg.risk_per_trade_pct * risk_mult`).

4. **`backend/app/config.py` (`RiskConfig`)**:
   - Lines 258–260:
     ```python
     strategy_risk_multipliers: dict[str, float] = Field(
         default_factory=lambda: {'momentum': 0.50}
     )
     ```
   - Currently, `momentum` is statically clamped to 0.50 regardless of whether its recent performance improves.

#### 2.1.2 How Rolling Expectancy & Sharpe Ratio are Tracked Elsewhere
- In `backend/app/ai/prediction.py:277–286`:
  ```python
  expectancy = (win_rate * 1.0) - ((1 - win_rate) * 1.0)  # R units
  sharpe = float(returns.mean() / (returns.std() + 1e-8) * math.sqrt(252))
  ```
- In `backend/app/backtest/metrics.py:73`:
  ```python
  sharpe = round(mean / std * math.sqrt(TRADING_DAYS), 2)
  ```
- In `backend/scripts/run_walk_forward_validation.py:223–225`:
  `draws["expectancy_r"]` and `draws["sharpe"] = mean_return / np.sqrt(variance + 1e-8) * np.sqrt(252.0)`.

#### 2.1.3 Proposed Architectural Implementation for Dynamic Risk Scaling
1. **Extend `StrategyStats` in `backend/app/portfolio/performance.py`**:
   - Maintain a rolling window of recent trade R-multiples: `_recent_r: deque = field(default_factory=lambda: deque(maxlen=20))`.
   - On `update(trade)`: append `r = pnl / (initial_risk * qty)` to `_recent_r`.
   - Add property `rolling_expectancy_r -> float`:
     ```python
     @property
     def rolling_expectancy_r(self) -> float:
         if not self._recent_r:
             return 0.0
         return float(sum(self._recent_r) / len(self._recent_r))
     ```
   - Add property `rolling_sharpe -> float`:
     ```python
     @property
     def rolling_sharpe(self) -> float:
         if len(self._recent_r) < 5:
             return 0.0
         r_vals = np.array(self._recent_r, dtype=float)
         std = float(r_vals.std())
         if std < 1e-8:
             return 0.0
         return float((r_vals.mean() / std) * math.sqrt(252))
     ```
2. **Dynamic Risk Scaling & Momentum 50% Cap**:
   - Provide helper `get_strategy_dynamic_multiplier(strategy: str) -> float`:
     - If `strategy.lower() == "momentum"`:
       - If `stats.rolling_expectancy_r <= 0` or `stats.trades < 10`: multiplier is capped at **0.50** (enforcing the mandated 50% risk cap).
       - When `stats.rolling_expectancy_r > 0` and `stats.trades >= 10`: dynamically scale up toward `1.0` (e.g. `min(1.0, 0.50 + 0.50 * min(1.0, stats.rolling_expectancy_r))`).
     - For other strategies (`orb`, `gap_and_go`, `vwap_reversion`, `bb_squeeze`):
       - Baseline is `1.0`.
       - If `stats.rolling_expectancy_r < 0` and `len(_recent_r) >= 10`: scale down toward floor (e.g., `max(0.50, 1.0 + stats.rolling_expectancy_r)`).
       - If `stats.rolling_expectancy_r > 0.30` and `stats.rolling_sharpe > 1.5`: allow moderate reward expansion up to `1.25x`.

---

### 2.2 Sector ETF Health Checks & Breakdown Detection in Market Scanner

#### 2.2.1 Existing Codebase Definitions
1. **`backend/app/core/scanner.py`**:
   - Lines 97–100: `MARKET_UNIVERSE["sector_etfs"]` defines major sector and macro ETFs:
     `["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "GLD", "SLV", "USO", "TLT", "HYG"]`
   - Lines 109–113: `SYMBOL_SECTOR: dict[str, str]` maps symbols to 7 broad sector groups (`mega_cap`, `large_cap_tech`, `financials`, `health`, `consumer_energy`, `high_beta`, `sector_etfs`).
2. **`backend/app/strategies/sector_map.py`**:
   - Lines 21–70: `SECTOR_ETF_MAP` defines representative GICS ETFs (`XLK`, `SMH`, `SOXX`, `XLF`, `KRE`, `XLV`, `XBI`, `XLE`, `XLY`, `XLP`, `XLI`, `XLB`, `XLRE`, `XLU`, `XLC`, `SPY`, `QQQ`, `IWM`).
   - Lines 75–133: `TICKER_SECTOR_ETF: dict[str, str]` maps individual equities to their specific sector ETF proxy (e.g., `NVDA -> SOXX`, `AAPL -> XLK`, `JPM -> XLF`, `XOM -> XLE`, `AMZN -> XLY`, `GOOGL -> XLC`, etc.).
   - Line 136: `get_sector_etf(symbol: str) -> str | None`.
3. **`backend/app/strategies/gap_and_go.py:189–220`**:
   - Contains an isolated check: queries `getattr(ctx, "sector_bars", None)` for `sector_etf` and suppresses signals if ETF intraday return `< -1.5%` with downward EMA20 slope.
   - **Vulnerability**: `ctx.sector_bars` is currently unpopulated by `TradingEngineEvaluationMixin` or `engine.py`, leaving the filter inert in live execution.

#### 2.2.2 Proposed Implementation in Market Scanner (`scanner.py`)
1. **Sector Health Assessment Phase**:
   - In `MarketScanner`, fetch intraday/recent daily bars for tracked sector ETFs (`XLK`, `SOXX`, `XLF`, `XLV`, `XBI`, `XLE`, `XLY`, `XLC`, `XLI`, `XLB`, `XLU`).
   - Define **Sector Breakdown Condition**:
     - `etf_return_pct = (etf_close - etf_open) / etf_open * 100.0 < -1.0%` AND
     - `etf_close < etf_ema20` (or downward EMA slope).
   - Alternatively, evaluate multi-bar breakdown: ETF trading below its prior 3-day low or breaking its opening range low with elevated volume.
2. **Penalize or Suppress in Stage 2 Technical Scoring**:
   - In `_stage2_technical_score(candidates)`:
     - For each candidate equity, look up its `sector_etf` via `get_sector_etf(candidate["symbol"])`.
     - If the sector ETF is in confirmed breakdown:
       - Apply a severe penalty to `candidate["tech_score"]` (e.g., `tech_score *= 0.50`), or
       - Drop long breakout candidates entirely from Stage 2 promotion to ML validation (`candidate["sector_breakdown_suppressed"] = True`).
3. **Bridge into Engine Context (`StrategyContext`)**:
   - When constructing `sctx: StrategyContext` in `engine_evaluation.py:1085`, populate `sector_bars` or `sector_health: dict[str, bool]` so strategies and `SignalPipeline` can reference authoritative sector status.

---

### 2.3 ORB (Opening Range Breakout) Core Protection

#### 2.3.1 Exact File Boundaries & Configuration
- **File**: `backend/app/strategies/orb.py` (Lines 1–1303, 50,944 bytes)
- **Class**: `OpeningRangeBreakout(Strategy)`
- **Immutable Default Parameters (`default_params`, lines 187–285)**:
  - `"range_minutes": 15`
  - `"start_time": "09:45"`
  - `"end_time": "12:00"` (morning drive cutoff)
  - `"breakout_buffer_bps": 5` (0.05% buffer over range extreme)
  - `"volume_z_min": 0.0`
  - `"short_volume_z_min": 0.0`
  - `"min_acceptance_ratio": 0.50`
  - `"climax_atr_mult": 10.0`
  - `"min_stop_cushion_pct": 0.0045` (0.45% minimum stop cushion)
  - `"require_vwap_alignment": True` (Longs $\ge$ VWAP, Shorts $\le$ VWAP)
  - `"avoid_counter_trend_regime": True`
  - `"use_or_midpoint_stop": False` (full-range structural stop anchored to `or_low` for longs, `or_high` for shorts)
  - `"chop_reward_risk": 0.75`
  - `"trending_reward_risk": 3.5`
  - `"reward_risk": 2.5`
  - `"atr_stop_mult": 1.5`

#### 2.3.2 Exact Entry Rules & Geometry
1. **Range Definition**:
   - First 15 completed 1-minute bars of the session establish the opening range:
     `or_high = today_df["high"].iloc[:15].max()`
     `or_low = today_df["low"].iloc[:15].min()`
   - Requires `n_today > 15` bars.
2. **Long Breakout Conditions**:
   - `close > or_high * buffer`
   - `volume_ok` (`vol_z >= volume_z_min`)
   - `is_bullish_5m` (5-minute trend alignment)
   - `close >= vwap_val`
   - Not in counter-trend regime (`ctx.regime != Regime.TREND_DOWN` and SPY not downtrending below VWAP)
   - `close > session_open` and `close >= 5.0` (penny stock guard)
   - Not flagged as a mover loser (`getattr(ctx, "mover_bias", None) != "loser"`)
   - `drift_long_ok` (`close > daily_sma - buf`)
   - `acceptance_long >= min_acceptance` (`close_pos_in_bar >= 0.50`)
3. **Long Stop & Target Geometry**:
   - `structural_stop = or_low` (since `use_or_midpoint_stop` is False)
   - `safety_stop = close - 1.5 * atr`
   - `stop = max(structural_stop, safety_stop)`
   - Cushion clamp: `stop = min(stop, close - 0.0045 * close)`
   - `risk = close - stop`
   - `target = close + rr_mult * risk` (where `rr_mult` is 0.75 in chop, 3.5 in `TREND_UP`, 2.5 otherwise).
4. **Short Breakdown Conditions**:
   - `close < or_low / buffer`
   - `volume_ok_short`
   - `is_bearish_5m`
   - `close <= vwap_val`
   - Not in counter-trend regime (`ctx.regime != Regime.TREND_UP` and SPY not uptrending above VWAP)
   - `close < session_open`
   - Not flagged as a mover gainer
   - Symbol not in leveraged bull ETF blacklist (`TQQQ`, `UPRO`, `SOXL`, etc.)
   - `drift_short_ok`
   - `acceptance_short >= min_acceptance`
5. **Short Stop & Target Geometry**:
   - `structural_stop = or_high`
   - `safety_stop = close + 1.5 * atr`
   - `stop = min(structural_stop, safety_stop)`
   - Cushion clamp: `stop = max(stop, close + 0.0045 * close)`
   - `risk = stop - close`
   - `target = close - rr_mult * risk` (where `rr_mult` is 0.75 in chop, 3.5 in `TREND_DOWN`, 2.5 otherwise).

#### 2.3.3 Core Immutability Guarantee Protocol
- **Under no circumstances should `backend/app/strategies/orb.py` entry logic, range timing, or stop/target equations be modified.**
- All dynamic allocations, sector ETF health checks, regime posture enforcements, and cold-start starvation protections must act as **external envelopes**:
  - Gated upstream in `engine_evaluation.py` (before calling `strat.on_bar`).
  - Gated downstream in `SignalPipeline.process()` (when evaluating generated signals).
  - Gated in `RiskManager.evaluate_entry()` (when sizing positions).

---

## 3. R3: Market Regime Unification & Cold-Start Guard

### 3.1 Unified 6-Regime `RegimeMatrix` Architecture & Wiring

#### 3.1.1 Location & Specification
- **File**: `backend/app/core/regime_matrix.py` (Lines 1–111)
- **Enumeration**: `UnifiedRegimeLabel`:
  - `TRENDING_BULLISH`
  - `TRENDING_BEARISH`
  - `SIDEWAYS_RANGE`
  - `HIGH_VOLATILITY`
  - `LOW_VOLATILITY`
  - `MARKET_STRESS`
- **Output Data Structure**:
  ```python
  @dataclass(frozen=True)
  class RegimePosture:
      regime_label: UnifiedRegimeLabel
      allow_long: bool
      allow_short: bool
      risk_scalar: float
      confidence: float
      rationale: str
  ```
- **Evaluation Rules in `RegimeMatrix.evaluate(macro_state, symbol_regime, atr_percentile)`**:
  | Macro State | Symbol Tape Regime | ATR Percentile | Resulting Regime Label | Allow Long | Allow Short | Risk Scalar |
  | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
  | `STRESS` / `PANIC` / `UNKNOWN` | Any | Any | `MARKET_STRESS` | **False** | **False** | **0.00** |
  | `NORMAL` / `CAUTION` | `VOLATILE` | Any | `HIGH_VOLATILITY` | **True** | **True** | **0.50** |
  | `NORMAL` / `CAUTION` | Any | `> 0.85` | `HIGH_VOLATILITY` | **True** | **True** | **0.50** |
  | `NORMAL` / `CAUTION` | Any | `< 0.20` | `LOW_VOLATILITY` | **True** | **False** | **0.75** |
  | `NORMAL` | `TREND_UP` | `0.20 - 0.85` | `TRENDING_BULLISH` | **True** | **False** | **1.00** |
  | `CAUTION` | `TREND_UP` | `0.20 - 0.85` | `TRENDING_BULLISH` | **True** | **False** | **0.80** |
  | `NORMAL` | `TREND_DOWN` | `0.20 - 0.85` | `TRENDING_BEARISH` | **False** | **True** | **1.00** |
  | `CAUTION` | `TREND_DOWN` | `0.20 - 0.85` | `TRENDING_BEARISH` | **False** | **True** | **0.80** |
  | `NORMAL` | `RANGE` | `0.20 - 0.85` | `SIDEWAYS_RANGE` | **True** | **True** | **0.75** |
  | `CAUTION` | `RANGE` | `0.20 - 0.85` | `SIDEWAYS_RANGE` | **True** | **True** | **0.50** |

#### 3.1.2 Current State: Isolated Component
- Currently, `RegimeMatrix` is only referenced in `backend/tests/test_regime_matrix.py`. It is **not** called anywhere in `engine.py`, `engine_evaluation.py`, or `pipeline.py`.

#### 3.1.3 Wiring Blueprint into `engine_evaluation.py`
In `TradingEngineEvaluationMixin._evaluate_symbol` (around line 385):
1. Microstructure regime is detected:
   `regime, details = await asyncio.to_thread(detect_regime, df)`
2. Extract macro risk state from engine:
   ```python
   macro_state = getattr(
       getattr(self, "last_safety_decision", None),
       "market_risk_state",
       MarketRiskState.NORMAL
   )
   if isinstance(macro_state, str):
       try:
           macro_state = MarketRiskState(macro_state)
       except ValueError:
           macro_state = MarketRiskState.NORMAL
   ```
3. Evaluate unified posture:
   ```python
   from app.core.regime_matrix import RegimeMatrix
   vol_p = details.get("vol_percentile") if isinstance(details, dict) else None
   posture = RegimeMatrix.evaluate(macro_state, regime, atr_percentile=vol_p)
   ```
4. Enforce in `engine_evaluation.py`:
   - If `posture.risk_scalar == 0.0` or `posture.regime_label == UnifiedRegimeLabel.MARKET_STRESS`:
     Early return `[]` for candidate entries (preserves PIS exits).
   - Attach `posture` to `sctx: StrategyContext`.
   - Pass `posture` into `self.pipeline.process(...)`.

#### 3.1.4 Wiring Blueprint into `SignalPipeline.process()`
In `SignalPipeline.process` (`backend/app/strategies/pipeline.py:99`):
1. Accept `posture: RegimePosture | None = None` in method signature.
2. In directional trade filter (around line 125):
   ```python
   if posture is not None:
       if sig.action == SignalAction.ENTER_LONG and not posture.allow_long:
           rejected.append((sig, f"RegimeMatrix: long entries suppressed under {posture.regime_label.value} ({posture.rationale})"))
           continue
       if sig.action == SignalAction.ENTER_SHORT and not posture.allow_short:
           rejected.append((sig, f"RegimeMatrix: short entries suppressed under {posture.regime_label.value} ({posture.rationale})"))
           continue
   ```
3. Apply risk scaling:
   ```python
   if posture is not None and posture.risk_scalar > 0:
       sig.risk_multiplier = round(getattr(sig, "risk_multiplier", 1.0) * posture.risk_scalar, 4)
   ```
4. Under `UnifiedRegimeLabel.LOW_VOLATILITY`: suppress mean-reversion strategies (`vwap_reversion`, `rsi2_reversion`), permitting only breakout strategies (`orb`, `bb_squeeze`).

---

### 3.2 Opening Cold-Start Bar Starvation Protection

#### 3.2.1 Problem Analysis
- At market open (09:30 AM ET), `DataManager.frame(symbol, "1Min", limit=600)` returns up to 600 bars because rolling in-memory frames are pre-warmed with prior-session bars.
- In `engine_evaluation.py:227`:
  ```python
  if df.empty or len(df) < 5:
      return []
  ```
  Because `len(df)` includes yesterday's bars, `len(df)` evaluates to `601` at 09:31 AM ET. The guard passes immediately!
- In `pipeline.py:317`:
  ```python
  if df is not None and len(df) >= 5:
  ```
  `len(df)` again checks the full historical slice, not completed intraday bars.
- Consequently, between 09:30 and 09:45 AM ET, strategies evaluate against as few as 1 to 5 bars of current-session price action. Intraday VWAP, volume Z-scores, and ATR are noisy and unformed, resulting in cold-start whipsaw and premature false breakout triggers.

#### 3.2.2 Exact Tracking of Intraday Bar Count
Intraday bars must be explicitly counted using the active ET trading date:
```python
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

et_index = df.index.tz_convert(ET)
et_dates = et_index.date
current_date = now.astimezone(ET).date() if now else et_dates[-1]
intraday_mask = (et_dates == current_date)
intraday_bars = int(intraday_mask.sum())
current_time = now.astimezone(ET).time() if now else et_index[-1].time()
```

#### 3.2.3 Cold-Start Suppression & `warmup_pending` Tagging
Before 09:45:00 AM ET (`current_time < dtime(9, 45)`):
1. If `intraday_bars < 15`:
   - All candidate entry signals (`ENTER_LONG`, `ENTER_SHORT`) are suppressed.
   - Attach tag `sig.warmup_pending = True`.
   - Log rejection reason: `"warmup_pending: fewer than 15 intraday bars before 09:45 ET"`.
   - Record in `shadow_ledger`:
     ```python
     shadow_ledger.record(
         symbol=symbol,
         action=str(sig.action.value),
         strategy=sig.strategy,
         gate="warmup_pending",
         reason=f"warmup_pending: intraday_bars={intraday_bars} < 15 before 09:45 ET",
         price=sig.price,
         ...
     )
     ```
2. **Critical Exemption**: Protective exits (`EXIT`, `SCALE_OUT_HALF`, trailing stop ratchets) are **never** suppressed by the warmup guard.

---

## 4. Inventory of Existing Tests

| Area | Test File | Test Count / Focus |
| :--- | :--- | :--- |
| **Regime Matrix** | `backend/tests/test_regime_matrix.py` | 6 tests: `MARKET_STRESS`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `TRENDING_BULLISH`, `TRENDING_BEARISH`, `SIDEWAYS_RANGE` |
| **Market Risk Regime** | `backend/tests/test_047_market_risk_regime.py` | 9 tests: multi-signal voting, SPY/QQQ divergence, breadth, fail-closed UNKNOWN |
| **PIS Regime Classifier** | `backend/tests/test_pis_regime_classifier.py` | 5 tests: PIS micro-regime classification |
| **Recent Performance Governor** | `backend/tests/test_recent_performance_governor.py` | 5 tests: loser trim, never amplify, thin sample bypass, cache TTL, DB failure |
| **Portfolio Risk & PDT** | `backend/tests/test_risk.py`, `test_portfolio.py`, `test_directional_risk_geometry.py` | ~40 tests: sizing constraints, PDT 5-day window, loss streak cooldowns |
| **Market Scanner** | `backend/tests/test_scanner.py`, `test_049_scanner_cache.py`, `test_300_scanner_integration_forensics.py`, `test_issue211_scanner_parity.py`, `test_scanner_discovery_*.py` | ~35 tests: 3-stage pipeline, concurrency, sector diversity cap, discovery inputs |
| **Strategies** | `backend/tests/test_strategies.py`, `test_bb_squeeze_ml_bypass.py`, `test_exit_lifecycle_strategy_ownership_20260727.py` | ~25 tests: ORB breakout, inside range, momentum cross down, RSI2 dip buy |
| **Signal Pipeline** | `backend/tests/test_pipeline_shadow_isolation.py`, `test_candidate_id_ordering.py` | ~15 tests: champion/challenger shadow isolation, candidate ID stability |

### Test Gaps to be Covered by Implementer:
1. Unit tests for `RegimeMatrix` integration directly inside `SignalPipeline.process()`:
   - Verifying `allow_long=False` blocks longs in `TRENDING_BEARISH`.
   - Verifying `allow_short=False` blocks shorts in `TRENDING_BULLISH` and `LOW_VOLATILITY`.
   - Verifying `risk_scalar` properly scales `sig.risk_multiplier`.
2. Unit tests for opening cold-start bar starvation protection:
   - Verifying candidate entry signals at 09:35 AM ET with 5 intraday bars are suppressed with `warmup_pending`.
   - Verifying signals at 09:46 AM ET or with $\ge 15$ intraday bars pass through to normal evaluation.
   - Verifying `EXIT` signals at 09:35 AM ET are unaffected.
3. Unit tests for Sector ETF breakdown detection in `MarketScanner`:
   - Verifying equities in breaking down sectors receive technical score penalties or are suppressed from Stage 2 promotion.
4. Unit tests for dynamic strategy risk allocation:
   - Verifying Momentum stays capped at 0.50 risk when rolling expectancy $\le 0.0$.
   - Verifying Momentum scales up when rolling expectancy $> 0.0$.

---

## 5. Architectural Impact & Risk Assessment

1. **System Stability**: High positive impact. Integrating `RegimeMatrix` eliminates counter-trend trading and aligns strategy execution with broad macro risk.
2. **Cold-Start Protection**: High positive impact. Eliminates erratic entries during the first 15 minutes of trading when technical indicators lack statistical stabilization.
3. **ORB Core Protection**: Zero regression risk on ORB. Baseline entry logic remains completely untouched, preserving all tested and verified ORB behaviors.
4. **Backward Compatibility**: Fully preserved. Existing interfaces in `StrategyContext`, `SignalPipeline`, and `engine_evaluation.py` will accept optional parameters with sensible defaults.
