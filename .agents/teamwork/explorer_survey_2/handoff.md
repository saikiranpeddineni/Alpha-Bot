# Handoff Report: R2 & R3 Architecture Survey

**Agent**: `explorer_survey_2` (Strategy Regime Explorer)  
**Parent Conversation ID**: `0f504116-8059-460c-a369-364fad59fafa`  
**Date**: 2026-11-11  
**Working Directory**: `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\explorer_survey_2`  
**Deliverable Document**: `c:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.agents\teamwork\explorer_survey_2\survey_r2_r3.md`  

---

## 1. Observation

1. **Strategy Risk Allocation & Performance Tracking**:
   - `backend/app/portfolio/performance.py:30–104`: `StrategyStats` computes static `win_rate`, `profit_factor`, `avg_r`, `expectancy` ($ expected PnL per trade), and `rolling_profit_factor` (over last 20 trades). It does **not** calculate rolling expectancy ($ or R-multiple) or rolling Sharpe ratio.
   - `backend/app/portfolio/recent_performance.py:30–148`: `RecentPerformanceGovernor` reads closed trades from DB and shrinks R-multiples (`(n * mean_r) / (n + k)`). It is strictly **tighten-only** (`multiplier <= 1.0`) and does nothing if `shrunk >= 0`.
   - `backend/app/config.py:258–260`: `RiskConfig.strategy_risk_multipliers` statically defines `{'momentum': 0.50}`.
   - `backend/app/risk/manager.py:1305–1317`: `RiskManager.evaluate_entry` applies `strat_risk_multipliers.get(strategy.lower(), 1.0)`, multiplying `risk_mult *= 0.50` statically for momentum.
   - `backend/app/core/engine.py:2463–2472`: `TradingEngine` calls `self._perf_governor.factor_for(sig.symbol, sig.strategy, now)` and scales `sig.risk_multiplier *= gov_factor`.

2. **Sector ETF Health Checks & Breakdown Detection**:
   - `backend/app/core/scanner.py:97–100`: `MARKET_UNIVERSE["sector_etfs"]` tracks 17 ETFs (`SPY`, `QQQ`, `XLK`, `XLF`, `XLE`, `XLV`, `XLI`, `XLY`, `XLP`, `XLU`, `XLB`, `XLRE`, `GLD`, `SLV`, `USO`, `TLT`, `HYG`).
   - `backend/app/strategies/sector_map.py:21–141`: Defines `SECTOR_ETF_MAP`, `TICKER_SECTOR_ETF` (mapping 100+ stocks to representative sector ETFs like `NVDA -> SOXX`, `AAPL -> XLK`, `JPM -> XLF`), and `get_sector_etf(symbol)`.
   - `backend/app/strategies/gap_and_go.py:189–220`: Implements sector divergence check against `getattr(ctx, "sector_bars", None)`. However, `ctx.sector_bars` is currently unpopulated by `TradingEngineEvaluationMixin` or `engine.py`.

3. **ORB Core Strategy Protection**:
   - `backend/app/strategies/orb.py:1–1303`: Core parameters in `default_params` (lines 187–285): `range_minutes: 15`, `start_time: "09:45"`, `end_time: "12:00"`, `breakout_buffer_bps: 5`, `reward_risk: 2.5`, `chop_reward_risk: 0.75`, `trending_reward_risk: 3.5`, `use_or_midpoint_stop: False`.
   - Range calculation: Lines 808–810: `or_high = today_df["high"].iloc[:15].max()`, `or_low = today_df["low"].iloc[:15].min()`, gated by `n_today > 15`.
   - Structural stops: Lines 1045, 1211: anchored to `or_low` (long) and `or_high` (short), with `1.5 * atr` safety stop and 0.45% minimum cushion.
   - Long breakout rule (lines 990–1017) and short breakdown rule (lines 1157–1184).

4. **Unified 6-Regime `RegimeMatrix`**:
   - `backend/app/core/regime_matrix.py:1–111`: Defines `UnifiedRegimeLabel` (`MARKET_STRESS`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `TRENDING_BULLISH`, `TRENDING_BEARISH`, `SIDEWAYS_RANGE`) and `RegimePosture(regime_label, allow_long, allow_short, risk_scalar, confidence, rationale)`.
   - `RegimeMatrix.evaluate(macro_state, symbol_regime, atr_percentile)` is fully implemented.
   - Currently, `RegimeMatrix` is only referenced in `backend/tests/test_regime_matrix.py` (line 4); it is **not wired** into `SignalPipeline.process()` or `engine_evaluation.py`.

5. **Cold-Start Bar Starvation**:
   - `backend/app/core/engine_evaluation.py:227`: Checks `if df.empty or len(df) < 5: return []`.
   - `backend/app/strategies/pipeline.py:317`: Checks `if df is not None and len(df) >= 5:`.
   - In live/paper runs, `DataManager.frame(symbol, "1Min", limit=600)` returns up to 600 prior-session bars. Thus, at 09:31 AM ET, `len(df)` is 601, passing the $< 5$ check despite having only 1 completed intraday bar.
   - Strategies evaluate against immature opening bar price action before 09:45 AM ET without 15 stabilized intraday bars.

6. **Existing Tests**:
   - 123 test files exist in `backend/tests/`. Key tests covering R2/R3 areas include `test_regime_matrix.py`, `test_047_market_risk_regime.py`, `test_recent_performance_governor.py`, `test_scanner.py`, `test_strategies.py`, `test_risk.py`, and `test_pipeline_shadow_isolation.py`.

---

## 2. Logic Chain

1. **Dynamic Risk Scaling (Step 1 -> Conclusion)**:
   - Observation 1 shows `StrategyStats` tracks closed trades and profit factors but lacks rolling expectancy and Sharpe ratio.
   - Observation 1 shows `RiskConfig.strategy_risk_multipliers` statically clamps momentum at 0.50.
   - Therefore, extending `StrategyStats` with `_recent_r` (rolling 20 trade R-multiples) enables computing `rolling_expectancy_r` and `rolling_sharpe`.
   - Hooking this metric into `RiskManager.evaluate_entry` allows dynamic scaling while keeping Momentum hard-capped at 0.50 until `rolling_expectancy_r > 0`.

2. **Sector ETF Health Checks (Step 2 -> Conclusion)**:
   - Observation 2 shows `MARKET_UNIVERSE["sector_etfs"]` and `sector_map.py` have complete ticker mappings, but `MarketScanner` does not evaluate sector ETF health during candidate selection, and `ctx.sector_bars` is unpopulated.
   - Therefore, computing intraday return and trend slope for sector ETFs in `MarketScanner` enables penalizing/suppressing equities whose sector ETF is breaking down ($< -1.0\%$ return and EMA slope $< 0$).

3. **ORB Core Protection (Step 3 -> Conclusion)**:
   - Observation 3 shows `backend/app/strategies/orb.py` contains fully hardened entry rules, range timing (09:45–12:00 ET), and 3-tier target geometry.
   - To guarantee 0 regressions, all new R2/R3 constraints must envelope ORB externally (via `engine_evaluation.py`, `SignalPipeline`, or `RiskManager`) and never edit `orb.py`.

4. **RegimeMatrix Integration (Step 4 -> Conclusion)**:
   - Observation 4 shows `RegimeMatrix` is already complete with clear contracts for `allow_long`, `allow_short`, and `risk_scalar`, but sits uncalled.
   - Wiring it into `engine_evaluation.py:385` (evaluating posture from `macro_state`, `symbol_regime`, `vol_percentile`) and into `SignalPipeline.process()` ensures directional trades and risk scaling strictly obey the 6-regime matrix.

5. **Cold-Start Protection (Step 5 -> Conclusion)**:
   - Observation 5 shows `len(df)` evaluates total history rather than today's intraday bars, allowing strategies to fire on premature 1-minute bars at 09:31 ET.
   - Counting intraday bars `intraday_bars = (df.index.tz_convert(ET).date == today).sum()` and suppressing entry candidates when `current_time < 09:45` and `intraday_bars < 15` with tag `warmup_pending` eliminates cold-start starvation while exempting protective exits.

---

## 3. Caveats

1. **Replay Mode vs Live Data**: In historical backtest replays using `ReplayClock`, intraday bars advance synchronously. Warmup bar count checks must reference `ctx.now` / `clock.now_utc()` converted to America/New_York date, not system wall clock.
2. **PIS Exit Exemption**: Any cold-start suppression or regime veto must strictly exempt `SignalAction.EXIT` and `SignalAction.SCALE_OUT_HALF`. The platform must never block position risk reduction or stop loss protection.
3. **No Code Modification Executed**: As an explorer subagent, this turn was 100% read-only investigation. No source code in `backend/` was altered.

---

## 4. Conclusion

The architecture for R2 and R3 has been completely mapped with exact file paths, line numbers, and implementation blueprints:
1. **R2 Dynamic Scaling**: Extend `StrategyStats` in `backend/app/portfolio/performance.py` with `_recent_r` to compute rolling expectancy and Sharpe ratio. Enforce the 50% cap on Momentum in `RiskManager.evaluate_entry` until rolling expectancy is positive.
2. **R2 Sector Breakdown**: Leverage `backend/app/strategies/sector_map.py` in `MarketScanner` (`backend/app/core/scanner.py`) to detect sector ETF breakdown ($< -1.0\%$ return + negative EMA slope) and penalize/suppress candidate breakout setups.
3. **R2 ORB Core Protection**: Maintain `backend/app/strategies/orb.py` completely untouched. Enforce all protections externally.
4. **R3 RegimeMatrix**: Wire `RegimeMatrix.evaluate()` into `engine_evaluation.py` (after `detect_regime`) and pass `posture` into `SignalPipeline.process()`, enforcing `allow_long`, `allow_short`, and `risk_scalar`.
5. **R3 Cold-Start Guard**: Track `intraday_bars = (et_dates == session_date).sum()` in `engine_evaluation.py` and `SignalPipeline.process()`. Suppress entry signals before 09:45 AM ET when `intraday_bars < 15`, tagging with `warmup_pending`.

---

## 5. Verification Method

1. **Verify Baseline Test Suite**:
   ```powershell
   pytest backend/tests/test_regime_matrix.py backend/tests/test_recent_performance_governor.py backend/tests/test_strategies.py backend/tests/test_scanner.py backend/tests/test_047_market_risk_regime.py
   ```
2. **Verify Code Locations**:
   - `view_file` on `backend/app/core/regime_matrix.py` (lines 35–111) confirms `RegimeMatrix` implementation.
   - `view_file` on `backend/app/strategies/orb.py` (lines 187–285) confirms ORB parameters and geometry.
   - `view_file` on `backend/app/core/engine_evaluation.py` (lines 202–232, 385–395, 1376–1385) confirms `detect_regime`, warmup frame length checks, and `pipeline.process` invocation.
   - `view_file` on `backend/app/strategies/pipeline.py` (lines 122–156, 314–326) confirms signal pre-filter and warmup logic.
3. **Invalidation Conditions**:
   - If `RegimeMatrix` contract changes `UnifiedRegimeLabel` values without updating tests.
   - If ORB code in `backend/app/strategies/orb.py` is edited instead of enveloped externally.
