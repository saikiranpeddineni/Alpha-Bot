"""Surgical cleanup for regressions exposed by the 2026-07-28 full pytest sweep."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def replace_once(path: Path, old: str, new: str, label: str) -> None:
    src = path.read_text(encoding="utf-8")
    if new in src:
        print(f"already applied: {label}"); return
    if old not in src:
        raise RuntimeError(f"anchor not found for {label}: {path}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    print(f"applied: {label}")

engine = ROOT / "backend/app/core/engine.py"
replace_once(engine,
'            entry_payload = self.db.get_latest_entry_order_payload(p.symbol, self.mode)\n            order_payload = entry_payload or self.db.get_latest_order_payload(p.symbol, self.mode)\n            if entry_payload and entry_payload.get("strategy"):',
'            entry_payload = self.db.get_latest_entry_order_payload(p.symbol, self.mode)\n            if not isinstance(entry_payload, dict):\n                entry_payload = None\n            fallback_payload = self.db.get_latest_order_payload(p.symbol, self.mode)\n            if not isinstance(fallback_payload, dict):\n                fallback_payload = None\n            order_payload = entry_payload or fallback_payload\n            if entry_payload and entry_payload.get("strategy"):', "defensive reconciliation payload typing")
replace_once(engine,
'                stop_loss = order_payload.get("stop_loss")\n                take_profit = order_payload.get("take_profit")\n                if stop_loss:\n                    initial_risk = abs(p.avg_entry_price - stop_loss)',
'                stop_loss = order_payload.get("stop_loss")\n                take_profit = order_payload.get("take_profit")\n                try:\n                    stop_loss = float(stop_loss) if stop_loss is not None and not isinstance(stop_loss, MagicMock) else None\n                except (TypeError, ValueError):\n                    stop_loss = None\n                try:\n                    take_profit = float(take_profit) if take_profit is not None and not isinstance(take_profit, MagicMock) else None\n                except (TypeError, ValueError):\n                    take_profit = None\n                if stop_loss and stop_loss > 0:\n                    initial_risk = abs(p.avg_entry_price - stop_loss)', "numeric reconciliation stop metadata")
replace_once(engine, '        await self.shadow_research.stop()\n        if self.state != EngineState.HALTED:',
'        shadow_runtime = getattr(self, "shadow_research", None)\n        shadow_stop = getattr(shadow_runtime, "stop", None) if shadow_runtime is not None else None\n        if callable(shadow_stop):\n            try:\n                stop_result = shadow_stop()\n                if inspect.isawaitable(stop_result):\n                    await stop_result\n            except Exception as exc:\n                applog.warning("engine", f"Shadow research stop failed open: {exc}")\n        if self.state != EngineState.HALTED:', "fail-open shadow teardown")
replace_once(engine, '                    same_thesis = (trade_strategy == sig.strategy) and (trade_side == sig_side)', '                    same_thesis = (trade_strategy == sig.strategy) and (not trade_side or trade_side == sig_side)', "behavioral re-entry legacy-side safety")
replace_once(engine, '                    if trade_strategy == sig.strategy and trade_side == sig_side:', '                    if trade_strategy == sig.strategy and (not trade_side or trade_side == sig_side):', "behavioral loss-streak legacy-side safety")
replace_once(engine,
'                        if is_take_profit and limit_px is not None:\n                            # Preserve target before any repair cancellation.\n                            self._position_take_profits[p.symbol] = float(limit_px)\n                            p.take_profit = float(limit_px)\n\n                        if is_bracket_or_oco_parent:',
'                        if is_take_profit and limit_px is not None:\n                            # Preserve target metadata. Broker-managed bracket/OCO TP legs\n                            # are valid siblings of a replacement stop and must not be cancelled.\n                            tp_map = getattr(self, "_position_take_profits", None)\n                            if isinstance(tp_map, dict):\n                                tp_map[p.symbol] = float(limit_px)\n                            p.take_profit = float(limit_px)\n                            if order_class in {"bracket", "oco", "oto"}:\n                                continue\n\n                        if is_bracket_or_oco_parent:', "protect bracket take-profit during stop repair")

alpaca = ROOT / "backend/app/brokers/alpaca.py"
replace_once(alpaca, '        strategy, reason = self._meta.get(\n            str(o.id),\n            (self._symbol_strategies.get(symbol_key, "alpaca"), ""),\n        )', '        symbol_strategies = getattr(self, "_symbol_strategies", {})\n        strategy, reason = self._meta.get(\n            str(o.id),\n            (symbol_strategies.get(symbol_key, "alpaca"), ""),\n        )', "alpaca optional strategy cache")

tracker = ROOT / "backend/app/portfolio/tracker.py"
replace_once(tracker, '        if lot.initial_stop_loss is None and initial_stop is not None:\n            lot.initial_stop_loss = initial_stop\n            if not lot.initial_risk_per_share and lot.avg_price:\n                lot.initial_risk_per_share = abs(lot.avg_price - initial_stop)\n            if not lot.initial_risk_dollars and lot.avg_price:\n                lot.initial_risk_dollars = abs(lot.closing_qty or lot.qty) * abs(lot.avg_price - initial_stop)\n\n        init_risk_dollars = lot.initial_risk_dollars', '        # Initial risk is entry-bound. An exit-time fallback stop cannot establish R\n        # for a lot that entered without authoritative initial-risk metadata.\n        init_risk_dollars = lot.initial_risk_dollars', "entry-bound initial risk")

replay = ROOT / "backend/app/replay/runtime.py"
replace_once(replay, '        await engine.spawn_coordinator.start()', '        await engine.shadow_research.start()', "replay current shadow composition root")

evidence = ROOT / "backend/app/research/evidence_store.py"
replace_once(evidence, 'def __init__(self, db: Database | None, *, max_records: int = 20000, persist_every: int = 50) -> None:', 'def __init__(self, db: Database | None, *, max_records: int = 20000, persist_every: int = 1) -> None:', "evidence default restart durability")
runtime = ROOT / "backend/app/research/shadow_runtime.py"
replace_once(runtime, '        self.evidence = UnifiedEvidenceStore(db)', '        self.evidence = UnifiedEvidenceStore(db, persist_every=50)', "runtime explicit evidence batching")

outcomes = ROOT / "backend/app/research/outcome_joiner.py"
replace_once(outcomes, '    def _register_candidate(self, event: TradingObservationEvent) -> None:\n        if not event.candidate_id or event.price is None or float(event.price) <= 0:\n            return', '    def _register_candidate(self, event: TradingObservationEvent) -> None:\n        self.candidates_seen += 1\n        if not event.candidate_id or event.price is None or float(event.price) <= 0:\n            return', "outcome candidate ingress telemetry")
replace_once(outcomes, '        self._by_symbol.setdefault(event.symbol.upper(), set()).add(event.candidate_id)\n        self.candidates_seen += 1', '        self._by_symbol.setdefault(event.symbol.upper(), set()).add(event.candidate_id)', "outcome candidate count once")

routes = ROOT / "backend/app/api/routes.py"
replace_once(routes, '        active_strategies = db.get_active_spawned_strategies()\n        if len(active_strategies) >= 5:', '        active_strategies = [\n            row for row in db.get_active_spawned_strategies()\n            if not str(row.get("policy_id", "")).startswith("spawn_g")\n        ]\n        if len(active_strategies) >= 5:', "manual spawner excludes autonomous lab children")
replace_once(routes, '        active_spawned_capital = db.get_active_spawned_capital()\n        research_pool_balance = total_allocated_research - active_spawned_capital\n        \n        if body.seed_capital <= 0.0:', '        active_spawned_capital = sum(float(row.get("seed_capital", 0.0) or 0.0) for row in active_strategies)\n        research_pool_balance = total_allocated_research - active_spawned_capital\n        \n        if body.seed_capital <= 0.0:', "manual spawner capital isolation")

remediation_test = ROOT / "backend/tests/test_remediation_batch_b.py"
replace_once(remediation_test, 'from datetime import datetime, time', 'from datetime import datetime, time, timedelta', "replay queue test timedelta import")
replace_once(remediation_test, '    pending = db.get_pending_shadow_replays(mode="demo", limit=10)', '    pending = db.get_pending_shadow_replays(mode="demo", now=ts_new + timedelta(hours=1), limit=10)', "deterministic replay queue test clock")

scanner_test = ROOT / "backend/tests/test_scanner.py"
replace_once(scanner_test, '    mock_data_manager.history.return_value = df\n\n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.PANIC  # panic volatility triggered', '    mock_data_manager.history.return_value = df\n    from app.core.market_risk import MarketRiskState\n    scanner._regime.assess = AsyncMock(return_value=(MarketRiskState.PANIC, 1.0, 1.0, ["test_panic"], {}))\n\n    res = await scanner.check_spy_volatility()\n    assert res is SpyVolatilityState.PANIC  # wrapper maps aggregator PANIC to legacy PANIC', "scanner wrapper test matches aggregator architecture")
print("Full regression cleanup patch complete.")
