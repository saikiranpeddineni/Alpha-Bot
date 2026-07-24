"""Child Policy Executor for Live Shadow Spawn Laboratory.

Executes counterfactual threshold, sizing, and exit policy evaluations in isolated
virtual sandboxes without touching production broker execution or Champion loops.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Any

from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio


class ChildPolicyExecutor:
    """Executes counterfactual policy evaluation and virtual portfolio accounting for one child variant."""

    def __init__(self, config: ChildPolicyConfig) -> None:
        self.config = config
        self.policy_id = config.policy_id
        self.config_fingerprint = config.compute_fingerprint()
        self.processed_watermarks: dict[str, dict[str, Any]] = {}

        if not isinstance(config.spawned_at, datetime) or config.spawned_at.tzinfo is None:
            self.status = "UNAVAILABLE"
            self.unavailable_reason = config.unavailable_reason or "MISSING_OR_NAIVE_SPAWN_TIMESTAMP"
        else:
            self.status = config.status
            self.unavailable_reason = None

        self.portfolio = SpawnVirtualPortfolio(
            policy_id=self.policy_id, seed_capital=config.seed_capital, risk_config=config.risk_overrides
        )

    def assert_config_integrity(self) -> bool:
        if self.config.compute_fingerprint() != self.config_fingerprint:
            self.status = "UNAVAILABLE"
            self.unavailable_reason = "CONFIG_FINGERPRINT_MUTATION_DETECTED"
            return False
        return True

    def is_eligible_at(self, ts: datetime) -> bool:
        if not self.assert_config_integrity() or self.status != "ACTIVE":
            return False
        if not isinstance(ts, datetime) or ts.tzinfo is None:
            return False
        if not isinstance(self.config.spawned_at, datetime) or self.config.spawned_at.tzinfo is None:
            return False
        return ts >= self.config.spawned_at

    def evaluate_observation(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        ts = observation.get("ts")
        if not isinstance(ts, datetime) or ts.tzinfo is None or not self.is_eligible_at(ts):
            return None

        symbol = observation["symbol"].upper()
        strategy = observation.get("strategy", "momentum")
        direction = observation.get("direction", "long").lower()
        price = float(observation.get("price", 0.0))
        if price <= 0:
            return None

        open_px = float(observation.get("open", price))
        high_px = float(observation.get("high", price))
        low_px = float(observation.get("low", price))
        close_px = float(observation.get("close", price))

        champion_cand = observation.get("champion_candidate") or {}
        cand_id = champion_cand.get("id") or f"{symbol}_{strategy.upper()}_{int(ts.timestamp())}"
        watermark_key = f"{self.policy_id}|{cand_id}|{ts.isoformat()}"

        # Fast in-process replay returns the exact prior event. Durable checkpoint
        # watermarks prevent state mutation after restart; the already-persisted event
        # remains authoritative, so no duplicate event needs to be emitted.
        if watermark_key in self.processed_watermarks:
            return self.processed_watermarks[watermark_key]
        if watermark_key in self.portfolio.processed_watermarks:
            return None

        exit_rec = self.portfolio.update_ohlc_and_check_exits(symbol, open_px, high_px, low_px, close_px, ts)

        # Phase 1 candidate-policy eligibility filters. This is intentionally not a
        # reimplementation of the production strategy signal generator.
        min_atr = float(self.config.strategy_overrides.get("min_atr", 0.0))
        curr_atr = float(observation.get("atr", 1.0))
        rsi_period = int(self.config.strategy_overrides.get("rsi_period", 0))
        curr_rsi = observation.get("rsi")
        rsi_pass = True
        if rsi_period > 0 and curr_rsi is not None:
            rsi_pass = float(curr_rsi) < 30.0 if direction == "long" else float(curr_rsi) > 70.0
        signal_gen = curr_atr >= min_atr and rsi_pass

        raw_ml_prob = champion_cand.get("ml_probability")
        if raw_ml_prob is None:
            raw_ml_prob = observation.get("ml_probability")
        ml_thresh = float(self.config.ml_overrides.get("ml_threshold", 0.4800))
        ml_pass = raw_ml_prob is not None and float(raw_ml_prob) >= ml_thresh
        ml_dec = ("PASS" if ml_pass else "REJECT") if raw_ml_prob is not None else "UNAVAILABLE"

        raw_meta_prob = champion_cand.get("meta_probability")
        if raw_meta_prob is None:
            raw_meta_prob = observation.get("meta_probability")
        meta_thresh = float(self.config.meta_overrides.get("meta_threshold", 0.5500))
        meta_pass = raw_meta_prob is not None and float(raw_meta_prob) >= meta_thresh
        meta_dec = ("PASS" if meta_pass else "REJECT") if raw_meta_prob is not None else "UNAVAILABLE"

        raw_trade_score = champion_cand.get("trade_score") if "trade_score" in champion_cand else observation.get("trade_score")
        score_thresh = float(self.config.risk_overrides.get("min_trade_score", 50.0))
        score_pass = raw_trade_score is not None and float(raw_trade_score) >= score_thresh
        score_dec = ("PASS" if score_pass else "REJECT") if raw_trade_score is not None else "UNAVAILABLE"

        can_enter = self.portfolio.can_open_position(symbol)
        risk_dec = "PASS" if can_enter else "REJECT_CONCURRENCY"
        hypo_pass = signal_gen and ml_pass and meta_pass and score_pass and can_enter
        hypo_final = "ACCEPTED" if hypo_pass else "REJECTED"

        champ_pass = champion_cand.get("final_decision") in ["accepted", "bypassed"]
        if champ_pass and hypo_pass:
            cat = "BOTH_PASS"
        elif not champ_pass and not hypo_pass:
            cat = "BOTH_REJECT"
        elif champ_pass:
            cat = "CHAMPION_ONLY"
        elif hypo_pass:
            cat = "CHILD_ONLY"
        else:
            cat = "NON_COMPARABLE"

        virt_order_id = None
        virt_fill_px = None
        stop_loss = observation.get("stop_loss")
        take_profit = observation.get("take_profit")
        if not stop_loss:
            risk_dist = price * 0.02
            stop_loss = price - risk_dist if direction == "long" else price + risk_dist

        if hypo_pass:
            qty = self.portfolio.calculate_position_size(price, float(stop_loss))
            if qty > 0:
                virt_order_id = f"virt_{self.policy_id}_{hashlib.sha256(f'{cand_id}|{ts.isoformat()}'.encode()).hexdigest()[:10]}"
                pos = self.portfolio.open_virtual_position(
                    symbol, direction, qty, price, float(stop_loss), take_profit, ts, virt_order_id
                )
                if pos:
                    virt_fill_px = price

        det_evt_id = hashlib.sha256(f"{self.policy_id}|{cand_id}|{ts.isoformat()}|EVALUATION".encode("utf-8")).hexdigest()

        # Add durable watermark before serializing the checkpoint so event + state
        # persistence contains the exact transition boundary.
        self.portfolio.record_processed_watermark(watermark_key)
        event_rec = {
            "id": det_evt_id, "policy_id": self.policy_id, "parent_policy_id": self.config.parent_policy_id,
            "config_fingerprint": self.config_fingerprint, "candidate_id": cand_id, "ts": ts, "symbol": symbol,
            "strategy": strategy, "direction": direction, "signal_generated": signal_gen,
            "ml_probability": float(raw_ml_prob) if raw_ml_prob is not None else None, "ml_decision": ml_dec,
            "meta_probability": float(raw_meta_prob) if raw_meta_prob is not None else None, "meta_decision": meta_dec,
            "trade_score": float(raw_trade_score) if raw_trade_score is not None else None, "trade_score_decision": score_dec,
            "risk_decision": risk_dec, "hypothetical_final_decision": hypo_final, "disagreement_category": cat,
            "entry_price": price, "stop_loss": stop_loss, "take_profit": take_profit,
            "virtual_order_id": virt_order_id, "virtual_fill_price": virt_fill_px,
            "virtual_exit_price": exit_rec["exit_price"] if exit_rec else None,
            "virtual_exit_reason": exit_rec["exit_reason"] if exit_rec else None,
            "realized_r": exit_rec["realized_r"] if exit_rec else None,
            "mfe_r": exit_rec["mfe_r"] if exit_rec else None, "mae_r": exit_rec["mae_r"] if exit_rec else None,
            "payload_obj": {"checkpoint": self.portfolio.to_checkpoint_dict(), "exit_record": exit_rec},
        }

        self.processed_watermarks[watermark_key] = event_rec
        if len(self.processed_watermarks) > 2000:
            for k in list(self.processed_watermarks.keys())[:500]:
                del self.processed_watermarks[k]
        return event_rec
