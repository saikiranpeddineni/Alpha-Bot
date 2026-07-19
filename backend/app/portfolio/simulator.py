"""Virtual Portfolio Simulator.

Event-driven chronological portfolio tracker that models capital constraints,
buying-power, sector concentration, correlation, same-cycle capital competition,
slippage, commissions, and short-selling margin/borrow rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Any
import numpy as np
import pandas as pd

from app.core.logging import applog

# Sectors map matching allocator.py
_SECTOR_MAP: dict[str, str] = {
    "AAPL": "TECH", "MSFT": "TECH", "NVDA": "TECH", "AMD": "TECH",
    "INTC": "TECH", "META": "TECH", "GOOGL": "TECH", "GOOG": "TECH",
    "AMZN": "CONSUMER", "TSLA": "CONSUMER", "PLTR": "TECH", "UBER": "INDUSTRIAL",
    "NET": "TECH", "SNOW": "TECH", "CRM": "TECH", "NOW": "TECH",
    "BAC": "FINANCE", "JPM": "FINANCE", "GS": "FINANCE", "WFC": "FINANCE",
    "C": "FINANCE", "MS": "FINANCE", "XOM": "ENERGY", "CVX": "ENERGY",
    "COP": "ENERGY", "EOG": "ENERGY", "KO": "CONSUMER", "PEP": "CONSUMER",
    "WMT": "CONSUMER", "COST": "CONSUMER", "MCD": "CONSUMER", "NKE": "CONSUMER",
    "JNJ": "HEALTHCARE", "PFE": "HEALTHCARE", "MRK": "HEALTHCARE",
    "ABBV": "HEALTHCARE", "LLY": "HEALTHCARE", "UNH": "HEALTHCARE",
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "DIA": "ETF",
}


def _get_sector(symbol: str) -> str:
    sym = symbol.upper()
    return _SECTOR_MAP.get(sym, f"UNKNOWN:{sym}")


@dataclass
class SimulatedPosition:
    symbol: str
    direction: str  # "long" or "short"
    qty: float
    entry_price: float
    entry_timestamp: datetime
    exit_timestamp: datetime
    exit_price: float
    exit_reason: str
    strategy: str
    initial_risk_per_share: float
    exit_timestamp_inferred: bool = False
    margin_held: float = 0.0
    entry_slippage: float = 0.0
    entry_commission: float = 0.0


@dataclass
class SimulationConfig:
    starting_capital: float = 100000.0
    risk_per_trade_pct: float = 0.5
    max_position_pct: float = 20.0
    max_open_positions: int = 4
    max_positions_per_sector: int = 2
    max_correlation: float = 0.75
    slippage_bps: float = 3.0
    commission_per_share: float = 0.005
    fixed_commission_per_trade: float = 0.0
    
    # Strategy-level allocation limits
    max_allocation_per_strategy: dict[str, float] = field(default_factory=lambda: {
        "orb": 50000.0,
        "momentum": 50000.0,
        "vwap_reversion": 50000.0,
        "rsi2_reversion": 50000.0,
        "bb_squeeze": 50000.0,
    })
    
    # Short sale configuration (configurable, not hardcoded)
    short_margin_requirement: float = 1.50  # 150% total collateral needed (proceeds + 50%)
    borrow_fee_annual_pct: float = 2.0      # annual borrow fee
    borrow_available_symbols: set[str] | None = None  # None = all available


@dataclass
class SimulationResult:
    trades: list[dict]
    equity_curve: list[tuple[datetime, float]]
    daily_returns: list[float]
    metrics: dict[str, Any]


class VirtualPortfolioSimulator:
    def __init__(self, config: SimulationConfig | None = None) -> None:
        self.cfg = config or SimulationConfig()

    def run(
        self,
        signals: list[dict],
        correlation_matrix: dict[date, pd.DataFrame] | None = None,
    ) -> SimulationResult:
        """Run event-driven virtual portfolio simulation over shadow signals.
        
        Assumes signals are structured shadow signal dicts from SQLite ShadowLedgerRow.
        """
        if not signals:
            return SimulationResult([], [], [], self._empty_metrics())

        # Sort signals chronologically
        sorted_signals = sorted(
            signals,
            key=lambda s: datetime.fromisoformat(s["timestamp"]) if isinstance(s["timestamp"], str) else s["timestamp"]
        )

        # 1. Compile events (entries and exits)
        events = []
        for sig_idx, sig in enumerate(sorted_signals):
            entry_ts = datetime.fromisoformat(sig["timestamp"]) if isinstance(sig["timestamp"], str) else sig["timestamp"]
            
            # Fetch exit details from outcomes/alternative exits
            outcomes = sig.get("outcomes") or {}
            alt_exits = sig.get("alternative_exits") or {}
            
            # Use production exit if available in alternative_exits, otherwise close outcome
            exit_data = alt_exits.get("exit_production") or outcomes.get("close") or outcomes.get("60m")
            if not exit_data:
                continue

            # Detect actual exit timestamp from replay payload
            exit_ts_str = exit_data.get("exit_timestamp")
            exit_inferred = False
            if exit_ts_str:
                exit_ts = datetime.fromisoformat(exit_ts_str)
            else:
                # Fallback to inference, flagging provenance
                bars_held = exit_data.get("bars_held", 60)
                exit_ts = entry_ts + timedelta(minutes=bars_held)
                exit_inferred = True

            pnl_pct = float(exit_data.get("pnl_pct", 0.0))
            direction = sig.get("direction", "long").lower()
            entry_price = float(sig.get("entry_price") or sig.get("signal_price") or 100.0)
            
            # Reconstruct exit price
            if direction == "long":
                exit_price = entry_price * (1.0 + pnl_pct / 100.0)
            else:
                exit_price = entry_price * (1.0 - pnl_pct / 100.0)

            # Determine stop loss distance for sizing
            stop_loss = sig.get("stop_loss")
            if stop_loss is not None:
                stop_loss = float(stop_loss)
            
            # Form simulated trade payload
            trade_info = {
                "signal_index": sig_idx,
                "symbol": sig["symbol"].upper(),
                "direction": direction,
                "entry_timestamp": entry_ts,
                "exit_timestamp": exit_ts,
                "entry_price_raw": entry_price,
                "exit_price_raw": exit_price,
                "exit_reason": exit_data.get("exit_reason", "time"),
                "strategy": sig.get("strategy", "unknown"),
                "stop_loss": stop_loss,
                "take_profit": sig.get("take_profit"),
                "trade_score": float(sig.get("trade_score") or 0.0),
                "exit_timestamp_inferred": exit_inferred,
                "bars_held": exit_data.get("bars_held", 60),
                "raw_pnl_pct": pnl_pct
            }

            # Enqueue entry
            events.append({
                "time": entry_ts,
                "type": "entry",
                "trade": trade_info
            })
            
            # Enqueue exit
            events.append({
                "time": exit_ts,
                "type": "exit",
                "trade": trade_info
            })

        # Sort events. Exit must precede entry on same-timestamp to release cash/slots
        events.sort(key=lambda e: (e["time"], 0 if e["type"] == "exit" else 1))

        # 2. Chronological simulation loop
        cash = self.cfg.starting_capital
        equity = self.cfg.starting_capital
        peak_equity = self.cfg.starting_capital
        
        open_positions: dict[int, SimulatedPosition] = {}
        completed_trades: list[dict] = []
        equity_curve: list[tuple[datetime, float]] = []
        daily_equity: dict[date, float] = {}

        # Track initial watermark
        if events:
            equity_curve.append((events[0]["time"] - timedelta(seconds=1), self.cfg.starting_capital))

        # Group events by timestamp and type to handle same-cycle competition
        idx = 0
        while idx < len(events):
            current_time = events[idx]["time"]
            
            # Gather all events at this exact timestamp
            time_events = []
            while idx < len(events) and events[idx]["time"] == current_time:
                time_events.append(events[idx])
                idx += 1

            # Separate exits and entries at this timestamp
            exits = [e for e in time_events if e["type"] == "exit"]
            entries = [e for e in time_events if e["type"] == "entry"]

            # --- STEP 1: Process Exits ---
            for exit_evt in exits:
                sig_id = exit_evt["trade"]["signal_index"]
                if sig_id not in open_positions:
                    continue  # already exited or was not entered
                
                pos = open_positions.pop(sig_id)
                t_info = exit_evt["trade"]
                
                # Settle Trade
                # Slippage execution adjustment
                slippage_cost_exit = pos.qty * pos.exit_price * (self.cfg.slippage_bps / 10000.0)
                commission_exit = self.cfg.fixed_commission_per_trade + (self.cfg.commission_per_share * pos.qty)
                
                if pos.direction == "long":
                    gross_exit_val = pos.qty * pos.exit_price
                    realized_pnl = (gross_exit_val - slippage_cost_exit - commission_exit) - (pos.qty * pos.entry_price + pos.entry_slippage + pos.entry_commission)
                    cash += gross_exit_val - slippage_cost_exit - commission_exit
                else:  # short
                    # Close value = cost to buy back
                    buyback_cost = pos.qty * pos.exit_price
                    borrow_fee = self._calculate_borrow_fee(pos.qty * pos.entry_price, pos.entry_timestamp, pos.exit_timestamp)
                    realized_pnl = (pos.qty * pos.entry_price - pos.entry_slippage - pos.entry_commission) - buyback_cost - slippage_cost_exit - commission_exit - borrow_fee
                    
                    # Cash release: returned margin + net realized PnL
                    cash += pos.margin_held + realized_pnl
                
                # Record trade
                completed_trades.append({
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "strategy": pos.strategy,
                    "entry_time": pos.entry_timestamp,
                    "exit_time": pos.exit_timestamp,
                    "entry_price": pos.entry_price,
                    "exit_price": pos.exit_price,
                    "qty": pos.qty,
                    "realized_pnl": realized_pnl,
                    "r_multiple": realized_pnl / (pos.initial_risk_per_share * pos.qty) if pos.initial_risk_per_share > 0 else 0.0,
                    "exit_reason": pos.exit_reason,
                    "exit_timestamp_inferred": pos.exit_timestamp_inferred,
                })

            # --- STEP 2: Equity Update ---
            current_unrealized = 0.0
            for pos in open_positions.values():
                # For daily/tick marking, use raw exit price as market price proxy
                if pos.direction == "long":
                    current_unrealized += pos.qty * (pos.exit_price - pos.entry_price)
                else:
                    current_unrealized += pos.qty * (pos.entry_price - pos.exit_price)
            
            equity = cash + current_unrealized
            peak_equity = max(peak_equity, equity)
            daily_equity[current_time.date()] = equity

            # --- STEP 3: Rank Candidates (Same-timestamp competition) ---
            if entries:
                # Filter out candidates whose signals are already in open_positions (sanity)
                eligible_entries = [e for e in entries if e["trade"]["signal_index"] not in open_positions]
                
                # Sort by Trade Score descending, resolving ties alphabetically by symbol name
                eligible_entries.sort(key=lambda e: (-e["trade"]["trade_score"], e["trade"]["symbol"]))

                # --- STEP 4-7: Sizing & Entry Gating ---
                for entry_evt in eligible_entries:
                    t = entry_evt["trade"]
                    symbol = t["symbol"]
                    strategy = t["strategy"]
                    direction = t["direction"]
                    price = t["entry_price_raw"]
                    stop_loss = t["stop_loss"]

                    # Constraint 1: Concurrent Positions
                    if len(open_positions) >= self.cfg.max_open_positions:
                        continue

                    # Constraint 2: Sector Concentration
                    sector = _get_sector(symbol)
                    sector_count = sum(1 for p in open_positions.values() if _get_sector(p.symbol) == sector)
                    if sector_count >= self.cfg.max_positions_per_sector:
                        continue

                    # Constraint 3: Strategy Allocation limit
                    strategy_cap = self.cfg.max_allocation_per_strategy.get(strategy, 0.0)
                    active_strategy_capital = sum(p.qty * p.entry_price for p in open_positions.values() if p.strategy == strategy)

                    # Constraint 4: Borrow list for Shorts
                    if direction == "short" and self.cfg.borrow_available_symbols is not None:
                        if symbol not in self.cfg.borrow_available_symbols:
                            continue

                    # Constraint 5: Point-in-time correlation gate
                    if open_positions:
                        corr_ok = self._check_correlation_constraint(symbol, list(open_positions.values()), current_time.date(), correlation_matrix)
                        if not corr_ok:
                            continue

                    # Step 5: Sizing calculation
                    risk_distance = abs(price - stop_loss) if stop_loss is not None else (price * 0.02)
                    risk_distance = max(risk_distance, 1e-6)

                    # Risk sizing: risk % of current equity
                    risk_dollars = equity * (self.cfg.risk_per_trade_pct / 100.0)
                    qty_risk = risk_dollars / risk_distance
                    
                    # Position cap sizing
                    qty_cap = (equity * (self.cfg.max_position_pct / 100.0)) / price

                    # Available buying power/cash limits
                    slippage_factor = 1.0 + (self.cfg.slippage_bps / 10000.0)
                    estimated_commission = self.cfg.fixed_commission_per_trade
                    
                    if direction == "long":
                        # Max long shares = cash / (price * slippage)
                        qty_cash = max(0.0, (cash - estimated_commission) / (price * slippage_factor))
                        qty = min(qty_risk, qty_cap, qty_cash)
                        if qty <= 0 or (price * qty * slippage_factor) > strategy_cap - active_strategy_capital:
                            continue
                        
                        # Apply long trade entry
                        final_qty = qty
                        entry_val = final_qty * price
                        slippage_cost = entry_val * (self.cfg.slippage_bps / 10000.0)
                        comm = self.cfg.fixed_commission_per_trade + (self.cfg.commission_per_share * final_qty)
                        total_entry_cost = entry_val + slippage_cost + comm
                        
                        cash -= total_entry_cost
                        margin_held = 0.0
                    else:  # short
                        # Short margin size: requires cash collateral = short_margin_requirement * position_value
                        # Max short shares = cash / (price * short_margin_requirement)
                        qty_cash = max(0.0, cash / (price * self.cfg.short_margin_requirement))
                        qty = min(qty_risk, qty_cap, qty_cash)
                        if qty <= 0 or (price * qty) > strategy_cap - active_strategy_capital:
                            continue
                        
                        final_qty = qty
                        entry_val = final_qty * price
                        slippage_cost = entry_val * (self.cfg.slippage_bps / 10000.0)
                        comm = self.cfg.fixed_commission_per_trade + (self.cfg.commission_per_share * final_qty)
                        
                        # Collateral blocked as margin
                        margin_held = entry_val * self.cfg.short_margin_requirement
                        
                        # Deduct margin + slippage/commission, add proceeds from short sale
                        cash += entry_val - margin_held - slippage_cost - comm
                    
                    # Create simulated position
                    open_positions[t["signal_index"]] = SimulatedPosition(
                        symbol=symbol,
                        direction=direction,
                        qty=final_qty,
                        entry_price=price,
                        entry_timestamp=t["entry_timestamp"],
                        exit_timestamp=t["exit_timestamp"],
                        exit_price=t["exit_price_raw"],
                        exit_reason=t["exit_reason"],
                        strategy=strategy,
                        initial_risk_per_share=risk_distance,
                        exit_timestamp_inferred=t["exit_timestamp_inferred"],
                        margin_held=margin_held,
                        entry_slippage=slippage_cost,
                        entry_commission=comm
                    )

            # Record equity watermark
            equity_curve.append((current_time, equity))

        # Close any lingering open positions at the end of the simulation
        if open_positions:
            end_time = sorted_signals[-1]["timestamp"]
            if isinstance(end_time, str):
                end_time = datetime.fromisoformat(end_time)
            
            for sig_id, pos in list(open_positions.items()):
                slippage_cost_exit = pos.qty * pos.exit_price * (self.cfg.slippage_bps / 10000.0)
                commission_exit = self.cfg.fixed_commission_per_trade + (self.cfg.commission_per_share * pos.qty)
                
                if pos.direction == "long":
                    gross_exit_val = pos.qty * pos.exit_price
                    realized_pnl = (gross_exit_val - slippage_cost_exit - commission_exit) - (pos.qty * pos.entry_price + pos.entry_slippage + pos.entry_commission)
                    cash += gross_exit_val - slippage_cost_exit - commission_exit
                else:  # short
                    buyback_cost = pos.qty * pos.exit_price
                    borrow_fee = self._calculate_borrow_fee(pos.qty * pos.entry_price, pos.entry_timestamp, pos.exit_timestamp)
                    realized_pnl = (pos.qty * pos.entry_price - pos.entry_slippage - pos.entry_commission) - buyback_cost - slippage_cost_exit - commission_exit - borrow_fee
                    cash += pos.margin_held + realized_pnl
                
                completed_trades.append({
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "strategy": pos.strategy,
                    "entry_time": pos.entry_timestamp,
                    "exit_time": pos.exit_timestamp,
                    "entry_price": pos.entry_price,
                    "exit_price": pos.exit_price,
                    "qty": pos.qty,
                    "realized_pnl": realized_pnl,
                    "r_multiple": realized_pnl / (pos.initial_risk_per_share * pos.qty) if pos.initial_risk_per_share > 0 else 0.0,
                    "exit_reason": pos.exit_reason,
                    "exit_timestamp_inferred": pos.exit_timestamp_inferred,
                })
                del open_positions[sig_id]

            equity = cash
            equity_curve.append((end_time, equity))

        # Generate outputs
        curve_series = pd.Series([eq for _, eq in equity_curve], index=pd.DatetimeIndex([t for t, _ in equity_curve])).sort_index()
        daily_res = curve_series.resample("1D").last().ffill()
        daily_rets = daily_res.pct_change().dropna().tolist()

        metrics = self._calculate_metrics(curve_series, completed_trades, daily_rets)
        return SimulationResult(completed_trades, equity_curve, daily_rets, metrics)

    def _check_correlation_constraint(
        self,
        symbol: str,
        open_positions: list[SimulatedPosition],
        sim_date: date,
        correlation_matrix: dict[date, pd.DataFrame] | None,
    ) -> bool:
        if not correlation_matrix:
            return True
        
        # Get point-in-time correlation matrix
        target_date = sim_date
        if target_date not in correlation_matrix:
            # find closest prior date in dictionary keys
            prior_keys = [k for k in correlation_matrix.keys() if k <= target_date]
            if not prior_keys:
                return True
            target_date = max(prior_keys)

        matrix = correlation_matrix[target_date]
        sym = symbol.upper()
        if sym not in matrix.columns:
            return True

        for pos in open_positions:
            pos_sym = pos.symbol.upper()
            if pos_sym in matrix.index:
                corr = matrix.loc[pos_sym, sym]
                if not pd.isna(corr) and abs(float(corr)) > self.cfg.max_correlation:
                    return False
        return True

    def _calculate_borrow_fee(self, size: float, entry: datetime, exit: datetime) -> float:
        duration_days = (exit - entry).total_seconds() / 86400.0
        # borrow fee paid is size * borrow_fee_annual_pct * duration_days / 365
        fee = size * (self.cfg.borrow_fee_annual_pct / 100.0) * (duration_days / 365.0)
        return max(fee, 0.0)

    def _calculate_metrics(
        self,
        curve_series: pd.Series,
        trades: list[dict],
        daily_returns: list[float]
    ) -> dict[str, Any]:
        if curve_series.empty:
            return self._empty_metrics()

        starting_equity = self.cfg.starting_capital
        final_equity = float(curve_series.iloc[-1])
        total_pnl = final_equity - starting_equity
        total_return_pct = (total_pnl / starting_equity) * 100.0

        # Max Drawdown
        running_max = curve_series.cummax()
        drawdown = (curve_series - running_max) / running_max
        max_dd_pct = float(-drawdown.min() * 100.0) if not drawdown.empty else 0.0

        # R multiples
        r_values = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]

        # Profit Factor
        gross_profit = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] > 0)
        gross_loss = abs(sum(t["realized_pnl"] for t in trades if t["realized_pnl"] <= 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Win Rate
        wins = [t for t in trades if t["realized_pnl"] > 0]
        win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0

        # Sharpe / Sortino
        sharpe = None
        sortino = None
        if len(daily_returns) >= 2:
            std = float(np.std(daily_returns, ddof=0))
            mean = float(np.mean(daily_returns))
            if std > 0:
                sharpe = round(mean / std * math.sqrt(252), 2)
            
            downside_rets = [r for r in daily_returns if r < 0]
            dstd = float(np.std(downside_rets, ddof=0)) if len(downside_rets) > 1 else 0.0
            if dstd > 0:
                sortino = round(mean / dstd * math.sqrt(252), 2)

        # CVaR 95% (Conditional Value at Risk)
        cvar_95 = None
        if daily_returns:
            daily_returns_sorted = sorted(daily_returns)
            k = max(1, int(len(daily_returns_sorted) * 0.05))
            cvar_95 = float(np.mean(daily_returns_sorted[:k]))

        # Turnover
        total_traded_val = sum(t["qty"] * t["entry_price"] for t in trades)
        turnover = total_traded_val / starting_equity

        # Capital Utilization
        capital_utilization = 0.0
        if trades:
            peak_utilized = max((t["qty"] * t["entry_price"] for t in trades), default=0.0)
            capital_utilization = peak_utilized / starting_equity

        return {
            "starting_equity": round(starting_equity, 2),
            "final_equity": round(final_equity, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "total_trades": len(trades),
            "win_rate_pct": round(win_rate, 2),
            "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
            "avg_r": round(float(np.mean(r_values)), 3) if r_values else None,
            "sharpe": sharpe,
            "sortino": sortino,
            "cvar_95": round(cvar_95, 4) if cvar_95 is not None else None,
            "turnover": round(turnover, 3),
            "capital_utilization": round(capital_utilization, 3),
        }

    def _empty_metrics(self) -> dict[str, Any]:
        return {
            "starting_equity": self.cfg.starting_capital,
            "final_equity": self.cfg.starting_capital,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "total_trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": None,
            "avg_r": None,
            "sharpe": None,
            "sortino": None,
            "cvar_95": None,
            "turnover": 0.0,
            "capital_utilization": 0.0,
        }
