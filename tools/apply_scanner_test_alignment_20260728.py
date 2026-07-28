"""Align legacy scanner wrapper tests with the current MarketRiskAggregator architecture.
Production scanner/VIX/regime logic is intentionally untouched.
Resilient to the partially-applied round-2 local test edit.
"""
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "backend/tests/test_scanner.py"
s = p.read_text(encoding="utf-8")

# Real module for the current regime state.
scanner_import = "from app.core.scanner import MarketScanner, DEFENSIVE_WATCHLIST, ScannerOutcome, SpyVolatilityState\n"
risk_import = "from app.risk.market_regime import MarketRiskState\n"
if risk_import not in s:
    if scanner_import not in s:
        raise RuntimeError("scanner import anchor not found")
    s = s.replace(scanner_import, scanner_import + risk_import, 1)

# Remove the incorrect import injected by the previous local patch, wherever present.
s = s.replace("    from app.core.market_risk import MarketRiskState\n", "")
s = s.replace("from app.core.market_risk import MarketRiskState\n", "")

# Insert deterministic aggregator result immediately before each legacy wrapper call.
def inject_before(assertion: str, state: str, reasons: str, label: str) -> None:
    global s
    call = "    res = await scanner.check_spy_volatility()\n"
    pos = s.find(assertion)
    if pos < 0:
        raise RuntimeError(f"{label} assertion not found")
    call_pos = s.rfind(call, 0, pos)
    if call_pos < 0:
        raise RuntimeError(f"{label} wrapper call not found")
    injection = f"    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.{state}, 1.0, 1.0, {reasons}, {{}}))\n"
    # Idempotent: replace any existing local assess mock directly preceding the call.
    prev_line_start = s.rfind("\n", 0, call_pos - 1) + 1
    prev_line = s[prev_line_start:call_pos]
    if "scanner._regime.assess = AsyncMock(" in prev_line:
        s = s[:prev_line_start] + injection + s[call_pos:]
    else:
        s = s[:call_pos] + injection + s[call_pos:]

inject_before(
    "    assert res is SpyVolatilityState.SAFE",
    "NORMAL",
    "[]",
    "low-vol",
)
inject_before(
    "    assert res is SpyVolatilityState.PANIC",
    "PANIC",
    '["test_panic"]',
    "panic",
)

# Update comments if still old; semantics only, no production changes.
s = s.replace(
    "assert res is SpyVolatilityState.SAFE  # normal volatility",
    "assert res is SpyVolatilityState.SAFE  # NORMAL/CAUTION map to legacy SAFE",
)
s = s.replace(
    "assert res is SpyVolatilityState.PANIC  # panic volatility triggered",
    "assert res is SpyVolatilityState.PANIC  # STRESS/PANIC map to legacy PANIC",
)

p.write_text(s, encoding="utf-8")
print("Scanner tests aligned with current MarketRiskAggregator; production code unchanged.")
