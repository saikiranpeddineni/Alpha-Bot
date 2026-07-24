"""Counterfactual executor for one isolated shadow child policy."""
from __future__ import annotations
from datetime import datetime
import hashlib
import math
from typing import Any
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio


class ChildPolicyExecutor:
    def __init__(self, config: ChildPolicyConfig) -> None:
        self.config = config
        self.policy_id = config.policy_id
        self.config_fingerprint = config.compute_fingerprint()
        self.processed_watermarks = {}
        if not isinstance(config.spawned_at, datetime) or config.spawned_at.tzinfo is None:
            self.status = "UNAVAILABLE"
            self.unavailable_reason = config.unavailable_reason or "MISSING_OR_NAIVE_SPAWN_TIMESTAMP"
        else:
            self.status = config.status
            self.unavailable_reason = None
        self.portfolio = SpawnVirtualPortfolio(self.policy_id, config.seed_capital, config.risk_overrides)

    def assert_config_integrity(self) -> bool:
        if self.config.compute_fingerprint() != self.config_fingerprint:
            self.status = "UNAVAILABLE"
            self.unavailable_reason = "CONFIG_FINGERPRINT_MUTATION_DETECTED"
            return False
        return True

    def is_eligible_at(self, ts: datetime) -> bool:
        return (
            self.assert_config_integrity()
            and self.status == "ACTIVE"
            and isinstance(ts, datetime)
            and ts.tzinfo is not None
            and isinstance(self.config.spawned_at, datetime)
            and self.config.spawned_at.tzinfo is not None
            and ts >= self.config.spawned_at
        )

    @staticmethod
    def _finite(value: Any) -> float | None:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError):
            return None

    def process_market_bar(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        """Advance an already-open virtual position using one completed 1-minute bar.

        This path never evaluates a new signal.  It exists solely so live shadow
        positions receive the same causal OHLC lifecycle as offline replay.
        """
        ts = observation.get("ts")
        if not isinstance(ts, datetime) or ts.tzinfo is None or not self.is_eligible_at(ts):
            return None
        symbol = str(observation.get("symbol", "")).upper()
        if not symbol or symbol not in self.portfolio.positions:
            return None
        open_px = self._finite(observation.get("open"))
        high_px = self._finite(observation.get("high"))
        low_px = self._finite(observation.get("low"))
        close_px = self._finite(observation.get("close"))
        if None in (open_px, high_px, low_px, close_px):
            return None
        bar_watermark = f"BAR|{symbol}|{ts.isoformat()}"
        if bar_watermark in self.portfolio.processed_watermarks:
            return None
        return self.portfolio.update_ohlc_and_check_exits(symbol, open_px, high_px, low_px, close_px, ts)

    def evaluate_observation(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        ts = observation.get("ts")
        if not isinstance(ts, datetime) or ts.tzinfo is None or not self.is_eligible_at(ts):
            return None
        symbol = str(observation.get("symbol", "")).upper()
        strategy = observation.get("strategy", "momentum")
        direction = str(observation.get("direction", "long")).lower()
        price = self._finite(observation.get("price"))
        if not symbol or direction not in ("long", "short") or price is None or price <= 0:
            return None
        open_px = self._finite(observation.get("open"))
        high_px = self._finite(observation.get("high"))
        low_px = self._finite(observation.get("low"))
        close_px = self._finite(observation.get("close"))
        if None in (open_px, high_px, low_px, close_px):
            return None

        champion = observation.get("champion_candidate") or {}
        candidate_id = champion.get("id") or f"{symbol}_{str(strategy).upper()}_{int(ts.timestamp())}"
        watermark = f"{self.policy_id}|{candidate_id}|{ts.isoformat()}"
        if watermark in self.processed_watermarks:
            return self.processed_watermarks[watermark]
        if watermark in self.portfolio.processed_watermarks:
            return None

        exit_record = self.portfolio.update_ohlc_and_check_exits(symbol, open_px, high_px, low_px, close_px, ts)

        signal_generated = True
        min_atr = float(self.config.strategy_overrides.get("min_atr", 0.0))
        atr = self._finite(observation.get("atr"))
        if min_atr > 0:
            signal_generated = atr is not None and atr >= min_atr
        rsi_period = int(self.config.strategy_overrides.get("rsi_period", 0))
        rsi = self._finite(observation.get("rsi"))
        if rsi_period > 0:
            signal_generated = signal_generated and rsi is not None and (rsi < 30.0 if direction == "long" else rsi > 70.0)

        raw_ml = self._finite(champion.get("ml_probability", observation.get("ml_probability")))
        ml_threshold = float(self.config.ml_overrides.get("ml_threshold", 0.48))
        ml_pass = raw_ml is not None and raw_ml >= ml_threshold
        ml_decision = ("PASS" if ml_pass else "REJECT") if raw_ml is not None else "UNAVAILABLE"

        raw_meta = self._finite(champion.get("meta_probability", observation.get("meta_probability")))
        if raw_meta is not None:
            meta_threshold = float(self.config.meta_overrides.get("meta_threshold", 0.55))
            meta_pass = raw_meta >= meta_threshold
            meta_decision = "PASS" if meta_pass else "REJECT"
        else:
            categorical_pipeline_pass = champion.get("final_decision") in ("accepted", "rejected", "bypassed")
            meta_pass = categorical_pipeline_pass
            meta_decision = "PASS_PRODUCTION_PIPELINE_EVIDENCE" if meta_pass else "UNAVAILABLE"

        raw_score = self._finite(champion.get("trade_score", observation.get("trade_score")))
        score_threshold = float(self.config.risk_overrides.get("min_trade_score", 50.0))
        score_pass = raw_score is not None and raw_score >= score_threshold
        score_decision = ("PASS" if score_pass else "REJECT") if raw_score is not None else "UNAVAILABLE"

        can_enter = self.portfolio.can_open_position(symbol)
        risk_decision = "PASS" if can_enter else "REJECT_CONCURRENCY"
        hypothetical_pass = signal_generated and ml_pass and meta_pass and score_pass and can_enter
        final_decision = "ACCEPTED" if hypothetical_pass else "REJECTED"
        champion_pass = champion.get("final_decision") in ("accepted", "bypassed")
        disagreement = (
            "BOTH_PASS" if champion_pass and hypothetical_pass
            else "BOTH_REJECT" if not champion_pass and not hypothetical_pass
            else "CHAMPION_ONLY" if champion_pass
            else "CHILD_ONLY"
        )

        stop = self._finite(observation.get("stop_loss"))
        target = self._finite(observation.get("take_profit"))
        if stop is None:
            distance = price * 0.02
            stop = price - distance if direction == "long" else price + distance

        virtual_order_id = None
        virtual_fill_price = None
        if hypothetical_pass:
            qty = self.portfolio.calculate_position_size(price, stop)
            if qty > 0:
                virtual_order_id = f"virt_{self.policy_id}_{hashlib.sha256(f'{candidate_id}|{ts.isoformat()}'.encode()).hexdigest()[:10]}"
                if self.portfolio.open_virtual_position(symbol, direction, qty, price, stop, target, ts, virtual_order_id):
                    virtual_fill_price = price

        event_id = hashlib.sha256(f"{self.policy_id}|{candidate_id}|{ts.isoformat()}|EVALUATION".encode()).hexdigest()
        self.portfolio.record_processed_watermark(watermark)
        record = {
            "id": event_id,
            "policy_id": self.policy_id,
            "parent_policy_id": self.config.parent_policy_id,
            "config_fingerprint": self.config_fingerprint,
            "candidate_id": candidate_id,
            "ts": ts,
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "signal_generated": signal_generated,
            "ml_probability": raw_ml,
            "ml_decision": ml_decision,
            "meta_probability": raw_meta,
            "meta_decision": meta_decision,
            "trade_score": raw_score,
            "trade_score_decision": score_decision,
            "risk_decision": risk_decision,
            "hypothetical_final_decision": final_decision,
            "disagreement_category": disagreement,
            "entry_price": price,
            "stop_loss": stop,
            "take_profit": target,
            "virtual_order_id": virtual_order_id,
            "virtual_fill_price": virtual_fill_price,
            "virtual_exit_price": exit_record["exit_price"] if exit_record else None,
            "virtual_exit_reason": exit_record["exit_reason"] if exit_record else None,
            "realized_r": exit_record["realized_r"] if exit_record else None,
            "mfe_r": exit_record["mfe_r"] if exit_record else None,
            "mae_r": exit_record["mae_r"] if exit_record else None,
            "payload_obj": {"checkpoint": self.portfolio.to_checkpoint_dict(), "exit_record": exit_record},
        }
        self.processed_watermarks[watermark] = record
        if len(self.processed_watermarks) > 2000:
            for key in list(self.processed_watermarks)[:500]:
                del self.processed_watermarks[key]
        return record
