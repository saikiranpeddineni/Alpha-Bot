"""Align legacy scanner wrapper tests with the current MarketRiskAggregator architecture.
Production scanner/VIX/regime logic is intentionally untouched.
"""
from pathlib import Path
p = Path(__file__).resolve().parents[1] / 'backend/tests/test_scanner.py'
s = p.read_text(encoding='utf-8')

# Import the state from its real module, once, at test module scope.
old = 'from app.core.scanner import MarketScanner, DEFENSIVE_WATCHLIST, ScannerOutcome, SpyVolatilityState\n'
new = old + 'from app.risk.market_regime import MarketRiskState\n'
if new not in s:
    if old not in s: raise RuntimeError('scanner import anchor not found')
    s = s.replace(old, new, 1)

# Remove any bad round-2 import injected into an individual test.
s = s.replace('    from app.core.market_risk import MarketRiskState\n', '')

# Low-vol wrapper test: exercise wrapper mapping, not obsolete raw-bar implementation.
needle = '    mock_data_manager.history.return_value = df\n    \n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.SAFE  # normal volatility'
repl = '    mock_data_manager.history.return_value = df\n    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.NORMAL, 1.0, 1.0, [], {}))\n    \n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.SAFE  # NORMAL/CAUTION map to legacy SAFE'
if needle in s:
    s = s.replace(needle, repl, 1)
elif 'NORMAL/CAUTION map to legacy SAFE' not in s:
    raise RuntimeError('low-vol test anchor not found')

# Panic wrapper test: same principle. Also keep scanner.run() on the same mocked regime.
needle = '    mock_data_manager.history.return_value = df\n    \n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.PANIC  # panic volatility triggered'
repl = '    mock_data_manager.history.return_value = df\n    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.PANIC, 1.0, 1.0, ["test_panic"], {}))\n    \n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.PANIC  # STRESS/PANIC map to legacy PANIC'
if needle in s:
    s = s.replace(needle, repl, 1)
elif 'STRESS/PANIC map to legacy PANIC' not in s:
    raise RuntimeError('panic test anchor not found')

p.write_text(s, encoding='utf-8')
print('Scanner tests aligned with current MarketRiskAggregator; production code unchanged.')
