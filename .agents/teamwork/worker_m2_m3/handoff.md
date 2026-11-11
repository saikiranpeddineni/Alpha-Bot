# Handoff Report: R2 Strategy Dynamic Allocation & R3 Market Regime Unification

**Worker**: `worker_m2_m3` (Strategy, Dynamic Allocation, Regime & Cold-Start)  
**Parent Conversation ID**: `0f504116-8059-460c-a369-364fad59fafa`  
**Initiative**: AlphaBot / AlphaDesk Master Hardening  
**Active Branch**: `Alphadesk_002`  
**Date**: 2026-11-11  

---

## 1. Observation

1. **Strategy Risk Allocation & Performance Metrics**:
   - `backend/app/portfolio/performance.py:30–104`: `StrategyStats` tracked `wins`, `losses`, `total_pnl`, `total_r`, and `rolling_profit_factor`, but had no rolling expectancy in R or rolling Sharpe ratio.
   - `backend/app/risk/manager.py:1305–1317`: `RiskManager.evaluate_entry` applied static multiplier from `cfg.strategy_risk_multipliers.get(strategy.lower(), 1.0)`, without dynamically adjusting as strategy performance evolved.

2. **Sector ETF Health Checks**:
   - `backend/app/core/scanner.py:550–608`: `_stage2_technical_score` ranked symbols across 4 components (`rvol`, `gap_pct`, `atr_pct`, `mom3`) in isolation, allowing stocks in breaking-down sectors to claim top ranks.
   - `backend/app/core/engine_evaluation.py:1109–1117`: `sctx = StrategyContext(...)` did not populate `ctx.sector_bars`, leaving strategy sector divergence filters (e.g. `gap_and_go.py:193–217`) unsupplied.

3. **Unified 6-Regime `RegimeMatrix` Wiring**:
   - `backend/app/core/regime_matrix.py:35–111`: Standalone `RegimeMatrix.evaluate(macro_state, symbol_regime, atr_percentile)` evaluated 6 unified postures (`MARKET_STRESS`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `TRENDING_BULLISH`, `TRENDING_BEARISH`, `SIDEWAYS_RANGE`) but was uncalled in `SignalPipeline.process()` and `engine_evaluation.py`.

4. **Opening Cold-Start Bar Starvation**:
   - `backend/app/core/engine_evaluation.py:227` and `backend/app/strategies/pipeline.py:317`: Warmup guards checked `len(df) >= 5` on the total historical dataframe slice, which at 09:31 AM ET was pre-warmed with prior-session bars (~600 bars), evaluating unformed intraday indicators before 15 session bars were formed.

5. **ORB Core Protection Guarantee**:
   - `backend/app/strategies/orb.py:1–1303`: Opening Range Breakout entry rules, 15m range timing, and structural stop/target geometry remained 100% UNTOUCHED (`git diff backend/app/strategies/orb.py` produced 0 changes).

---

## 2. Logic Chain

1. **R2 Dynamic Scaling Implementation**:
   - From Observation 1, extended `StrategyStats` with `_recent_r` (deque maxlen=20) to compute `rolling_expectancy_r` and `rolling_sharpe`.
   - In `backend/app/risk/manager.py`, added `get_strategy_risk_multiplier(strategy, stats)`. For `momentum`, it strictly enforces the 50% cap (`0.50`) when `stats is None`, `trades < 5`, or `rolling_expectancy_r <= 0`. When `rolling_expectancy_r > 0`, it scales up dynamically toward `1.0`.
   - For other strategies, underperformance (`rolling_expectancy_r < 0`) scales down toward `0.50`, while high performance (`rolling_expectancy_r > 0.30` and `rolling_sharpe > 1.5`) permits up to `1.25x`.
   - In `evaluate_entry`, the dynamic multiplier is applied to `risk_mult`.

2. **R2 Sector ETF Health Checks Implementation**:
   - From Observation 2, added `assess_sector_etf_health(as_of)` in `MarketScanner` (`backend/app/core/scanner.py`), evaluating intraday return (`(close - session_open) / session_open * 100.0`) and trend slope for tracked sector ETFs. If return is $< -1.0\%$ with negative slope, `is_breaking_down = True`.
   - In `_stage2_technical_score`, equities whose sector ETF is breaking down receive a 50% technical score penalty and are tagged with `sector_breakdown = True`.
   - In `backend/app/core/engine_evaluation.py:1118`, populated `sctx.sector_bars = {sector_etf: sector_df}` so strategy-level divergence checks receive live ETF frames.

3. **R3 Unified 6-Regime `RegimeMatrix` Wiring Implementation**:
   - From Observation 3, wired `RegimeMatrix.evaluate()` into `SignalPipeline.process()` (`backend/app/strategies/pipeline.py`).
   - If `posture.regime_label == UnifiedRegimeLabel.MARKET_STRESS` or `posture.risk_scalar == 0.0`: freezes all entry signals.
   - If `sig.action == SignalAction.ENTER_LONG and not posture.allow_long`: suppresses long entries (e.g. in `TRENDING_BEARISH`).
   - If `sig.action == SignalAction.ENTER_SHORT and not posture.allow_short`: suppresses short entries (e.g. in `TRENDING_BULLISH`).
   - In `LOW_VOLATILITY`: suppresses mean-reversion strategies (`vwap_reversion`, `rsi2_reversion`), permitting breakout strategies (`orb`, `bb_squeeze`).
   - Scales risk: `sig.risk_multiplier = round(sig.risk_multiplier * posture.risk_scalar, 4)` and attaches `sig.regime_posture = posture` and `sig.unified_regime = posture.regime_label.value`.
   - In `backend/app/core/engine_evaluation.py:1419`, evaluated `_posture` from engine `last_safety_decision.market_risk_state`, `regime`, and `vol_percentile`, and passed `posture=_posture, now=now` to `pipeline.process`.

4. **R3 Opening Cold-Start Bar Starvation Guard Implementation**:
   - From Observation 4, calculated intraday bars for the active session date in America/New_York (`(et_idx.date == current_date).sum()`).
   - Before 09:45 AM ET, if `intraday_bars < 15`, suppresses candidate entry signals, sets `sig.warmup_pending = True`, rejects with reason `"warmup_pending: fewer than 15 intraday bars before 09:45 ET"`, and logs to `shadow_ledger` under gate `"warmup_pending"`.
   - Protective exits (`EXIT`, `SCALE_OUT_HALF`) are strictly exempt.

5. **Code Modification & Comment Preservation**:
   - All touched files strictly preserved the original code in-place using the structured comment header template (Timestamp, Original Intent, Reason for Change, Safe Rollback).

---

## 3. Caveats

- **Historical Replay Support**: In backtest replays where bar timestamps are past historical dates, `now` or the bar index timestamp is converted to `America/New_York` to resolve the intraday session date, ensuring deterministic behavior in live, paper, and backtest modes.
- **Protective Exit Invariance**: No cold-start guard or regime filter ever suppresses risk reduction or stop loss protection (`SignalAction.EXIT` and `SignalAction.SCALE_OUT_HALF` always pass).

---

## 4. Conclusion

All requirements for Milestone 2 (R2) and Milestone 3 (R3) have been fully implemented, tested, and verified:
1. `StrategyStats` rolling expectancy in R and rolling Sharpe over the last 20 trades implemented.
2. `RiskManager` dynamic strategy risk scaling with 50% Momentum cap until rolling expectancy is positive implemented.
3. `MarketScanner` sector ETF health checks with 50% technical score penalty for breaking down sectors implemented, and `ctx.sector_bars` populated in engine evaluation.
4. Unified 6-regime `RegimeMatrix` wired into `SignalPipeline.process()` and `engine_evaluation.py`.
5. Cold-start bar starvation protection (< 15 intraday bars before 09:45 AM ET) implemented with `warmup_pending` tagging and shadow ledger logging.
6. Core ORB logic in `backend/app/strategies/orb.py` verified 100% untouched.

---

## 5. Verification Method

Run targeted test suites using Python virtual environment:
```powershell
& "C:\Users\peddi\Desktop\Bots\Alpha-Bot Master\.venv\Scripts\python.exe" -m pytest backend/tests/test_regime_matrix.py backend/tests/test_regime_wiring.py backend/tests/test_cold_start_guard.py backend/tests/test_risk.py backend/tests/test_portfolio.py backend/tests/test_scanner.py backend/tests/test_recent_performance_governor.py backend/tests/test_strategies.py
```

### Verified Test Output:
```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\peddi\Desktop\Bots\Alpha-Bot Master
configfile: pytest.ini
plugins: anyio-4.14.2, asyncio-1.4.0
collected 72 items

backend\tests\test_regime_matrix.py ......                               [  8%]
backend\tests\test_regime_wiring.py ......                               [ 16%]
backend\tests\test_cold_start_guard.py ...                               [ 20%]
backend\tests\test_risk.py .....................                         [ 50%]
backend\tests\test_portfolio.py ............                             [ 66%]
backend\tests\test_scanner.py ....                                       [ 72%]
backend\tests\test_recent_performance_governor.py .....                  [ 79%]
backend\tests\test_strategies.py ...............                         [100%]

============================= 72 passed in 15.78s =============================
```

ORB Core Immutability Check:
```powershell
git diff backend/app/strategies/orb.py
# (Returns empty - 0 lines modified)
```
