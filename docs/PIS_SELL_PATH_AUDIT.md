# AlphaBot Repository Sell-Path Audit & Single Discretionary Exit Certification

## Executive Summary
This document provides a comprehensive audit of all exit and sell execution paths in AlphaBot. It certifies that **PIS (Position Intelligence System)** is the **single discretionary exit authority** for all live and paper positions, while strictly non-discretionary safety nets (Rule-41 protective stop repair, EOD Flatten, Hard Halt, Emergency Liquidation, Broker Reconciliation, Operator Override) remain isolated non-discretionary execution channels.

---

## Sell Path Audit Matrix

| Category | Function / Location | Ownership | Type | Status |
| :--- | :--- | :--- | :--- | :---: |
| **Discretionary Exit** | `PISEngineAdapter.evaluate_position_exit` | PIS Core | Discretionary Exit Authority | ✅ AUTHORITATIVE |
| **Discretionary Exit** | `PositionBrain.observe` | PIS Core | Single Source of Truth | ✅ AUTHORITATIVE |
| **Non-Discretionary Safety** | `_check_protective_stop_integrity` (Rule-41) | TradingEngine | Hard Protection Repair | ✅ PERMITTED |
| **Non-Discretionary Safety** | `flatten_all_positions` (EOD Flatten) | TradingEngine | Hard Session Flatten | ✅ PERMITTED |
| **Non-Discretionary Safety** | `trigger_hard_halt` (Kill Switch) | RiskManager / Engine | Hard Halt Safety Net | ✅ PERMITTED |
| **Non-Discretionary Safety** | `emergency_flatten` | TradingEngine | Hard Emergency Liquidation | ✅ PERMITTED |
| **Non-Discretionary Safety** | `reconcile_broker_positions` | PortfolioTracker / Engine | Broker State Sync | ✅ PERMITTED |
| **Operator Override** | API Manual Close Request | User Interface / API | Operator Override | ✅ PERMITTED |

---

## Discretionary Sell Path Subordination Audit

1. **Strategy Signal Exits**: Ordinary lagging strategy exit signals (`SignalAction.EXIT`) are subordinated once PIS runner or harvest management is active on a position.
2. **Inline Dynamic Trailing Exits**: Legacy hardcoded trailing exit math (`0.60`, `0.35`, `0.05` magic numbers) in `TradingEngine._manage_live_winner_exit` has been removed and replaced by `PISEngineAdapter`.
3. **Forbidden Sell Paths**: Verified zero uncategorized or legacy discretionary sell paths remain in the codebase.

---

## Certification Sign-Off
- **Discretionary Owner**: 100% PIS (`app.pis`)
- **Non-Discretionary Safety Nets**: Preserved & Isolated
- **Audit Date**: 2026-07-30
- **Branch**: `agent/cr-290-position-intelligence-system`
