# CR-290 PIS Exit Authority

## Decision

PIS is the sole owner of **discretionary live exit policy** for the CR-290 architecture.

Legacy take-profit, trailing-profit, strategy-exit, adaptive-profit-harvester and exit-challenger logic must not independently close a live production position after migration is complete. They may remain temporarily as replay baselines until equivalent evidence has been captured, then should be deleted or archived from the production path.

## What is not a competing exit strategy

The following remain authoritative safety/lifecycle mechanisms and MUST NOT be removed as "old exits":

- catastrophic broker stop protection
- kill-switch / hard risk liquidation
- reconciliation correction
- duplicate-close protection
- pre-earnings mandatory liquidation when configured as a portfolio safety policy
- mandatory end-of-day flattening for an intraday-only system
- manual/operator emergency close

These mechanisms protect capital or enforce operating constraints. PIS may not veto them.

## Migration rule

No live exit path may be removed before PIS can reconstruct the position's initial risk and has sufficiently fresh point-in-time evidence to make a decision. Missing/stale PIS evidence is a safety fault, not permission to run an unprotected position.

## Target live flow

```text
open/reconciled position
        |
        v
PIS PositionBrain
        |
        +-- Trade Health
        +-- Thesis Drift
        +-- lifecycle
        +-- current/peak/trough R
        |
        v
PIS Exit Planner
        |
        +-- HOLD
        +-- KEEP_PLAN
        +-- TIGHTEN_PROTECTION
        +-- TRAIL
        +-- HARVEST_PARTIAL
        +-- EXIT
        |
        v
risk/safety invariant validation
        |
        v
broker execution
```

## CR-290 first implementation

The first code slice establishes the PIS contracts and deterministic PositionBrain. It intentionally does **not** delete broker safety protection or blindly disable the current engine exit path before live integration and regression coverage exist. The next migration slice must route the engine's discretionary exit decisions through `PositionIntelligenceSystem`, then remove the superseded legacy policy calls in the same branch.
