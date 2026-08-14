import time, threading, traceback, sys, os, pytest

def dump():
    time.sleep(10)
    with open('dump_output.txt', 'w') as f:
        traceback.print_stack(sys._current_frames()[threading.main_thread().ident], file=f)
    os._exit(1)

threading.Thread(target=dump).start()
pytest.main(['backend/tests/test_backtest.py::test_backtest_runs_and_reports'])
