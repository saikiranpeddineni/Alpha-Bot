"""Round-2 surgical fixes after targeted regression validation."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def rep(path, old, new, label):
    s=path.read_text(encoding='utf-8')
    if new in s: print('already applied:',label); return
    if old not in s: raise RuntimeError(f'anchor not found for {label}: {path}')
    path.write_text(s.replace(old,new,1),encoding='utf-8'); print('applied:',label)

engine=ROOT/'backend/app/core/engine.py'
rep(engine,
'                    recovery_strategy = getattr(p, "strategy", None) or "reconciled"\n                    if self.portfolio and p.symbol in self.portfolio._lots:\n                        recovery_strategy = getattr(self.portfolio._lots[p.symbol], "strategy", None) or recovery_strategy\n                    if recovery_strategy in {"alpaca", "emergency_protection", ""}:\n                        recovery_strategy = "reconciled"',
'                    recovery_strategy = getattr(p, "strategy", None)\n                    if not isinstance(recovery_strategy, str):\n                        recovery_strategy = None\n                    if self.portfolio and p.symbol in self.portfolio._lots:\n                        lot_strategy = getattr(self.portfolio._lots[p.symbol], "strategy", None)\n                        if isinstance(lot_strategy, str) and lot_strategy:\n                            recovery_strategy = lot_strategy\n                    if not recovery_strategy or recovery_strategy in {"alpaca", "emergency_protection"}:\n                        recovery_strategy = "reconciled"', 'recovery strategy type safety')
rep(engine,
'                        return f"behavioral_loop_detector: re-entry on same symbol {symbol} ({sig.strategy}/{sig_side}) within 30 mins after stop"',
'                        return f"behavioral_loop_detector: re-entry on same symbol {symbol} within 30 mins after stop ({sig.strategy}/{sig_side})"', 'stable behavioral reentry reason')
rep(engine,
'                    if turnover > 5.0:\n                        return f"behavioral_loop_detector: high portfolio churn (turnover={turnover:.2f}x equity)"',
'                    if turnover > 2.0:\n                        return f"behavioral_loop_detector: high portfolio churn/turnover (turnover={turnover:.2f}x equity)"', 'restore documented churn threshold/reason')

routes=ROOT/'backend/app/api/routes.py'
rep(routes,
'    active_spawned_capital = db.get_active_spawned_capital()\n    research_pool_balance = max(0.0, total_allocated_research - active_spawned_capital)\n    \n    active_strategies = db.get_active_spawned_strategies()',
'    active_strategies = [row for row in db.get_active_spawned_strategies() if not str(row.get("policy_id", "")).startswith("spawn_g")]\n    active_spawned_capital = sum(float(row.get("seed_capital", 0.0) or 0.0) for row in active_strategies)\n    research_pool_balance = max(0.0, total_allocated_research - active_spawned_capital)', 'spawner status manual-lab isolation')

replay=ROOT/'backend/app/replay/runtime.py'
rep(replay,
'                    await engine._tick()\n                    if engine.spawn_coordinator.running: await engine.spawn_coordinator.queue.join()',
'                    await engine._tick()\n                    spawn = getattr(getattr(engine, "shadow_research", None), "spawn", None)\n                    if spawn is not None and spawn.running:\n                        await spawn.queue.join()', 'replay spawn queue composition root')

scanner=ROOT/'backend/tests/test_scanner.py'
s=scanner.read_text(encoding='utf-8')
old='''    mock_data_manager.history.return_value = df\n\n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.PANIC  # panic volatility triggered'''
new='''    mock_data_manager.history.return_value = df\n    from app.core.market_risk import MarketRiskState\n    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.PANIC, 1.0, 1.0, ["test_panic"], {}))\n\n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.PANIC  # wrapper maps aggregator PANIC to legacy PANIC'''
if old in s:
    scanner.write_text(s.replace(old,new,1),encoding='utf-8'); print('applied: scanner wrapper architecture test')
elif 'wrapper maps aggregator PANIC' in s:
    print('already applied: scanner wrapper architecture test')
else:
    # Match current assertion block without relying on comment spacing.
    marker='    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.PANIC'
    if marker not in s: raise RuntimeError('scanner panic assertion anchor not found')
    s=s.replace('    res = await scanner.check_spy_volatility()\n', '    from app.core.market_risk import MarketRiskState\n    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.PANIC, 1.0, 1.0, ["test_panic"], {}))\n\n    res = await scanner.check_spy_volatility()\n',1)
    scanner.write_text(s,encoding='utf-8'); print('applied: scanner wrapper architecture test (fallback anchor)')
print('Round-2 regression cleanup complete.')
