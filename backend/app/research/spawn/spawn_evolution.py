"""Bounded evolutionary controller for the SHADOW-ONLY spawn laboratory."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib, random
from typing import Iterable
from app.research.spawn.spawn_config import ChildPolicyConfig
from app.research.spawn.spawn_executor import ChildPolicyExecutor

@dataclass(frozen=True)
class EvolutionSettings:
    max_population:int=24; elite_count:int=6; children_per_cycle:int=6; min_trades_for_selection:int=12
    max_drawdown_pct:float=12.0; mutation_fraction:float=0.08; seed_capital:float=2500.0; bootstrap_population:int=6
@dataclass(frozen=True)
class FitnessSnapshot:
    policy_id:str; trades:int; realized_r:float; expectancy_r:float; drawdown_pct:float; equity_return_pct:float; fitness:float; eligible:bool

class SpawnEvolutionEngine:
    """Research-only evolution. No broker imports and no production promotion capability."""
    PARAM_BOUNDS={("ml","ml_threshold"):(0.30,0.80),("meta","meta_threshold"):(0.30,0.85),("risk","min_trade_score"):(20.,95.),
                  ("risk","risk_per_trade_pct"):(0.10,2.00),("risk","max_position_pct"):(2.,35.),("risk","trailing_stop_atr_mult"):(0.,4.)}
    def __init__(self,settings:EvolutionSettings|None=None,seed:int=146)->None:
        self.settings=settings or EvolutionSettings(); self.rng=random.Random(seed); self.generation=0
        if self.settings.elite_count>=self.settings.max_population:raise ValueError("elite_count must be smaller than max_population")
    def score(self,e:ChildPolicyExecutor)->FitnessSnapshot:
        p=e.portfolio; trades=int(p.trade_count); rr=float(p.realized_r); exp=rr/trades if trades else 0.; dd=float(p.get_drawdown_pct())
        ret=((p.marked_equity/p.seed_capital)-1.)*100. if p.seed_capital else 0.
        eligible=e.status=="ACTIVE" and trades>=self.settings.min_trades_for_selection and dd<=self.settings.max_drawdown_pct
        fit=exp-0.05*dd+0.01*ret
        if not eligible:fit=-1_000_000.+fit
        return FitnessSnapshot(e.policy_id,trades,rr,exp,dd,ret,fit,eligible)
    def rank(self,executors:Iterable[ChildPolicyExecutor])->list[FitnessSnapshot]:
        return sorted((self.score(e) for e in executors),key=lambda x:(x.fitness,x.trades,x.policy_id),reverse=True)
    def select_elites(self,executors:dict[str,ChildPolicyExecutor])->list[ChildPolicyExecutor]:
        return [executors[x.policy_id] for x in self.rank(executors.values()) if x.eligible][:self.settings.elite_count]
    def bootstrap_configs(self,now:datetime|None=None)->list[ChildPolicyConfig]:
        """Small diverse initial cohort; all values are explicit research hypotheses."""
        now=now or datetime.now(timezone.utc)
        seeds=[(0.44,0.50,45.,0.8),(0.48,0.55,50.,1.0),(0.52,0.60,55.,0.8),(0.56,0.55,60.,0.6),(0.48,0.62,65.,0.6),(0.42,0.58,55.,0.5)]
        out=[]
        for i,(ml,meta,score,risk) in enumerate(seeds[:self.settings.bootstrap_population]):
            out.append(ChildPolicyConfig(policy_id=f"spawn_g0_seed_{i+1:02d}",parent_policy_id="champion",policy_version="g0",spawned_at=now,
                seed_capital=self.settings.seed_capital,execution_mode="SHADOW_ONLY",ml_overrides={"ml_threshold":ml},meta_overrides={"meta_threshold":meta},
                risk_overrides={"min_trade_score":score,"risk_per_trade_pct":risk,"max_position_pct":20.0},spawn_reason="BOOTSTRAP_DIVERSE_COHORT"))
        return out
    @staticmethod
    def _copy(c:ChildPolicyConfig):return dict(c.strategy_overrides),dict(c.ml_overrides),dict(c.meta_overrides),dict(c.risk_overrides)
    def _mutate_value(self,v:float,lo:float,hi:float)->float:
        span=hi-lo; return min(hi,max(lo,float(v)+self.rng.uniform(-span*self.settings.mutation_fraction,span*self.settings.mutation_fraction)))
    def mutate(self,parent:ChildPolicyConfig,now:datetime|None=None)->ChildPolicyConfig:
        now=now or datetime.now(timezone.utc); strategy,ml,meta,risk=self._copy(parent); group,key=self.rng.choice(list(self.PARAM_BOUNDS)); target={"ml":ml,"meta":meta,"risk":risk}[group]; lo,hi=self.PARAM_BOUNDS[(group,key)]
        defaults={"ml_threshold":.48,"meta_threshold":.55,"min_trade_score":50.,"risk_per_trade_pct":1.,"max_position_pct":25.,"trailing_stop_atr_mult":0.}
        target[key]=round(self._mutate_value(float(target.get(key,defaults[key])),lo,hi),6)
        raw=f"{parent.policy_id}|{parent.compute_fingerprint()}|{self.generation}|{key}|{target[key]}|{now.isoformat()}"
        cid=f"spawn_g{self.generation}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return ChildPolicyConfig(policy_id=cid,parent_policy_id=parent.policy_id,policy_version=f"g{self.generation}",spawned_at=now,
            seed_capital=min(parent.seed_capital,self.settings.seed_capital),execution_mode="SHADOW_ONLY",strategy_overrides=strategy,ml_overrides=ml,
            meta_overrides=meta,risk_overrides=risk,code_commit_sha=parent.code_commit_sha,model_manifest_sha=parent.model_manifest_sha,spawn_reason=f"EVOLUTION_MUTATION:{group}.{key}")
    def next_generation(self,executors:dict[str,ChildPolicyExecutor],now:datetime|None=None)->list[ChildPolicyConfig]:
        elites=self.select_elites(executors)
        if not elites:return []
        room=max(0,self.settings.max_population-sum(e.status=="ACTIVE" for e in executors.values())); count=min(room,self.settings.children_per_cycle)
        if count<=0:return []
        self.generation+=1; children=[]; seen={e.config_fingerprint for e in executors.values()}; attempts=0
        while len(children)<count and attempts<count*20:
            parent=elites[attempts%len(elites)]; attempts+=1; child=self.mutate(parent.config,now); fp=child.compute_fingerprint()
            if fp in seen:continue
            seen.add(fp); children.append(child)
        return children
    def retirement_candidates(self,executors:dict[str,ChildPolicyExecutor])->list[str]:
        scored=self.rank(executors.values()); sampled=[x for x in scored if x.trades>=self.settings.min_trades_for_selection]
        weak=[x.policy_id for x in sampled if x.drawdown_pct>self.settings.max_drawdown_pct or x.expectancy_r<-.20]
        overflow=max(0,sum(e.status=="ACTIVE" for e in executors.values())-self.settings.max_population)
        if overflow:weak.extend([x.policy_id for x in reversed(scored) if x.trades>=self.settings.min_trades_for_selection][:overflow])
        return list(dict.fromkeys(weak))
