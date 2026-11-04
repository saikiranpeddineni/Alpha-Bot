# Gates: Risk Hardening, Anti-Counter-Trend Firewall & Leveraged ETF Safeguards

OWNS: backend/app/strategies/gap_and_go.py, backend/app/strategies/bb_squeeze.py, backend/app/strategies/orb.py, backend/app/strategies/pipeline.py, backend/app/risk/manager.py, backend/tests/test_opportunity_capitalization_fixes.py

Scope: Implement Gap-and-Go and BB Squeeze anti-counter-trend firewalls, universal sub-$5 penny stock gating, leveraged ETF sizing scaler, and inverse ETF market alignment.

- [x] G1: Gap-and-Go rejects counter-trend entries (shorts on gainers/above open, longs on losers/below open) and sub-$5 penny stocks
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_opportunity_capitalization_fixes.py -k test_gap_and_go_rejects_counter_trend_and_penny -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=b045feccc6fde6280e1050289e4f48fa0db0a09c62f8dee43c3b02abe3cdf1b1; exit=0; EXPECT=matched; output-sha256=4dcd543b559d53463bd7aa7c0cd3bd3ea757bfb2966d06c130420cfbec63439a; output-bytes=746; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G2: BB Squeeze rejects long breakouts below VWAP/session-open (falling knives) and sub-$5 penny stocks
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_opportunity_capitalization_fixes.py -k test_bb_squeeze_rejects_sub_vwap_long_and_penny -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=c8afc89c30d1163c160f19deffb23547c7530e091fab82f05edc15b207ee141e; exit=0; EXPECT=matched; output-sha256=cd72ba5ea09988ff813f21e968b953a1b9402917c4538b707d3f85a8d4ff850e; output-bytes=746; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G3: SignalPipeline enforces universal $5.00 penny stock gate and rejects inverse ETF long entries during bullish market regimes
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_opportunity_capitalization_fixes.py -k "test_pipeline_rejects_sub_5_penny_stock or test_pipeline_rejects_inverse_etf_in_trend_up" -v
  EXPECT: 2 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=029f96ce4d0c8e1361e8de91868bb096be474306b839fa1acfe4045965db9f80; exit=0; EXPECT=matched; output-sha256=b9c43550e5b32e66933559c31f2d75dd988cbc940e3443108c42e8cfbd3235b7; output-bytes=855; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G4: RiskManager scales notional allocation and dollar risk for 2x and 3x leveraged ETFs to prevent outsized losses
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_opportunity_capitalization_fixes.py -k test_leveraged_etf_sizing_scaler -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=108acfdba7c557adc810bbd523f7780d7bb3226e3e8c7c6e0c857ccca374c6c4; exit=0; EXPECT=matched; output-sha256=ea28b7c0aae890eb831f8460be95ace43badf0d0bbff98b85949661292be5ccd; output-bytes=731; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G5: Full opportunity capitalization test suite passes without regressions
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_opportunity_capitalization_fixes.py -v
  EXPECT: 10 passed in
  EVIDENCE: automatic-evidence=v1; definition-sha256=d020328c6d9d61e4352214294890587ae241563df935bf132809a21b1588a90d; exit=0; EXPECT=matched; output-sha256=1309146aca0ed204462ab74c112adc159608f143b6d9dd624dfcbbf3d15f6b81; output-bytes=1746; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries
