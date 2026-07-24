"""Child Policy Executor for Live Shadow Spawn Laboratory.

Evaluates market context independently for a child policy variant,
manages hypothetical decisions, virtual order/fill state, and disagreement taxonomy.
Zero references to live Broker execution methods.
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio


class ChildPolicyExecutor:
    """Executes independent evaluation and virtual portfolio accounting for one child policy variant."""

    def __init__(self, config: ChildPolicyConfig) -> None:
        self.config = config
        self.policy_id = config.policy_id
        self.config_fingerprint = config.compute_fingerprint()
        
        # Enforce fail-closed on malformed spawn timestamp
        if not isinstance(config.spawned_at, datetime):
            self.status = "UNAVAILABLE"
        else:
            self.status = config.status

        self.portfolio = SpawnVirtualPortfolio(
            policy_id=self.policy_id,
            seed_capital=config.seed_capital,
            risk_config=config.risk_overrides
        )

    def is_eligible_at(self, ts: datetime) -> bool:
        """Enforces strict causal spawn boundary: child cannot evaluate data before spawned_at."""
        if self.status != "ACTIVE":
            return False
        if not isinstance(self.config.spawned_at, datetime):
            return False
        
        # Normalize timezone awareness
        spawn_ts = self.config.spawned_at
        if spawn_ts.tzinfo is None and ts.tzinfo is not None:
            spawn_ts = spawn_ts.replace(tzinfo=ts.tzinfo)
        elif spawn_ts.tzinfo is not None and ts.tzinfo is None:
            spawn_ts = spawn_ts.astimezone(ts.tzinfo)
            
        return ts >= spawn_ts

    def evaluate_observation(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        """Evaluates a market observation snapshot independently for this child policy variant."""
        ts = observation.get("ts") or datetime.now(timezone.utc)
        if not self.is_eligible_at(ts):
            return None

        symbol = observation["symbol"].upper()
        strategy = observation.get("strategy", "momentum")
        direction = observation.get("direction", "long").lower()
        price = float(observation.get("price", 0.0))
        champion_cand = observation.get("champion_candidate") or {}
        cand_id = champion_cand.get("id") or f"{symbol}_{strategy.upper()}_{int(ts.timestamp())}"

        # Check existing position price updates & exits
        exit_rec = self.portfolio.update_price_and_check_exits(symbol, price, ts)

        # Independent Policy Evaluation
        # 1. Strategy Signal check (overrides check)
        min_atr = float(self.config.strategy_overrides.get("min_atr", 0.0))
        curr_atr = float(observation.get("atr", 1.0))
        signal_gen = curr_atr >= min_atr

        # 2. ML Probability check
        raw_ml_prob = champion_cand.get("ml_probability")
        if raw_ml_prob is None:
            raw_ml_prob = observation.get("ml_probability", 0.50)
        
        ml_thresh = float(self.config.ml_overrides.get("ml_threshold", 0.4800))
        ml_pass = float(raw_ml_prob) >= ml_thresh
        ml_dec = "PASS" if ml_pass else "REJECT"

        # 3. Meta Label Probability check
        raw_meta_prob = champion_cand.get("meta_probability")
        if raw_meta_prob is None:
            raw_meta_prob = observation.get("meta_probability", 0.60)
            
        meta_thresh = float(self.config.meta_overrides.get("meta_threshold", 0.5500))
        meta_pass = float(raw_meta_prob) >= meta_thresh
        meta_dec = "PASS" if meta_pass else "REJECT"

        # 4. Trade Score check
        raw_trade_score = champion_cand.get("trade_score") or observation.get("trade_score", 70.0)
        score_thresh = float(self.config.risk_overrides.get("min_trade_score", 50.0))
        score_pass = float(raw_trade_score) >= score_thresh

        # 5. Virtual Risk / Concurrency check
        can_enter = self.portfolio.can_open_position(symbol)
        risk_dec = "PASS" if can_enter else "REJECT_CONCURRENCY"

        hypo_pass = signal_gen and ml_pass and meta_pass and score_pass and can_enter
        hypo_final = "ACCEPTED" if hypo_pass else "REJECTED"

        # Disagreement Taxonomy vs Champion
        champ_pass = champion_cand.get("final_decision") in ["accepted", "bypassed"]
        if champ_pass and hypo_pass:
            cat = "BOTH_PASS"
        elif not champ_pass and not hypo_pass:
            cat = "BOTH_REJECT"
        elif champ_pass and not hypo_pass:
            cat = "CHAMPION_ONLY"
        elif not champ_pass and hypo_pass:
            cat = "CHILD_ONLY"
        else:
            cat = "NON_COMPARABLE"

        # Execute Virtual Order if ACCEPTED
        virt_order_id = None
        virt_fill_px = None
        stop_loss = observation.get("stop_loss")
        take_profit = observation.get("take_profit")
        
        if not stop_loss:
            risk_dist = price * 0.02
            stop_loss = price - risk_dist if direction == "long" else price + risk_dist

        if hypo_pass:
            qty = self.portfolio.calculate_position_size(price, stop_loss)
            virt_order_id = f"virt_{self.policy_id}_{uuid.uuid4().hex[:8]}"
            pos = self.portfolio.open_virtual_position(
                symbol=symbol,
                side=direction,
                qty=qty,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                now=ts,
                order_id=virt_order_id
            )
            if pos:
                virt_fill_px = price

        event_rec = {
            "id": f"evt_{self.policy_id}_{uuid.uuid4().hex[:12]}",
            "policy_id": self.policy_id,
            "parent_policy_id": self.config.parent_policy_id,
            "config_fingerprint": self.config_fingerprint,
            "candidate_id": cand_id,
            "ts": ts,
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "signal_generated": signal_gen,
            "ml_probability": float(raw_ml_prob) if raw_ml_prob is not None else None,
            "ml_decision": ml_dec,
            "meta_probability": float(raw_meta_prob) if raw_meta_prob is not None else None,
            "meta_decision": meta_dec,
            "trade_score": float(raw_trade_score) if raw_trade_score is not None else None,
            "risk_decision": risk_dec,
            "hypothetical_final_decision": hypo_final,
            "disagreement_category": cat,
            "entry_price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "virtual_order_id": virt_order_id,
            "virtual_fill_price": virt_fill_px,
            "virtual_exit_price": exit_rec["exit_price"] if exit_rec else None,
            "virtual_exit_reason": exit_rec["exit_reason"] if exit_rec else None,
            "realized_r": exit_rec["realized_r"] if exit_rec else None,
            "mfe_r": exit_rec["mfe_r"] if exit_rec else None,
            "mae_r": exit_rec["mae_r"] if exit_rec else None,
            "payload_obj": {
                "checkpoint": self.portfolio.to_checkpoint_dict(),
                "exit_record": exit_rec,
            }
        }
        return event_rec
