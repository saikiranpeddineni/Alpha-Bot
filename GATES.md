# Gates: Monte Carlo Borderline Rescue Calibration & Pipeline Harmonization

OWNS: backend/app/config.py, backend/app/strategies/pipeline.py, backend/tests/test_monte_carlo_gate.py

Scope: Harmonize RiskConfig defaults with Settings, calibrate rescue thresholds for real-world ML confidence, wire rescue into pipeline.py, and verify with tests.

- [x] G1: RiskConfig and Settings defaults match and enable calibrated Monte Carlo borderline rescue
  CHECK: .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'backend'); from app.config import RiskConfig, Settings; rc = RiskConfig(); s = Settings(); assert rc.enable_monte_carlo_borderline_rescue is True; assert s.enable_monte_carlo_borderline_rescue is True; assert rc.monte_carlo_rescue_min_ml_probability == 0.52; assert rc.monte_carlo_rescue_min_confidence == 0.60; assert rc.monte_carlo_rescue_min_target_prob == 0.50; print('G1_CHECK_PASSED')"
  EXPECT: G1_CHECK_PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=361d5063b261b748301759ce23a45f843414e3c34effb3b5938659c1ff64df8d; exit=0; EXPECT=matched; output-sha256=f70b3f7358991a65a38e746ca369b11796d6cf410f5a19ff1472dabec03813e6; output-bytes=17; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G2: Pipeline instantiates MonteCarloGate with rescue configuration and honors rescue outcomes
  CHECK: .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'backend'); import inspect; from app.strategies.pipeline import SignalPipeline; src = inspect.getsource(SignalPipeline.process); assert 'rescue_enabled' in src; print('G2_CHECK_PASSED')"
  EXPECT: G2_CHECK_PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=94c5d91d58b3357501040940fb395c758ad68652d201aabd92dbf96b94acdf9f; exit=0; EXPECT=matched; output-sha256=837efe9071e6cd4f114ea777a17453c8d305a162bee10034cf929829773b9e34; output-bytes=17; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G3: Monte Carlo gate test suite passes with calibrated borderline rescue validations
  CHECK: .venv\Scripts\python.exe -m pytest backend/tests/test_monte_carlo_gate.py
  EXPECT: 7 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=689ef02e7ee6126ab4afff060409212d7d13bc8f2edfab54cfca00b9b2424dee; exit=0; EXPECT=matched; output-sha256=f81f1b69d8f61c1e312127d742b52c84d9808bd31705255223936b9a1116e8f0; output-bytes=566; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G4: Full strategy and risk test suites pass without regression
  CHECK: .venv\Scripts\python.exe -m pytest backend/tests/test_monte_carlo_gate.py backend/tests/test_risk.py backend/tests/test_strategies.py
  EXPECT: ========================
  EVIDENCE: automatic-evidence=v1; definition-sha256=453c32d0097ff7bb996e5b17ab6e8da83ebed8e97078db9d51a762b82a2ca8a1; exit=0; EXPECT=matched; output-sha256=3b2961a01b3b8a9fb851b2ee267eaa329315855dc9df6c281554877aadf6ccd3; output-bytes=729; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries
