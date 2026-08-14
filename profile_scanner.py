import cProfile
import pstats
import pytest

def main():
    profiler = cProfile.Profile()
    profiler.enable()
    pytest.main(["backend/tests/test_backtest.py::test_backtest_dynamic_scanner_runs"])
    profiler.disable()
    stats = pstats.Stats(profiler).sort_stats("cumtime")
    stats.print_stats(30)

if __name__ == "__main__":
    main()
