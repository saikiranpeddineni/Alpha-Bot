"""Stateful virtual portfolio for spawned child policies."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import math

@dataclass
class VirtualPosition:
    symbol:str; side:str; qty:float; entry_price:float; entry_at:datetime; initial_stop:float; initial_target:float|None=None; trailing_stop:float|None=None; peak_price:float=0.0; trough_price:float=0.0; virtual_order_id:str=""; strategy:str="unknown"
    def __post_init__(self):
        self.peak_price=self.peak_price or self.entry_price; self.trough_price=self.trough_price or self.entry_price
        if self.trailing_stop is None:self.trailing_stop=self.initial_stop
    @property
    def initial_risk_per_share(self):
        risk=abs(self.entry_price-self.initial_stop); return risk if risk>0 else 1.0
    def unrealized_pnl(self,price):return ((price-self.entry_price) if self.side=="long" else (self.entry_price-price))*self.qty
    @property
    def entry_notional(self):return abs(self.qty*self.entry_price)
    @property
    def initial_dollar_risk(self):return abs(self.qty*self.initial_risk_per_share)

class SpawnVirtualPortfolio:
    MAX_DURABLE_WATERMARKS=10000
    MAX_DURABLE_CLOSED_TRADES=5000
    def __init__(self,policy_id,seed_capital=10000.0,risk_config=None):
        self.policy_id=policy_id; self.seed_capital=seed_capital; self.cash=seed_capital; self.realized_equity=seed_capital; self.marked_equity=seed_capital; self.peak_equity=seed_capital; self.max_drawdown_pct=0.0; self.risk_config=risk_config or {}; self.risk_per_trade_pct=float(self.risk_config.get("risk_per_trade_pct",1.0)); self.max_position_pct=float(self.risk_config.get("max_position_pct",25.0)); self.max_open_positions=int(self.risk_config.get("max_open_positions",2)); self.allow_fractional=bool(self.risk_config.get("allow_fractional",False)); self.profit_reinvestment_fraction=min(1.0,max(0.0,float(self.risk_config.get("profit_reinvestment_fraction",0.0)))); self.positions={}; self.last_prices={}; self.pending_orders=[]; self.daily_pnl=0.0; self.realized_r=0.0; self.trade_count=0; self.closed_trades=[]; self.processed_watermarks={}; self.current_capital_deployed=0.0; self.peak_capital_deployed=0.0; self.cumulative_entry_notional=0.0; self.cumulative_initial_risk=0.0; self.realized_profit_reinvested=0.0
    @staticmethod
    def _positive_finite(value):
        try:value=float(value)
        except (TypeError,ValueError):return None
        return value if math.isfinite(value) and value>0.0 else None
    def record_processed_watermark(self,key):
        self.processed_watermarks[key]=None; overflow=len(self.processed_watermarks)-self.MAX_DURABLE_WATERMARKS
        if overflow>0:
            for old in list(self.processed_watermarks)[:overflow]:self.processed_watermarks.pop(old,None)
    def _refresh_capital_deployed(self):self.current_capital_deployed=sum(p.entry_notional for p in self.positions.values()); self.peak_capital_deployed=max(self.peak_capital_deployed,self.current_capital_deployed)
    def _sizing_equity(self):
        profit=max(0.0,self.realized_equity-self.seed_capital); self.realized_profit_reinvested=profit*self.profit_reinvestment_fraction; return self.seed_capital+self.realized_profit_reinvested
    def capital_efficiency_metrics(self):
        peak_pct=self.peak_capital_deployed/self.seed_capital*100 if self.seed_capital>0 else 0; ret=((self.marked_equity/self.seed_capital)-1)*100 if self.seed_capital>0 else 0; turnover=self.cumulative_entry_notional/max(self.peak_capital_deployed,1.0); risk_eff=self.daily_pnl/max(self.cumulative_initial_risk,1.0)
        return {"current_capital_deployed":self.current_capital_deployed,"peak_capital_deployed":self.peak_capital_deployed,"peak_capital_pct":peak_pct,"cumulative_entry_notional":self.cumulative_entry_notional,"cumulative_initial_risk":self.cumulative_initial_risk,"capital_turnover":turnover,"risk_efficiency":risk_eff,"return_pct":ret,"return_per_peak_capital_pct":ret/max(peak_pct,1.0),"pnl_per_peak_dollar":self.daily_pnl/max(self.peak_capital_deployed,1.0),"profit_reinvestment_fraction":self.profit_reinvestment_fraction,"realized_profit_reinvested":self.realized_profit_reinvested,"sizing_equity":self._sizing_equity()}
    def mark_to_market(self,prices):
        if prices:
            for symbol,value in prices.items():
                parsed=self._positive_finite(value)
                if parsed is not None:self.last_prices[str(symbol).upper()]=parsed
        self.marked_equity=self.cash+sum(p.unrealized_pnl(self.last_prices.get(s,p.entry_price)) for s,p in self.positions.items()); self.peak_equity=max(self.peak_equity,self.marked_equity); self.max_drawdown_pct=max(self.max_drawdown_pct,self.get_drawdown_pct()); return self.marked_equity
    def get_drawdown_pct(self):return max(0.0,(self.peak_equity-self.marked_equity)/self.peak_equity*100) if self.peak_equity>0 else 0.0
    def get_max_drawdown_pct(self):return self.max_drawdown_pct
    def can_open_position(self,symbol):return symbol.upper() not in self.positions and len(self.positions)<self.max_open_positions
    def calculate_position_size(self,price,stop):
        price=self._positive_finite(price); stop=self._positive_finite(stop)
        if price is None or stop is None:return 0.0
        risk=abs(price-stop)
        if risk<=0:return 0.0
        eq=self._sizing_equity(); raw=min(eq*(self.risk_per_trade_pct/100)/risk,eq*(self.max_position_pct/100)/price); return max(0.0,round(raw,4)) if self.allow_fractional else (float(math.floor(raw)) if raw>=1 else 0.0)
    def check_and_execute_rotation(self, candidate_obs, now, profiles=None):
        from app.research.spawn.spawn_opportunity_cost import check_rotation_opportunity
        res = check_rotation_opportunity(self.positions, self.last_prices, candidate_obs, now, profiles, self.risk_config)
        if res is None:return None
        weakest_pos, weakest_eval, incoming_eval, edge_delta = res
        raw_px = self.last_prices.get(weakest_pos.symbol)
        try:px=float(raw_px)
        except (TypeError,ValueError):return None
        if not math.isfinite(px) or px<=0.0:return None
        rec=self.close_virtual_position(weakest_pos.symbol,px,"OPPORTUNITY_ROTATION_EXIT",now)
        if rec:rec["rotation_telemetry"]={"incumbent_symbol":weakest_eval.symbol,"incoming_symbol":incoming_eval.symbol,"incumbent_score":weakest_eval.incumbent_score,"incoming_score":incoming_eval.incoming_score,"score_delta":edge_delta,"incumbent_current_r":weakest_eval.current_r,"capital_released":weakest_pos.entry_notional,"holding_minutes":weakest_eval.holding_mins,"rotation_reason":"OPPORTUNITY_ROTATION_EDGE","missing_incumbent_mark":False}
        return rec
    def open_virtual_position(self,symbol,side,qty,price,stop,target,now,order_id="",candidate_obs=None,profiles=None):
        sym=str(symbol).upper(); side=str(side).lower(); qty=self._positive_finite(qty); price=self._positive_finite(price); stop=self._positive_finite(stop); target=None if target is None else self._positive_finite(target)
        if not sym or side not in("long","short") or qty is None or price is None or stop is None or not isinstance(now,datetime) or now.tzinfo is None:return None
        if (side=="long" and stop>=price) or (side=="short" and stop<=price):return None
        if target is not None and ((side=="long" and target<=price) or (side=="short" and target>=price)):return None
        if not self.can_open_position(sym) and candidate_obs:self.check_and_execute_rotation(candidate_obs,now,profiles)
        if not self.can_open_position(sym):return None
        p=VirtualPosition(sym,side,qty,price,now,stop,target,virtual_order_id=order_id,strategy=str((candidate_obs or {}).get("strategy","unknown")).lower()); self.positions[sym]=p; self.last_prices[sym]=price; self.cumulative_entry_notional+=p.entry_notional; self.cumulative_initial_risk+=p.initial_dollar_risk; self._refresh_capital_deployed(); self.mark_to_market({sym:price}); return p
    def _stagnation_exit_price(self,position,close_px,now):
        minutes=float(self.risk_config.get("stagnation_exit_minutes",0.0)); max_abs_r=float(self.risk_config.get("stagnation_max_abs_r",0.0))
        if minutes<=0 or max_abs_r<=0 or not isinstance(now,datetime) or not isinstance(position.entry_at,datetime):return None
        elapsed=(now-position.entry_at).total_seconds()/60.0
        if elapsed<minutes:return None
        signed_r=((close_px-position.entry_price) if position.side=="long" else (position.entry_price-close_px))/position.initial_risk_per_share
        return close_px if abs(signed_r)<=max_abs_r else None
    def update_ohlc_and_check_exits(self,symbol,o=None,h=None,l=None,c=None,now=None,**legacy_kwargs):
        if o is None:o=legacy_kwargs.pop("open_px",None)
        if h is None:h=legacy_kwargs.pop("high_px",None)
        if l is None:l=legacy_kwargs.pop("low_px",None)
        if c is None:c=legacy_kwargs.pop("close_px",None)
        if now is None:now=legacy_kwargs.pop("ts",None)
        if legacy_kwargs:raise TypeError(f"Unexpected OHLC arguments: {sorted(legacy_kwargs)}")
        if any(v is None for v in(o,h,l,c,now)):raise ValueError("Complete OHLC values and timestamp are required")
        if not isinstance(now,datetime) or now.tzinfo is None:raise ValueError("OHLC timestamp must be timezone-aware")
        vals=[]
        for v in(o,h,l,c):
            x=self._positive_finite(v)
            if x is None:raise ValueError("OHLC values must be finite positive numbers")
            vals.append(x)
        o,h,l,c=vals
        if h<max(o,c,l) or l>min(o,c,h):raise ValueError("Invalid OHLC ordering")
        sym=symbol.upper(); p=self.positions.get(sym)
        if not p:return None
        if not isinstance(p.entry_at,datetime) or p.entry_at.tzinfo is None or now<p.entry_at:raise ValueError("Cannot apply a market bar before position entry")
        watermark=f"BAR|{sym}|{now.isoformat()}"
        if watermark in self.processed_watermarks:return None
        p.peak_price=max(p.peak_price,h); p.trough_price=min(p.trough_price,l)
        stop=p.trailing_stop if p.trailing_stop is not None else p.initial_stop; exit_record=None
        if p.side=="long":
            if o<=stop:exit_record=self.close_virtual_position(sym,o,"STOP_LOSS_GAP",now)
            elif l<=stop:exit_record=self.close_virtual_position(sym,stop,"STOP_LOSS",now)
            elif p.initial_target and o>=p.initial_target:exit_record=self.close_virtual_position(sym,o,"TAKE_PROFIT_GAP",now)
            elif p.initial_target and h>=p.initial_target:exit_record=self.close_virtual_position(sym,p.initial_target,"TAKE_PROFIT",now)
        else:
            if o>=stop:exit_record=self.close_virtual_position(sym,o,"STOP_LOSS_GAP",now)
            elif h>=stop:exit_record=self.close_virtual_position(sym,stop,"STOP_LOSS",now)
            elif p.initial_target and o<=p.initial_target:exit_record=self.close_virtual_position(sym,o,"TAKE_PROFIT_GAP",now)
            elif p.initial_target and l<=p.initial_target:exit_record=self.close_virtual_position(sym,p.initial_target,"TAKE_PROFIT",now)
        if exit_record is None:
            stagnation_price=self._stagnation_exit_price(p,c,now)
            if stagnation_price is not None:exit_record=self.close_virtual_position(sym,stagnation_price,"STAGNATION_EXIT",now)
        self.record_processed_watermark(watermark)
        if exit_record is not None:return exit_record
        mult=float(self.risk_config.get("trailing_stop_atr_mult",0.0)); risk=p.initial_risk_per_share
        if mult>0 and p.side=="long" and p.peak_price>p.entry_price:p.trailing_stop=max(p.trailing_stop or p.initial_stop,p.peak_price-risk*mult)
        elif mult>0 and p.side=="short" and p.trough_price<p.entry_price:p.trailing_stop=min(p.trailing_stop or p.initial_stop,p.trough_price+risk*mult)
        self.mark_to_market({sym:c}); return None
    def close_virtual_position(self,symbol,exit_price,reason,now):
        sym=str(symbol).upper(); exit_price=self._positive_finite(exit_price)
        if sym not in self.positions or exit_price is None or not isinstance(now,datetime) or now.tzinfo is None:return None
        p=self.positions[sym]
        if not isinstance(p.entry_at,datetime) or p.entry_at.tzinfo is None or now<p.entry_at:return None
        self.positions.pop(sym); risk=p.initial_risk_per_share; pnl=((exit_price-p.entry_price) if p.side=="long" else (p.entry_price-exit_price))*p.qty; rr=((exit_price-p.entry_price) if p.side=="long" else (p.entry_price-exit_price))/risk; mfe=((p.peak_price-p.entry_price) if p.side=="long" else (p.entry_price-p.trough_price))/risk; mae=((p.entry_price-p.trough_price) if p.side=="long" else (p.peak_price-p.entry_price))/risk; self.cash+=pnl; self.realized_equity+=pnl; self.daily_pnl+=pnl; self.realized_r+=rr; self.trade_count+=1; self.last_prices[sym]=exit_price; self._refresh_capital_deployed(); self.mark_to_market({sym:exit_price}); self._sizing_equity(); record={"policy_id":self.policy_id,"symbol":sym,"side":p.side,"strategy":p.strategy,"qty":p.qty,"entry_price":p.entry_price,"exit_price":exit_price,"entry_at":p.entry_at.isoformat(),"exit_at":now.isoformat(),"pnl":pnl,"realized_r":rr,"mfe_r":mfe,"mae_r":mae,"exit_reason":reason,"entry_notional":p.entry_notional,"initial_dollar_risk":p.initial_dollar_risk}; self.closed_trades.append(record); return record
    def to_checkpoint_dict(self):
        positions={s:{"symbol":p.symbol,"side":p.side,"strategy":p.strategy,"qty":p.qty,"entry_price":p.entry_price,"entry_at":p.entry_at.isoformat(),"initial_stop":p.initial_stop,"initial_target":p.initial_target,"trailing_stop":p.trailing_stop,"peak_price":p.peak_price,"trough_price":p.trough_price,"virtual_order_id":p.virtual_order_id} for s,p in self.positions.items()}; envelope={"schema":7,"orders":self.pending_orders,"last_prices":dict(self.last_prices),"processed_watermarks":list(self.processed_watermarks),"max_drawdown_pct":self.max_drawdown_pct,"capital_efficiency":self.capital_efficiency_metrics(),"closed_trades":list(self.closed_trades[-self.MAX_DURABLE_CLOSED_TRADES:])}; return {"policy_id":self.policy_id,"cash":self.cash,"equity":self.marked_equity,"peak_equity":self.peak_equity,"positions":positions,"last_prices":dict(self.last_prices),"processed_watermarks":list(self.processed_watermarks),"pending_orders":envelope,"daily_pnl":self.daily_pnl,"realized_r":self.realized_r,"trade_count":self.trade_count,"drawdown":self.get_drawdown_pct(),"max_drawdown_pct":self.max_drawdown_pct,"current_capital_deployed":self.current_capital_deployed,"peak_capital_deployed":self.peak_capital_deployed,"cumulative_entry_notional":self.cumulative_entry_notional,"cumulative_initial_risk":self.cumulative_initial_risk,"realized_profit_reinvested":self.realized_profit_reinvested,"closed_trades":list(self.closed_trades[-self.MAX_DURABLE_CLOSED_TRADES:])}
    def restore_from_checkpoint(self,checkpoint):
        self.cash=float(checkpoint.get("cash",self.seed_capital)); self.marked_equity=float(checkpoint.get("equity",self.seed_capital)); self.realized_equity=self.cash; self.peak_equity=float(checkpoint.get("peak_equity",self.marked_equity)); self.daily_pnl=float(checkpoint.get("daily_pnl",0)); self.realized_r=float(checkpoint.get("realized_r",0)); self.trade_count=int(checkpoint.get("trade_count",0)); self.max_drawdown_pct=float(checkpoint.get("max_drawdown_pct",checkpoint.get("drawdown",0.0))); self.current_capital_deployed=float(checkpoint.get("current_capital_deployed",0.0)); self.peak_capital_deployed=float(checkpoint.get("peak_capital_deployed",0.0)); self.cumulative_entry_notional=float(checkpoint.get("cumulative_entry_notional",0.0)); self.cumulative_initial_risk=float(checkpoint.get("cumulative_initial_risk",0.0)); self.realized_profit_reinvested=float(checkpoint.get("realized_profit_reinvested",0.0)); envelope=checkpoint.get("pending_orders") or {}
        if isinstance(envelope,dict):
            self.pending_orders=list(envelope.get("orders",[])); self.last_prices={str(k).upper():float(v) for k,v in(envelope.get("last_prices") or checkpoint.get("last_prices") or {}).items()}; watermarks=envelope.get("processed_watermarks") or checkpoint.get("processed_watermarks") or []; self.processed_watermarks={str(i):None for i in watermarks[-self.MAX_DURABLE_WATERMARKS:]}; ce=envelope.get("capital_efficiency") or {}; self.peak_capital_deployed=max(self.peak_capital_deployed,float(ce.get("peak_capital_deployed",0.0) or 0.0)); self.cumulative_entry_notional=max(self.cumulative_entry_notional,float(ce.get("cumulative_entry_notional",0.0) or 0.0)); self.cumulative_initial_risk=max(self.cumulative_initial_risk,float(ce.get("cumulative_initial_risk",0.0) or 0.0)); closed=envelope.get("closed_trades") or checkpoint.get("closed_trades") or []
        else:self.pending_orders=list(envelope); self.last_prices={str(k).upper():float(v) for k,v in(checkpoint.get("last_prices") or {}).items()}; self.processed_watermarks={str(i):None for i in(checkpoint.get("processed_watermarks") or [])}; self.peak_capital_deployed=max(self.peak_capital_deployed,float(checkpoint.get("peak_capital_deployed",0.0) or 0.0)); self.cumulative_entry_notional=max(self.cumulative_entry_notional,float(checkpoint.get("cumulative_entry_notional",0.0) or 0.0)); self.cumulative_initial_risk=max(self.cumulative_initial_risk,float(checkpoint.get("cumulative_initial_risk",0.0) or 0.0)); closed=checkpoint.get("closed_trades") or []
        self.closed_trades=[dict(t) for t in closed[-self.MAX_DURABLE_CLOSED_TRADES:] if isinstance(t,dict)]
        self.positions={}
        for s,d in(checkpoint.get("positions") or {}).items():
            p=VirtualPosition(d["symbol"],d["side"],d["qty"],d["entry_price"],datetime.fromisoformat(d["entry_at"]),d["initial_stop"],d.get("initial_target"),d.get("trailing_stop"),d.get("peak_price",d["entry_price"]),d.get("trough_price",d["entry_price"]),d.get("virtual_order_id",""),d.get("strategy","unknown")); self.positions[str(s).upper()]=p
        self._refresh_capital_deployed(); self.mark_to_market(self.last_prices)
