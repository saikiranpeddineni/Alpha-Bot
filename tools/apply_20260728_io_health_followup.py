"""Follow-up root-cause hardening for 2026-07-28 runtime issues.

Apply after apply_20260728_root_cause_safety_fix.py.
Changes only engine heartbeat severity handling and Alpaca data-provider I/O budgets.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend/app/core/engine.py"
DATA = ROOT / "backend/app/data/alpaca_data.py"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    src = path.read_text(encoding="utf-8")
    if new in src:
        print(f"already applied: {label}")
        return
    if old not in src:
        raise RuntimeError(f"anchor not found for {label}: {path}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    print(f"applied: {label}")


# Heartbeat severity is monotonic within a heartbeat. A lower-severity monitor
# must never overwrite SAFE_MODE/HALTED established by an earlier monitor.
replace_once(
    ENGINE,
    '            self.allow_new_entries = False\n            self.system_health = "DEGRADED"\n            applog.warning("engine.heartbeat", f"Broker connection lost/unknown: {exc}")',
    '            self.allow_new_entries = False\n            if self.system_health not in ("SAFE_MODE", "HALTED"):\n                self.system_health = "DEGRADED"\n            applog.warning("engine.heartbeat", f"Broker connection lost/unknown: {exc}")',
    "broker health cannot downgrade SAFE_MODE",
)
replace_once(
    ENGINE,
    '            diagnostics["db_connected"] = False\n            self.system_health = "DEGRADED"\n            applog.error("engine.heartbeat", f"Database connection health check failed: {exc}")',
    '            diagnostics["db_connected"] = False\n            self.allow_new_entries = False\n            if self.system_health not in ("SAFE_MODE", "HALTED"):\n                self.system_health = "DEGRADED"\n            applog.error("engine.heartbeat", f"Database connection health check failed: {exc}")',
    "database health cannot downgrade SAFE_MODE and fails closed",
)

# The Alpaca trading adapter already has a bounded _call(), but market-data calls
# used raw asyncio.to_thread(), so an SDK/HTTP stall could occupy event-monitor
# workers indefinitely. Bound the coroutine-facing wait at the provider boundary.
replace_once(
    DATA,
    "_SNAPSHOT_SUPPRESS_AFTER = 2  # downgrade to DEBUG after this many consecutive failures\n",
    "_SNAPSHOT_SUPPRESS_AFTER = 2  # downgrade to DEBUG after this many consecutive failures\n"
    "_ALPACA_DATA_IO_TIMEOUT_SECONDS = 10.0\n",
    "Alpaca data I/O timeout constant",
)
replace_once(
    DATA,
    "                df = await asyncio.to_thread(self._fetch_bars, symbol.upper(), timeframe, start, end)",
    "                df = await asyncio.wait_for(\n"
    "                    asyncio.to_thread(self._fetch_bars, symbol.upper(), timeframe, start, end),\n"
    "                    timeout=_ALPACA_DATA_IO_TIMEOUT_SECONDS,\n"
    "                )",
    "bounded Alpaca bar fetch",
)
replace_once(
    DATA,
    "                screener_symbols = await asyncio.to_thread(fetch_screener)",
    "                screener_symbols = await asyncio.wait_for(\n"
    "                    asyncio.to_thread(fetch_screener),\n"
    "                    timeout=_ALPACA_DATA_IO_TIMEOUT_SECONDS,\n"
    "                )",
    "bounded Alpaca screener fetch",
)
replace_once(
    DATA,
    "            return await asyncio.to_thread(fetch)\n        except Exception as exc:  # noqa: BLE001\n            applog.error(\"data.alpaca\", f\"asset discovery failed: {exc}\")",
    "            return await asyncio.wait_for(\n"
    "                asyncio.to_thread(fetch),\n"
    "                timeout=_ALPACA_DATA_IO_TIMEOUT_SECONDS,\n"
    "            )\n"
    "        except Exception as exc:  # noqa: BLE001\n"
    "            applog.error(\"data.alpaca\", f\"asset discovery failed: {exc}\")",
    "bounded Alpaca asset discovery",
)
replace_once(
    DATA,
    "            return await asyncio.to_thread(fetch)\n        except Exception as exc:  # noqa: BLE001\n            applog.error(\"data.alpaca\", f\"snapshot batch failed: {exc}\")",
    "            return await asyncio.wait_for(\n"
    "                asyncio.to_thread(fetch),\n"
    "                timeout=_ALPACA_DATA_IO_TIMEOUT_SECONDS,\n"
    "            )\n"
    "        except Exception as exc:  # noqa: BLE001\n"
    "            applog.error(\"data.alpaca\", f\"snapshot batch failed: {exc}\")",
    "bounded Alpaca snapshot fetch",
)

for path in (ENGINE, DATA):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")

print("I/O and heartbeat-severity follow-up applied; syntax compile passed.")
