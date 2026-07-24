"""Stateful virtual portfolio for spawned child policies."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any
@dataclass
class VirtualPosition:
    symbol:str; side:str; qty:float; entry_price:float; entry_at:datetime; initial_stop:float; initial_target:float|None=None; trailing_stop:float|None=None; peak_price:float=0.; trough_price:float=0.; virtual_order_id:str=""
    def __post_init__(self):
        self.peak_price=self.peak_price or self.entry_price; self.trough_price=self.trough_price or self.entry_price
        if self.trailing_stop is None:self.trailing_stop=self.initial_stop
    @property
    def initial_risk_per_share(self):
        r=abs(self.entry_price-self.initial_stop); return r if r>0 else 1.
    def unrealized_pnl(self,p):return ((p-self.entry_price) if self.side=="long" else (self.entry_price-p))*self.qty
class SpawnVirtualPortfolio:
    MAX_DURABLE_WATERMARKS=10000
    def __init__(self,policy_id,seed_capital=10000.,risk_config=None):
        self.policy_id=policy_id; self.seed_capital=seed_capital; self.cash=seed_capital; self.realized_equity=seed_capital; self.marked_equity=seed_capital; self.peak_equity=seed_capital; self.risk_config=risk_config or {}; self.risk_per_trade_pct=float(self.risk_config.get("risk_per_trade_pct",1.)); self.max_position_pct=float(self.risk_config.get("max_position_pct",25.)); self.max_open_positions=int(self.risk_config.get("max_open_positions",2)); self.allow_fractional=bool(self.risk_config.get("allow_fractional",False)); self.positions={}; self.last_prices={}; self.pending_orders=[]; self.daily_pnl=0.; self.realized_r=0.; self.trade_count=0; self.closed_trades=[]; self.processed_watermarks={}
    def record_processed_watermark(self,key):
        self.processed_watermarks[key]=None; overflow=len(self.processed_watermarks)-self.MAX_DURABLE_WATERMARKS
        if overflow>0:
            for old in list(self.processed_watermarks)[:overflow]:self.processed_watermarks.pop(old,None)
    def mark_to_market(self,prices):
        if prices:self.last_prices.update(prices)
        self.marked_equity=self.cash+sum(p.unrealized_pnl(self.last_prices.get(s,p.entry_price)) for s,p in self.positions.items()); self.peak_equity=max(self.peak_equity,self.marked_equity); return self.marked_equity
    def get_drawdown_pct(self):return max(0.,(self.peak_equity-self.marked_equity)/self.peak_equity*100.) if self.peak_equity>0 else 0.
    def can_open_position(self,symbol):return symbol.upper() not in self.positions and len(self.positions)<self.max_open_positions
    def calculate_position_size(self,price,stop):
        risk=abs(price-stop)
        if price<=0 or risk<=0:return 0.
        raw=min(self.marked_equity*(self.risk_per_trade_pct/100.)/risk,self.marked_equity*(self.max_position_pct/100.)/price); return max(0.,round(raw,4)) if self.allow_fractional else (float(math.floor(raw)) if raw>=1 else 0.)
    def open_virtual_position(self,symbol,side,qty,price,stop,target,now,order_id=""):
        sym=symbol.upper()
        if not self.can_open_position(sym) or qty<=0:return None
        p=VirtualPosition(sym,side.lower(),qty,price,now,stop,target,virtual_order_id=order_id); self.positions[sym]=p; self.last_prices[sym]=price; self.mark_to_market({sym:price}); return p
    def update_ohlc_and_check_exits(self,symbol,o,h,l,c,now):
        sym=symbol.upper(); p=self.positions.get(sym)
        if not p:return None
        stop=p.trailing_stop if p.trailing_stop is not None else p.initial_stop
        if p.side=="long":
            if l<=stop:return self.close_virtual_position(sym,stop,"STOP_LOSS",now)
            if p.initial_target and h>=p.initial_target:return self.close_virtual_position(sym,p.initial_target,"TAKE_PROFIT",now)
        else:
            if h>=stop:return self.close_virtual_position(sym,stop,"STOP_LOSS",now)
            if p.initial_target and l<=p.initial_target:return self.close_virtual_position(sym,p.initial_target,"TAKE_PROFIT",now)
        p.peak_price=max(p.peak_price,h); p.trough_price=min(p.trough_price,l); mult=float(self.risk_config.get("trailing_stop_atr_mult",0.)); risk=p.initial_risk_per_share
        if mult>0 and p.side=="long" and p.peak_price>p.entry_price:p.trailing_stop=max(p.trailing_stop or p.initial_stop,p.peak_price-risk*mult)
        elif mult>0 and p.side=="short" and p.trough_price<p.entry_price:p.trailing_stop=min(p.trailing_stop or p.initial_stop,p.trough_price+risk*mult)
        self.mark_to_market({sym:c}); return None
    def close_virtual_position(self,symbol,exit_price,reason,now):
        sym=symbol.upper(); p=self.positions.pop(sym); risk=p.initial_risk_per_share; pnl=((exit_price-p.entry_price) if p.side=="long" else (p.entry_price-exit_price))*p.qty; rr=((exit_price-p.entry_price) if p.side=="long" else (p.entry_price-exit_price))/risk; mfe=((p.peak_price-p.entry_price) if p.side=="long" else (p.entry_price-p.trough_price))/risk; mae=((p.entry_price-p.trough_price) if p.side=="long" else (p.peak_price-p.entry_price))/risk; self.cash+=pnl; self.realized_equity+=pnl; self.daily_pnl+=pnl; self.realized_r+=rr; self.trade_count+=1; self.last_prices[sym]=exit_price; self.mark_to_market({sym:exit_price}); rec={"policy_id":self.policy_id,"symbol":sym,"side":p.side,"qty":p.qty,"entry_price":p.entry_price,"exit_price":exit_price,"entry_at":p.entry_at.isoformat(),"exit_at":now.isoformat(),"pnl":pnl,"realized_r":rr,"mfe_r":mfe,"mae_r":mae,"exit_reason":reason}; self.closed_trades.append(rec); return rec
    def to_checkpoint_dict(self):
        positions={s:{"symbol":p.symbol,"side":p.side,"qty":p.qty,"entry_price":p.entry_price,"entry_at":p.entry_at.isoformat(),"initial_stop":p.initial_stop,"initial_target":p.initial_target,"trailing_stop":p.trailing_stop,"peak_price":p.peak_price,"trough_price":p.trough_price,"virtual_order_id":p.virtual_order_id} for s,p in self.positions.items()}
        # Database already persists pending_orders_json. Use a versioned envelope to durably
        # carry research-only runtime state without adding/migrating production DB columns.
        envelope={"schema":2,"orders":self.pending_orders,"last_prices":dict(self.last_prices),"processed_watermarks":list(self.processed_watermarks)}
        return {"policy_id":self.policy_id,"cash":self.cash,"equity":self.marked_equity,"peak_equity":self.peak_equity,"positions":positions,"last_prices":dict(self.last_prices),"processed_watermarks":list(self.processed_watermarks),"pending_orders":envelope,"daily_pnl":self.daily_pnl,"realized_r":self.realized_r,"trade_count":self.trade_count,"drawdown":self.get_drawdown_pct()}
    def restore_from_checkpoint(self,checkpoint):
        self.cash=float(checkpoint.get("cash",self.seed_capital)); self.marked_equity=float(checkpoint.get("equity",self.seed_capital)); self.realized_equity=self.cash; self.peak_equity=float(checkpoint.get("peak_equity",self.marked_equity)); self.daily_pnl=float(checkpoint.get("daily_pnl",0)); self.realized_r=float(checkpoint.get("realized_r",0)); self.trade_count=int(checkpoint.get("trade_count",0)); envelope=checkpoint.get("pending_orders") or {}; durable=envelope if isinstance(envelope,dict) and envelope.get("schema")==2 else {}; self.pending_orders=list(durable.get("orders",[])) if durable else (envelope if isinstance(envelope,list) else []); prices=checkpoint.get("last_prices") or durable.get("last_prices") or {}; marks=checkpoint.get("processed_watermarks") or durable.get("processed_watermarks") or []; self.last_prices={str(k).upper():float(v) for k,v in prices.items()}; self.processed_watermarks={str(k):None for k in marks[-self.MAX_DURABLE_WATERMARKS:]}; restored={}
        for sym,p in (checkpoint.get("positions") or {}).items():
            raw=p.get("entry_at")
            if isinstance(raw,str):
                try:entry=datetime.fromisoformat(raw)
                except ValueError as exc:raise ValueError(f"Invalid checkpoint entry_at for {sym}: {raw!r}") from exc
            elif isinstance(raw,datetime):entry=raw
            else:raise ValueError(f"Missing checkpoint entry_at for {sym}")
            if entry.tzinfo is None:raise ValueError(f"Naive checkpoint entry_at for {sym}: {raw!r}")
            restored[sym.upper()]=VirtualPosition(sym.upper(),p.get("side","long"),float(p.get("qty",1)),float(p.get("entry_price",0)),entry,float(p.get("initial_stop",0)),p.get("initial_target"),p.get("trailing_stop"),float(p.get("peak_price",p.get("entry_price",0))),float(p.get("trough_price",p.get("entry_price",0))),p.get("virtual_order_id",""))
        self.positions=restored
        for sym,p in restored.items():self.last_prices.setdefault(sym,p.entry_price)
