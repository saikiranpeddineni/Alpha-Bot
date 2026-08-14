"""Risk execution hardening hooks.

The canonical RiskManager remains the sizing authority. This module only applies
post-decision invariants that are needed at the execution boundary so scanner
quality and PIS target metadata cannot be lost when Kelly allocation is active.

Beast Mode principle:
    A losing trade is evidence about a *setup*, not proof that the symbol is bad.
    Historical symbol-loss cooldowns therefore remain telemetry/risk memory, but
    they are not a hard symbol blacklist. The durable same-thesis block remains
    authoritative, while the existing global post-trade wait prevents churn.
"""

from __future__ import annotations

import math

from app.risk import manager as _manager


if not getattr(_manager.RiskManager, "_profit_hardening_installed", False):
    _original_evaluate_entry = _manager.RiskManager.evaluate_entry
    _original_is_entry_permitted = _manager.RiskManager.is_entry_permitted

    def _thesis_aware_is_entry_permitted(self, signal, now=None):
        """Do not turn historical losses into a permanent symbol blacklist.

        The canonical manager still enforces:
          - hard/daily halts
          - strategy loss-streak protection
          - global Beast Mode wait
          - same-thesis loss memory
          - entry-quality / extension gates
          - session and instrument guards

        Only the generic symbol/behavioral cooldown containers are softened here.
        They are still reconstructed and persisted for telemetry, but a fresh
        thesis can compete for a new entry. This preserves opportunity flow while
        stopping the exact same failed thesis through ``closed_theses``.
        """
        saved_symbol = self.symbol_cooldowns
        saved_behavioral = self.behavioral_loop_cooldowns
        try:
            self.symbol_cooldowns = {}
            self.behavioral_loop_cooldowns = {}
            decision = _original_is_entry_permitted(self, signal, now=now)
        finally:
            self.symbol_cooldowns = saved_symbol
            self.behavioral_loop_cooldowns = saved_behavioral
        return decision

    def _profit_hardened_evaluate_entry(self, signal, ctx, bars, now=None, kelly_override=None):
        decision = _original_evaluate_entry(
            self,
            signal,
            ctx,
            bars,
            now=now,
            kelly_override=kelly_override,
        )
        if not decision.approved or decision.order is None:
            return decision

        order = decision.order

        # PIS owns discretionary targets, but the original strategy target must
        # survive the broker boundary as metadata. The broker adapter strips this
        # field before submission and restores it on the local Order object.
        native_target = getattr(signal, "take_profit", None)
        if native_target is not None and float(native_target) > 0:
            order.take_profit = float(native_target)

        # When Kelly allocation is active, the legacy RiskManager branch bypasses
        # its scanner-quality scaling. Apply a continuous scanner-quality factor
        # after sizing so discovery quality cannot be silently discarded. The
        # factor is derived from the observed scanner score and configured minimum
        # specialist AUC; no strategy-specific thresholds are introduced here.
        if getattr(signal, "discovery_source", None) == "scanner":
            scanner_score = getattr(signal, "scanner_score", None)
            scanner_auc = getattr(signal, "scanner_auc", None)
            min_auc = float(getattr(self.config, "min_specialist_auc", 0.0) or 0.0)
            if (
                isinstance(scanner_score, (int, float))
                and isinstance(scanner_auc, (int, float))
                and math.isfinite(float(scanner_score))
                and math.isfinite(float(scanner_auc))
                and min_auc > 0
            ):
                score_factor = min(max(float(scanner_score), 0.0), 1.0)
                auc_factor = min(max(float(scanner_auc) / min_auc, 0.0), 1.0)
                quality_factor = score_factor * auc_factor
                original_qty = float(order.qty)
                adjusted_qty = math.floor(original_qty * quality_factor)
                if adjusted_qty >= 1 and adjusted_qty < original_qty:
                    order.qty = adjusted_qty
                    decision.reason = (
                        f"{decision.reason}; scanner_quality_factor={quality_factor:.4f} "
                        f"qty={original_qty:g}->{adjusted_qty:g}"
                    )

        return decision

    _manager.RiskManager.is_entry_permitted = _thesis_aware_is_entry_permitted
    _manager.RiskManager.evaluate_entry = _profit_hardened_evaluate_entry
    _manager.RiskManager._profit_hardening_installed = True
