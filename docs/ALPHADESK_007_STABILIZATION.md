# AlphaDesk 007 Stabilization

This branch is the production-stabilization pass following the AlphaDesk 006 forensic review.

## Invariants

- RiskManager is the sole final position-sizing authority.
- Planned risk is capped before any Kelly, score, or strategy multiplier can enlarge it.
- A single broker-account/symbol economic lifecycle may produce at most one closed TradeRecord.
- Trade telemetry must preserve hold duration and configuration/code lineage.
- Stop geometry must respect percentage and ATR minimum distance.
- Realized loss and execution degradation are monitored separately from planned stop risk.
- Shadow replay must not be treated as economically equivalent to a live/paper trade unless it includes entry/exit and execution assumptions.

## Stabilization defaults

- Base planned risk target: 0.25% equity.
- Hard planned-risk cap: 0.40% equity.
- Portfolio heat cap: 2.0% equity.
- Maximum open positions: 5.
- Daily loss halt: 1.5%.
- Strategy loss context threshold: 3 recent losses.
- Symbol/thesis cooldown: 30/120 minutes where applicable.
- Minimum stop distance: max($0.02, 0.10% of price, 0.50 ATR).
- Emergency realized-loss boundary: 1.50R per economic lifecycle before forced flattening.
