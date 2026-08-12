# Main Regression Suite

This directory contains the behavioral regression tests that protect AlphaDesk's existing production architecture.

## Coverage

- `market_scenarios.py`: deterministic market-condition fixtures.
- `test_market_scenario_fixtures.py`: executes those fixtures through the canonical entry-intelligence path.
- `test_market_scenario_regression_suite.py`: focused edge cases for directional context, news, extension, regime, and thesis memory.
- `test_pis_profit_preservation.py`: PIS protection and profit-preservation invariants.
- `test_champion_non_regression.py`: Champion-vs-candidate promotion guardrails.

## Promotion principle

A change is not considered better merely because it produces higher raw P&L in one sample. The regression contract also considers expectancy, profit factor, drawdown, and profit capture.

## Historical replay

The deterministic fixtures are deliberately separate from historical replay. Historical replay must use the existing replay clock/feed/parity infrastructure and must not receive future bars or future news. This suite should be expanded with locked, out-of-sample replay manifests before using performance numbers as a promotion gate.
