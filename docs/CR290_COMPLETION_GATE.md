# CR-290 Completion Gate

CR-290 is not complete merely because the PIS package exists. Production completion requires all of the following to be true and evidenced by tests/replay.

## Live authority

- TradingEngine evaluates every open position through PIS on fresh completed-bar evidence.
- A discretionary close can reach the broker only through a confirmed PIS EXIT.
- Fixed take-profit, strategy EXIT, strategy ATR trailing, live-runner and adaptive-harvester close authority are removed from the production path.
- Legacy policies may remain only as counterfactual/shadow labels.

## Safety authority retained

- catastrophic broker protection
- hard risk / kill switch liquidation
- reconciliation correction
- duplicate-close protection
- mandatory EOD flattening for intraday mode
- operator emergency close

PIS cannot veto these mechanisms.

## State continuity

Open positions must restore original 1R, entry health, peak/trough R, lifecycle and thesis memory after process restart. Missing original risk is a fail-safe condition; the engine must not fabricate 1R.

## Validation

Promotion requires replay and shadow evidence showing positive expectancy delta, improved MFE capture, reduced premature exits, no unacceptable drawdown regression, and zero catastrophic safety regressions. Future MFE is research-only and must never enter live PIS features.

## Operational rule

Until every production-readiness gate passes, CR-290 must remain non-live. No claim of 'no loss' or guaranteed profit is valid; the objective is higher risk-adjusted realized expectancy and better right-tail capture while preserving hard downside controls.
