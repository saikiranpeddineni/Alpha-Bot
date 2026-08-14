import subprocess
import time
import signal
import os

p = subprocess.Popen(['pytest', 'backend/tests/test_backtest.py::test_backtest_runs_and_reports', '-s'], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
time.sleep(15)
os.kill(p.pid, signal.CTRL_C_EVENT)
p.wait()
