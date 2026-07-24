"""Counterfactual executor for one isolated shadow child policy."""
from __future__ import annotations
from datetime import datetime
import hashlib, math
from typing import Any
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_virtual_portfolio import SpawnVirtualPortfolio

class ChildPolicyExecutor:
    def __init__(self,config:ChildPolicyConfig)->None:
        self.config=config; self.policy_id=config.policy_id; self.config_fingerprint=config.compute_fingerprint(); self.processed_watermarks={}
        if not isinstance(config.spawned_at,datetime) or config.spawned_at.tzinfo is None:self.status="UNAVAILABLE"; self.unavailable_reason=config.unavailable_reason or "MISSING_OR_NAIVE_SPAWN_TIMESTAMP"
        else:self.status=config.status; self.unavailable_reason=None
        self.portfolio=SpawnVirtualPortfolio(self.policy_id,config.seed_capital,config.risk_overrides)
    def assert_config_integrity(self)->bool:
        if self.config.compute_fingerprint()!=self.config_fingerprint:self.status="UNAVAILABLE"; self.unavailable_reason="CONFIG_FINGERPRINT_MUTATION_DETECTED"; return False
        return True
    def is_eligible_at(self,ts:datetime)->bool:
        return self.assert_config_integrity() and self.status=="ACTIVE" and isinstance(ts,datetime) and ts.tzinfo is not None and isinstance(self.config.spawned_at,datetime) and self.config.spawned_at.tzinfo is not None and ts>=self.config.spawned_at
    @staticmethod
    def _finite(v:Any)->float|None:
        try:x=float(v); return x if math.isfinite(x) else None
        except (TypeError,ValueError):return None
    def evaluate_observation(self,observation:dict[str,Any])->dict[str,Any]|None:
        ts=observation.get("ts")
        if not isinstance(ts,datetime) or ts.tzinfo is None or not self.is_eligible_at(ts):return None
        symbol=str(observation.get("symbol","")).upper(); strategy=observation.get("strategy","momentum"); direction=str(observation.get("direction","long")).lower()
        price=self._finite(observation.get("price"))
        if not symbol or direction not in ("long","short") or price is None or price<=0:return None
        open_px=self._finite(observation.get("open")); high_px=self._finite(observation.get("high")); low_px=self._finite(observation.get("low")); close_px=self._finite(observation.get("close"))
        if None in (open_px,high_px,low_px,close_px):return None
        champion=observation.get("champion_candidate") or {}; cand_id=champion.get("id") or f"{symbol}_{str(strategy).upper()}_{int(ts.timestamp())}"; watermark=f"{self.policy_id}|{cand_id}|{ts.isoformat()}"
        if watermark in self.processed_watermarks:return self.processed_watermarks[watermark]
        if watermark in self.portfolio.processed_watermarks:return None
        exit_rec=self.portfolio.update_ohlc_and_check_exits(symbol,open_px,high_px,low_px,close_px,ts)
        # Optional strategy evidence is fail-closed when the child explicitly requests it.
        signal_gen=True; min_atr=float(self.config.strategy_overrides.get("min_atr",0.0)); atr=self._finite(observation.get("atr"))
        if min_atr>0:signal_gen=atr is not None and atr>=min_atr
        rsi_period=int(self.config.strategy_overrides.get("rsi_period",0)); rsi=self._finite(observation.get("rsi"))
        if rsi_period>0:signal_gen=signal_gen and rsi is not None and (rsi<30. if direction=="long" else rsi>70.)
        raw_ml=self._finite(champion.get("ml_probability",observation.get("ml_probability"))); ml_thresh=float(self.config.ml_overrides.get("ml_threshold",.48)); ml_pass=raw_ml is not None and raw_ml>=ml_thresh; ml_dec=("PASS" if ml_pass else "REJECT") if raw_ml is not None else "UNAVAILABLE"
        raw_meta=self._finite(champion.get("meta_probability",observation.get("meta_probability")))
        if raw_meta is not None:
            meta_thresh=float(self.config.meta_overrides.get("meta_threshold",.55)); meta_pass=raw_meta>=meta_thresh; meta_dec="PASS" if meta_pass else "REJECT"
        else:
            # Current engine bridge publishes only signals that already passed production pipeline Meta.
            # Preserve that categorical fact without inventing a probability. Meta threshold mutation is
            # therefore intentionally excluded from the evolution engine until raw Meta evidence is wired.
            categorical_pipeline_pass=champion.get("final_decision") in ("accepted","rejected","bypassed")
            meta_pass=categorical_pipeline_pass; meta_dec="PASS_PRODUCTION_PIPELINE_EVIDENCE" if meta_pass else "UNAVAILABLE"
        raw_score=self._finite(champion.get("trade_score",observation.get("trade_score"))); score_thresh=float(self.config.risk_overrides.get("min_trade_score",50.0)); score_pass=raw_score is not None and raw_score>=score_thresh; score_dec=("PASS" if score_pass else "REJECT") if raw_score is not None else "UNAVAILABLE"
        can_enter=self.portfolio.can_open_position(symbol); risk_dec="PASS" if can_enter else "REJECT_CONCURRENCY"; hypo=signal_gen and ml_pass and meta_pass and score_pass and can_enter; final="ACCEPTED" if hypo else "REJECTED"
        champ_pass=champion.get("final_decision") in ("accepted","bypassed"); cat="BOTH_PASS" if champ_pass and hypo else "BOTH_REJECT" if not champ_pass and not hypo else "CHAMPION_ONLY" if champ_pass else "CHILD_ONLY"
        stop=self._finite(observation.get("stop_loss")); target=self._finite(observation.get("take_profit"))
        if stop is None:
            dist=price*.02; stop=price-dist if direction=="long" else price+dist
        virt_id=None; virt_fill=None
        if hypo:
            qty=self.portfolio.calculate_position_size(price,stop)
            if qty>0:
                virt_id=f"virt_{self.policy_id}_{hashlib.sha256(f'{cand_id}|{ts.isoformat()}'.encode()).hexdigest()[:10]}"
                if self.portfolio.open_virtual_position(symbol,direction,qty,price,stop,target,ts,virt_id):virt_fill=price
        event_id=hashlib.sha256(f"{self.policy_id}|{cand_id}|{ts.isoformat()}|EVALUATION".encode()).hexdigest(); self.portfolio.record_processed_watermark(watermark)
        rec={"id":event_id,"policy_id":self.policy_id,"parent_policy_id":self.config.parent_policy_id,"config_fingerprint":self.config_fingerprint,"candidate_id":cand_id,"ts":ts,"symbol":symbol,"strategy":strategy,"direction":direction,"signal_generated":signal_gen,
             "ml_probability":raw_ml,"ml_decision":ml_dec,"meta_probability":raw_meta,"meta_decision":meta_dec,"trade_score":raw_score,"trade_score_decision":score_dec,"risk_decision":risk_dec,"hypothetical_final_decision":final,"disagreement_category":cat,
             "entry_price":price,"stop_loss":stop,"take_profit":target,"virtual_order_id":virt_id,"virtual_fill_price":virt_fill,"virtual_exit_price":exit_rec["exit_price"] if exit_rec else None,"virtual_exit_reason":exit_rec["exit_reason"] if exit_rec else None,
             "realized_r":exit_rec["realized_r"] if exit_rec else None,"mfe_r":exit_rec["mfe_r"] if exit_rec else None,"mae_r":exit_rec["mae_r"] if exit_rec else None,"payload_obj":{"checkpoint":self.portfolio.to_checkpoint_dict(),"exit_record":exit_rec}}
        self.processed_watermarks[watermark]=rec
        if len(self.processed_watermarks)>2000:
            for k in list(self.processed_watermarks)[:500]:del self.processed_watermarks[k]
        return rec
