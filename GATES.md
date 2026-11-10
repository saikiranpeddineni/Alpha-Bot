# Gates: Trading Restrictions & Panic Exit Remediation (Alphadesk_002)

OWNS: backend/app/brokers/paper.py, backend/app/pis/models.py, backend/app/strategies/pipeline.py, backend/app/config.py, backend/app/ai/monte_carlo_gate.py, backend/app/risk/manager.py, backend/app/core/engine_evaluation.py, backend/app/opportunities/engine.py, backend/app/strategies/orb.py, backend/tests/test_restrictions_and_exits_remediation.py

Scope: Calibrate paper broker trailing stops to prevent premature stop-outs, de-choke PIS protection floors, relax the entry extreme proximity guard to admit genuine momentum breakouts, lower Monte Carlo hurdle to positive edge, calibrate scratch neutrality to prevent ticker lockout, fix opportunity engine scale-out handling, expand ORB runner targets to 3.5R, restore full default watchlist, and verify regression safety.

- [x] G1: Paper broker advance trail arms breakeven at 1.2R and preserves breathing room
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_paper_advance_trail_breathing_room -v
  EXPECT: 1 passed

- [x] G2: PIS config parameters provide breathing room and prevent micro-scratch shakeouts
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_pis_config_calibrations -v
  EXPECT: 1 passed

- [x] G3: Pipeline entry extreme guard admits momentum breakouts within 3 ATR of session high
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_pipeline_extreme_guard_calibration -v
  EXPECT: 1 passed

- [x] G4: Monte Carlo gate approves trades with positive edge (Expected R >= 0.00)
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_monte_carlo_gate_calibration -v
  EXPECT: 1 passed

- [x] G5: Risk manager treats small scratches up to 0.30% neutrally without locking out symbols
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_risk_scratch_neutrality_calibration -v
  EXPECT: 1 passed

- [x] G6: OpportunityEngine passes scale-out and exit actions without false cost rejections
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_opportunity_engine_action_handling -v
  EXPECT: 1 passed

- [x] G7: ORB strategy provides expanded 3.5R reward-risk in trending conditions
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_orb_trending_target_expansion -v
  EXPECT: 1 passed

- [x] G8: Full regression suite passes cleanly
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_nov04_forensic_fixes.py backend/tests/test_entry_extreme_guard.py backend/tests/test_monte_carlo_gate.py backend/tests/test_risk.py backend/tests/test_restrictions_and_exits_remediation.py -v
  EXPECT: 48 passed

- [x] G9: Git branch verified as Alphadesk_002
  CHECK: git branch --show-current
  EXPECT: Alphadesk_002
