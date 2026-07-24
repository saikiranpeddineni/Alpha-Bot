#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python -m pytest -q \
  tests/test_issue_20_same_cycle_reservations.py \
  tests/test_issue_22_restart_kill_switch.py \
  tests/test_issue_65_pdt_restart_durability.py
