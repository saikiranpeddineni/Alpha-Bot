"""Replay a chronological JSONL observation stream through the shadow spawn army.

Input rows are either:
  {"kind":"bar","ts":"...+00:00","symbol":"AAPL","open":...,"high":...,"low":...,"close":...}
  {"kind":"candidate", ...same candidate observation schema used by SpawnShadowCoordinator...}

This script is analysis-only: no broker, production engine, config, or model writes.
"""
from __future__ import annotations
import argparse, json
from datetime import datetime
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.research.spawn.spawn_evolution import EvolutionSettings, SpawnEvolutionEngine
from app.research.spawn.spawn_executor import ChildPolicyExecutor


def parse_ts(v):
    ts=datetime.fromisoformat(v) if isinstance(v,str) else v
    if not isinstance(ts,datetime) or ts.tzinfo is None:raise ValueError(f"timestamp must be timezone-aware: {v!r}")
    return ts

def load_rows(path:Path):
    rows=[]
    with path.open("r",encoding="utf-8") as fh:
        for n,line in enumerate(fh,1):
            if not line.strip():continue
            row=json.loads(line); row["ts"]=parse_ts(row.get("ts")); row["_line"]=n; rows.append(row)
    rows.sort(key=lambda r:(r["ts"],0 if r.get("kind")=="bar" else 1,r["_line"]))
    return rows

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("input",type=Path); ap.add_argument("--max-population",type=int,default=24); ap.add_argument("--evolve-every",type=int,default=250); ap.add_argument("--min-trades",type=int,default=12); ap.add_argument("--output",type=Path,default=Path("spawn_army_replay_report.json")); args=ap.parse_args()
    rows=load_rows(args.input)
    if not rows:raise SystemExit("No replay rows")
    settings=EvolutionSettings(max_population=args.max_population,elite_count=min(6,args.max_population-1),min_trades_for_selection=args.min_trades)
    evo=SpawnEvolutionEngine(settings); executors={c.policy_id:ChildPolicyExecutor(c) for c in evo.bootstrap_configs(rows[0]["ts"])}; candidates=0; bars=0; exits=0
    for row in rows:
        kind=row.get("kind")
        if kind=="bar":
            bars+=1; sym=str(row["symbol"]).upper(); o=float(row["open"]); h=float(row["high"]); l=float(row["low"]); c=float(row["close"]); ts=row["ts"]
            for ex in list(executors.values()):
                if ex.status!="ACTIVE":continue
                if sym in ex.portfolio.positions:
                    rec=ex.portfolio.update_ohlc_and_check_exits(sym,o,h,l,c,ts)
                    if rec:exits+=1
        elif kind=="candidate":
            candidates+=1
            for ex in list(executors.values()):ex.evaluate_observation(row)
            if candidates%max(1,args.evolve_every)==0:
                for pid in evo.retirement_candidates(executors):executors[pid].status="RETIRED"
                for cfg in evo.next_generation(executors,row["ts"]):executors[cfg.policy_id]=ChildPolicyExecutor(cfg)
        else:raise ValueError(f"Unknown kind on line {row['_line']}: {kind!r}")
    ranking=evo.rank(executors.values()); report={"input":str(args.input),"rows":len(rows),"bars":bars,"candidates":candidates,"virtual_exits":exits,"generation":evo.generation,"population":len(executors),"active":sum(e.status=="ACTIVE" for e in executors.values()),"ranking":[x.__dict__ for x in ranking]}
    args.output.write_text(json.dumps(report,indent=2),encoding="utf-8"); print(json.dumps(report,indent=2))
if __name__=="__main__":main()
