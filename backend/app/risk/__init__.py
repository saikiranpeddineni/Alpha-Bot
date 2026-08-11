"""Risk execution hardening hooks.

The canonical RiskManager remains the sizing authority. This module only applies
post-decision invariants that are needed at the execution boundary so scanner
quality and PIS target metadata cannot be lost when Kelly allocation is active.
"""

from __future__ import annotations

import math

from app.risk import manager as _manager


if not getattr(_manager.RiskManager, "_profit_hardening_installed", False):
    _original_evaluate_entry = _manager.RiskManager.evaluate_entry

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

    _manager.RiskManager.evaluate_entry = _profit_hardened_evaluate_entry
    _manager.RiskManager._profit_hardening_installed = True
