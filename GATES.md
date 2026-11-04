# Gates: November 4, 2026 Forensic Audit & Root-Cause Remediation

OWNS: backend/app/brokers/alpaca.py, backend/app/core/engine.py, backend/app/strategies/gap_and_go.py, backend/app/strategies/orb.py, backend/tests/test_nov04_forensic_fixes.py, artifacts/FORENSIC_ANALYSIS_2026_11_04.md

Scope: Audit all losses from 2026-11-04 session, complete forensic trade-by-trade analysis, fix bracket child fill polling, scale gap-and-go stops by ATR, buffer ORB VWAP exits, and resolve ghost lot deadlocks.

- [x] G1: Forensic analysis report generated and verified for 2026-11-04 session
  CHECK: .venv\Scripts\python -c "import os; assert os.path.exists('artifacts/FORENSIC_ANALYSIS_2026_11_04.md') and os.path.getsize('artifacts/FORENSIC_ANALYSIS_2026_11_04.md') > 10000; print('FORENSIC REPORT VERIFIED')"
  EXPECT: FORENSIC REPORT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=f6d6243ff8bedf388d265235c3c419b1fe58d991023e7444d127956faab91786; exit=0; EXPECT=matched; output-sha256=e39a3cc34e7de6ced9d9ef0c8e56f3c8d058edd0dee65568e33b35de7b91c873; output-bytes=26; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G2: Alpaca broker unrolls nested bracket and OTO child legs in poll_fills
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_nov04_forensic_fixes.py -k test_alpaca_nested_child_fills_unrolled -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=1fde475c3fe00e065fb529eb0cc0e530d4dc9a29deeaa5a28a286a92ae2b4ed1; exit=0; EXPECT=matched; output-sha256=7f38294faa7f11a997d8567df6b55295ae146d973d4fc1e2970113d3b06957a4; output-bytes=725; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G3: Gap-and-Go strategy stop-loss is dynamically scaled by ATR
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_nov04_forensic_fixes.py -k test_gap_and_go_stop_loss_scaled_by_atr -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=16febf9a63d41dd821c6c55574b997b7da63b87f40429f34ce4c965b0af6435e; exit=0; EXPECT=matched; output-sha256=0af8c39981b78290557b422b0c6f225829a2e274520790ce75318b932e9e5518; output-bytes=725; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G4: ORB strategy requires volatility buffer and 3-bar confirmation for VWAP fade exits
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_nov04_forensic_fixes.py -k test_orb_vwap_fade_volatility_buffer -v
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=7d48620a7b383468a9421e3aa4955cc9aa15eacee6e2043f9aa6522a43a492ac; exit=0; EXPECT=matched; output-sha256=50d2997443ae814f0fe1a39ed1901256648f6ba32db578020e8559d802f6cfba; output-bytes=722; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G5: Full forensic fixes test suite passes cleanly
  CHECK: .venv\Scripts\python -m pytest backend/tests/test_nov04_forensic_fixes.py -v
  EXPECT: 3 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=724905add26a11c395fd351acfc9fc29d5856c0014646f77331b779f4cf80c29; exit=0; EXPECT=matched; output-sha256=e167372e1eee1ea2f6de1d548ed070bd1029a4cb918a0122d9a0aa53b7a16492; output-bytes=892; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries

- [x] G6: Git branch is Alphadesk_002
  CHECK: git branch --show-current
  EXPECT: Alphadesk_002
  EVIDENCE: automatic-evidence=v1; definition-sha256=8a8f3cfd03c7a36a941129578a96d8258e0312144e660ddeaa0d6cbececd8493; exit=0; EXPECT=matched; output-sha256=14a0ce25eae33ead12391f159b0d0d53e799c785e6bc49ec72fa16059c8baab9; output-bytes=14; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\peddi\Desktop\Bots\Alpha-Bot Master; path=33941c8ef8e1/57 entries
