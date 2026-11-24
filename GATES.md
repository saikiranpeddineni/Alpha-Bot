# Gates: Trading Restrictions & Panic Exit Remediation (Alphadesk_002)

OWNS: backend/app/brokers/paper.py, backend/app/pis/models.py, backend/app/pis/exit_planner.py, backend/app/core/engine.py, backend/app/strategies/pipeline.py, backend/app/config.py, backend/app/ai/monte_carlo_gate.py, backend/app/risk/manager.py, backend/app/core/engine_evaluation.py, backend/app/opportunities/engine.py, backend/app/strategies/orb.py, backend/tests/test_restrictions_and_exits_remediation.py, backend/tests/test_two_tier_protection.py

Scope: Calibrate paper broker trailing stops to prevent premature stop-outs, de-choke PIS protection floors, relax the entry extreme proximity guard to admit genuine momentum breakouts, lower Monte Carlo hurdle to positive edge, calibrate scratch neutrality to prevent ticker lockout, fix opportunity engine scale-out handling, expand ORB runner targets to 3.5R, restore full default watchlist, implement Two-Tier Protection Architecture (Tier 1 Soft Technical Breach -$300 with structural breathing room + Tier 2 Hard Catastrophe Circuit Breaker -$500), and verify regression safety.

- [x] G1: Paper broker advance trail arms breakeven at 1.2R and preserves breathing room
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_paper_advance_trail_breathing_room -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=0efdea476b05ab4ea4fa4eaa99957ce146e834d41eeaddc0bf5e85105d437e2a; exit=0; EXPECT=matched; output-sha256=af203a49e28b5cd48f19fb4f0c5b761b81fe4fa89e808e49fca94bf6a655765e; output-bytes=739; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G2: PIS config parameters provide breathing room and prevent micro-scratch shakeouts
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_pis_config_calibrations -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=8ca120aae5281bc923c9aa2a571abbe0a26bc03f87411e0b349e581be3d655ef; exit=0; EXPECT=matched; output-sha256=753a52d8c9e7a6e4750277eff31bd9af06398112766eb1765e576c73f89465ea; output-bytes=728; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G3: Pipeline entry extreme guard admits momentum breakouts within 3 ATR of session high
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_pipeline_extreme_guard_calibration -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=6fb3448c4a539ee0fd61a74da2069a8f4028f5eab40166f62729324a9ffe47cc; exit=0; EXPECT=matched; output-sha256=3614c0faa6c29abf2866e403099f8ff4b3abd022f0e6b9edb2ab4c87dd52c393; output-bytes=739; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G4: Monte Carlo gate approves trades with positive edge (Expected R >= 0.00)
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_monte_carlo_gate_calibration -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=0ca3b9ccdb762ff656679c3b3accb1f777045cd136669881199c8eda3ce0c07e; exit=0; EXPECT=matched; output-sha256=afadeacfd698c31d8b62405efb7ab2cae47c13fcb50107c65b6338c6e0acb94c; output-bytes=733; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G5: Risk manager treats small scratches up to 0.30% neutrally without locking out symbols
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_risk_scratch_neutrality_calibration -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=219f66ceedb3287e7f8d0ae4e5d54d6f57ddf9bbc7687fa429c0da47bdac3549; exit=0; EXPECT=matched; output-sha256=9fa3de46aa605d86e120d49f7d6901dfc73cac1ccd41bd6845a14e670de93294; output-bytes=740; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G6: OpportunityEngine passes scale-out and exit actions without false cost rejections
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_opportunity_engine_action_handling -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=2b434eac13a235bad66881054e9c8e375808b160e34b38794084ed5e7a693c74; exit=0; EXPECT=matched; output-sha256=8cf2a34b557f42446d7db44e39f28c156dbd8d7a24b1b5b047e9659dbf454f06; output-bytes=739; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G7: ORB strategy provides expanded 3.5R reward-risk in trending conditions
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py -k test_orb_trending_target_expansion -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=8fa88806a2db0281501273b14e1e49fa47e1de7c6b000644fbeb6559544a46c6; exit=0; EXPECT=matched; output-sha256=b3665f7a755d98973b803cefbda22a12205ac4081aeec179787ee6af03131cec; output-bytes=734; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [ ] G8: Full regression suite passes cleanly
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_nov04_forensic_fixes.py backend/tests/test_entry_extreme_guard.py backend/tests/test_monte_carlo_gate.py backend/tests/test_risk.py backend/tests/test_restrictions_and_exits_remediation.py -v
  EXPECT: 48 passed
  EVIDENCE: pending

- [x] G9: Git branch verified as Alphadesk_002
  CHECK: git branch --show-current
  EXPECT: Alphadesk_002
  EVIDENCE: automatic-evidence=v1; definition-sha256=8a8f3cfd03c7a36a941129578a96d8258e0312144e660ddeaa0d6cbececd8493; exit=0; EXPECT=matched; output-sha256=14a0ce25eae33ead12391f159b0d0d53e799c785e6bc49ec72fa16059c8baab9; output-bytes=14; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G10: Two-Tier protection unit tests pass (Tier 1 soft technical + Tier 2 catastrophe breaker + breathing room)
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_two_tier_protection.py -v
  EXPECT: 8 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=5413aa4a5da58cd024fc0ef9a771f76b16ec1dce228c591523fe9ab216d30453; exit=0; EXPECT=matched; output-sha256=ee9dc1096a5ede038a9566d34ea8b41abc85ce2c3849704ded68f15976d7254c; output-bytes=1388; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries

- [x] G11: Full PIS and restrictions regression suite passes
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_restrictions_and_exits_remediation.py backend/tests/test_two_tier_protection.py backend/tests/test_pis_early_invalidation.py backend/tests/test_pis_floor_tag_runner.py backend/tests/test_pis_noise_immunity.py -v
  EXPECT: 28 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=ee8aa3485146fb4e1806197af92c5518f5b77297810aa9e17fefd5f02bec7423; exit=0; EXPECT=matched; output-sha256=e49f8494c25496db0aa5e35d76acb3a951b56082ee926a3478ac40b6eda8d377; output-bytes=3518; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=9e8b76fe17e0/55 entries
