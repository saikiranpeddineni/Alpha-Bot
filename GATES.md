# Gates: Market Opportunity Capitalization, Anti-Counter-Trend Firewall & Risk Hardening

OWNS: backend/app/risk/market_regime.py, backend/app/strategies/orb.py, backend/app/ai/monte_carlo_gate.py, backend/app/strategies/momentum.py, backend/tests/test_opportunity_capitalization_fixes.py

Scope: Fix false PANIC market regime, prevent ORB counter-trend shorting on market leaders, calibrate Monte Carlo adaptive win-rate hurdle for high-RR setups, and add minimum price filter to momentum strategy.

- [x] G1: Market Risk Aggregator intraday uptrend fallback prevents false PANIC on bullish/neutral SPY/QQQ
  CHECK: .\.venv\Scripts\pytest.exe backend/tests/test_opportunity_capitalization_fixes.py -k test_market_regime_intraday_uptrend_fallback -v
  EXPECT: 1 passed in
  EVIDENCE: automatic-evidence=v1; exit=0; EXPECT=matched; 1 passed in 0.23s; SPY/QQQ fallback activates VWAP and session open when <20 hourly bars exist.

- [x] G2: ORB short entry rejected when equity is trading above open, VWAP, or positive on day
  CHECK: .\.venv\Scripts\pytest.exe backend/tests/test_opportunity_capitalization_fixes.py -k test_orb_rejects_short_on_positive_trend -v
  EXPECT: 1 passed in
  EVIDENCE: automatic-evidence=v1; exit=0; EXPECT=matched; 1 passed in 0.23s; ORB rejects short breakdowns on equities trading above session open or flagged as mover gainers.

- [x] G3: Monte Carlo gate approves positive-edge high-RR setups without artificial 35% win rate veto
  CHECK: .\.venv\Scripts\pytest.exe backend/tests/test_opportunity_capitalization_fixes.py -k test_monte_carlo_adaptive_hurdle_high_rr -v
  EXPECT: 1 passed in
  EVIDENCE: automatic-evidence=v1; exit=0; EXPECT=matched; 1 passed in 0.23s; setups with r_target >= 2.0 (breakeven <= 33%) and positive expectancy pass adaptive hurdle.

- [x] G4: Momentum strategy rejects entry signals for low-priced penny stocks under $5.00
  CHECK: .\.venv\Scripts\pytest.exe backend/tests/test_opportunity_capitalization_fixes.py -k test_momentum_rejects_sub_5_penny_stock -v
  EXPECT: 1 passed in
  EVIDENCE: automatic-evidence=v1; exit=0; EXPECT=matched; 1 passed in 0.23s; stocks priced < $5.00 (e.g. INO at $2.02) are cleanly rejected at admission.

- [x] G5: Existing Monte Carlo, risk, and strategy test suites continue to pass without regression
  CHECK: .\.venv\Scripts\pytest.exe backend/tests/test_monte_carlo_gate.py backend/tests/test_risk.py backend/tests/test_strategies.py -v
  EXPECT: passed in
  EVIDENCE: automatic-evidence=v1; exit=0; EXPECT=matched; 47 passed in 1.38s with zero regressions.
