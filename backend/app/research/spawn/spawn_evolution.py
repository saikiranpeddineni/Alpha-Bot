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
    max_population:int=24; elite_count:int=6; children_per_cycle:int=6; min_trades_for_selection:int=12; max_drawdown_pct:float=12.; mutation_fraction:float=.08; seed_capital:float=2500.; bootstrap_population:int=6
@dataclass(frozen=True)
class FitnessSnapshot:
    policy_id:str; trades:int; realized_r:float; expectancy_r:float; drawdown_pct:float; equity_return_pct:float; fitness:float; eligible:bool
class SpawnEvolutionEngine:
    """Research-only evolution; parameters are limited to evidence the bridge actually supplies."""
    PARAM_BOUNDS={("ml","ml_threshold"):(.48,.80),("risk","min_trade_score"):(20.,95.),("risk","risk_per_trade_pct"):(.10,2.),("risk","max_position_pct"):(2.,35.),("risk","trailing_stop_atr_mult"):(0.,4.)}
    def __init__(self,settings:EvolutionSettings|None=None,seed:int=146)->None:
        self.settings=settings or EvolutionSettings(); self.rng=random.Random(seed); self.generation=0
        if self.settings.elite_count>=self.settings.max_population:raise ValueError("elite_count must be smaller than max_population")
    def score(self,e:ChildPolicyExecutor)->FitnessSnapshot:
        p=e.portfolio; n=int(p.trade_count); rr=float(p.realized_r); exp=rr/n if n else 0.; dd=float(p.get_drawdown_pct()); ret=((p.marked_equity/p.seed_capital)-1.)*100. if p.seed_capital else 0.; ok=e.status=="ACTIVE" and n>=self.settings.min_trades_for_selection and dd<=self.settings.max_drawdown_pct; fit=exp-.05*dd+.01*ret
        if not ok:fit=-1_000_000.+fit
        return FitnessSnapshot(e.policy_id,n,rr,exp,dd,ret,fit,ok)
    def rank(self,executors:Iterable[ChildPolicyExecutor])->list[FitnessSnapshot]:return sorted((self.score(e) for e in executors),key=lambda x:(x.fitness,x.trades,x.policy_id),reverse=True)
    def select_elites(self,executors:dict[str,ChildPolicyExecutor])->list[ChildPolicyExecutor]:return [executors[x.policy_id] for x in self.rank(executors.values()) if x.eligible][:self.settings.elite_count]
    def bootstrap_configs(self,now:datetime|None=None)->list[ChildPolicyConfig]:
        now=now or datetime.now(timezone.utc); seeds=[(.48,45.,.5,0.),(.50,50.,.7,.5),(.52,55.,.8,1.),(.55,60.,.6,1.5),(.58,65.,.5,2.),(.62,70.,.4,2.5)]; out=[]
        for i,(ml,score,risk,trail) in enumerate(seeds[:self.settings.bootstrap_population]):out.append(ChildPolicyConfig(policy_id=f"spawn_g0_seed_{i+1:02d}",parent_policy_id="champion",policy_version="g0",spawned_at=now,seed_capital=self.settings.seed_capital,execution_mode="SHADOW_ONLY",ml_overrides={"ml_threshold":ml},risk_overrides={"min_trade_score":score,"risk_per_trade_pct":risk,"max_position_pct":20.,"trailing_stop_atr_mult":trail},spawn_reason="BOOTSTRAP_DIVERSE_RISK_EXIT_COHORT"))
        return out
    @staticmethod
    def _copy(c):return dict(c.strategy_overrides),dict(c.ml_overrides),dict(c.meta_overrides),dict(c.risk_overrides)
    def _mutate_value(self,v,lo,hi):span=hi-lo; return min(hi,max(lo,float(v)+self.rng.uniform(-span*self.settings.mutation_fraction,span*self.settings.mutation_fraction)))
    def mutate(self,parent:ChildPolicyConfig,now:datetime|None=None)->ChildPolicyConfig:
        now=now or datetime.now(timezone.utc); strategy,ml,meta,risk=self._copy(parent); group,key=self.rng.choice(list(self.PARAM_BOUNDS)); target={"ml":ml,"risk":risk}[group]; lo,hi=self.PARAM_BOUNDS[(group,key)]; defaults={"ml_threshold":.48,"min_trade_score":50.,"risk_per_trade_pct":1.,"max_position_pct":25.,"trailing_stop_atr_mult":0.}; target[key]=round(self._mutate_value(target.get(key,defaults[key]),lo,hi),6); raw=f"{parent.policy_id}|{parent.compute_fingerprint()}|{self.generation}|{key}|{target[key]}|{now.isoformat()}"; cid=f"spawn_g{self.generation}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return ChildPolicyConfig(policy_id=cid,parent_policy_id=parent.policy_id,policy_version=f"g{self.generation}",spawned_at=now,seed_capital=min(parent.seed_capital,self.settings.seed_capital),execution_mode="SHADOW_ONLY",strategy_overrides=strategy,ml_overrides=ml,meta_overrides=meta,risk_overrides=risk,code_commit_sha=parent.code_commit_sha,model_manifest_sha=parent.model_manifest_sha,spawn_reason=f"EVOLUTION_MUTATION:{group}.{key}")
    def next_generation(self,executors,now=None):
        elites=self.select_elites(executors)
        if not elites:return []
        count=min(max(0,self.settings.max_population-sum(e.status=="ACTIVE" for e in executors.values())),self.settings.children_per_cycle)
        if not count:return []
        self.generation+=1; out=[]; seen={e.config_fingerprint for e in executors.values()}; attempts=0
        while len(out)<count and attempts<count*20:
            p=elites[attempts%len(elites)]; attempts+=1; c=self.mutate(p.config,now); fp=c.compute_fingerprint()
            if fp not in seen:seen.add(fp); out.append(c)
        return out
    def retirement_candidates(self,executors):
        scored=self.rank(executors.values()); sampled=[x for x in scored if x.trades>=self.settings.min_trades_for_selection]; weak=[x.policy_id for x in sampled if x.drawdown_pct>self.settings.max_drawdown_pct or x.expectancy_r<-.20]; overflow=max(0,sum(e.status=="ACTIVE" for e in executors.values())-self.settings.max_population)
        if overflow:weak.extend([x.policy_id for x in reversed(scored) if x.trades>=self.settings.min_trades_for_selection][:overflow])
        return list(dict.fromkeys(weak))
