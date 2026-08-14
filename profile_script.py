import cProfile
import pytest
import pstats

print("Running test with cProfile...")
cProfile.run("pytest.main(['backend/tests/test_backtest.py::test_backtest_runs_and_reports'])", "prof.stats")
print("Profiling complete. Stats:")
p = pstats.Stats('prof.stats')
p.sort_stats('cumtime').print_stats(30)
