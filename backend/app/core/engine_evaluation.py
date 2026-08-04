"""TradingEngineEvaluationMixin mixin."""

from __future__ import annotations
import asyncio
import json
import uuid
from functools import partial
from datetime import datetime, timedelta, timezone, UTC
from typing import Any
import pandas as pd
from app.core.domain import Signal, SignalAction, Position, OrderRequest, OrderSide, OrderType, Order
from app.core.logging import applog
from app.core.shadow_ledger import ShadowLedger
from app.indicators.ta import enrich, compute_relative_strength
from app.indicators.vwap_telemetry import compute_vwap_telemetry
from app.indicators.slot_volume import SlotVolumeEngine
from app.ai.regime import detect_regime
from app.strategies import SignalPipeline, StrategyContext
from app.strategies.evaluator import ParallelEvidenceEvaluator

class TradingEngineEvaluationMixin:
    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime) -> list[tuple[Signal, pd.DataFrame]]:  # noqa: ANN001
        assert self.data and self.broker and self.risk and self.pipeline
        symbol = symbol.upper()
        # Reference volatility feeds are risk inputs only and must never enter the tradable pipeline.
        if symbol in {s.upper() for s in self._vix_symbols()}:
            applog.debug("engine", f"{symbol}: reference/risk symbol excluded from strategy evaluation")
            return []
        df = await asyncio.to_thread(enrich, self.data.frame(symbol, "1Min", limit=600))
        if df.empty or len(df) < 30:
            return []

        regime, details = await asyncio.to_thread(detect_regime, df)
        if self._regimes.get(symbol) != regime:
            self._regimes[symbol] = regime
            self.publish("insight", self.insights.on_regime_change(symbol, regime, details).model_dump(mode="json"))

        # Trailing stop update & check
        if position is not None:
            close = float(df["close"].iloc[-1])
            high_val = float(df["high"].iloc[-1])
            low_val = float(df["low"].iloc[-1])
            if symbol not in self._position_peaks:
                self._position_peaks[symbol] = position.avg_entry_price
                self._position_troughs[symbol] = position.avg_entry_price
                self._position_initial_stops[symbol] = position.stop_loss

            # Fetch live news sentiment if provider supports it
            news_sentiment = 0.0
            if hasattr(self.data, "get_news_sentiment"):
                news_sentiment = await self.data.get_news_sentiment(symbol)

            # Route all positions (winners and losers) through PIS for local safety-net evaluation
            try:
                pis_order, pis_decision = await self.pis_adapter.evaluate_position_exit(
                    position=position,
                    bar_df=df,
                    initial_stop=self._position_initial_stops.get(symbol),
                    take_profit=self._position_take_profits.get(symbol),
                    current_stop=None,
                    news_sentiment=news_sentiment,
                )
                if pis_order is not None:
                    applog.info("engine", f"{symbol}: PIS Unified Exit triggered: {pis_order.reason} (qty={pis_order.qty})")
                    await self._close_position_guarded(symbol, reason=pis_order.reason, qty=pis_order.qty)
                    if pis_decision is None or pis_decision.action == ExitAction.FULL_EXIT or pis_order.qty >= abs(position.qty) - 1e-6:
                        self._position_peaks.pop(symbol, None)
                        self._position_troughs.pop(symbol, None)
                        self._position_initial_stops.pop(symbol, None)
                        self._position_take_profits.pop(symbol, None)
                    return []
            except Exception as e:
                applog.warning("engine.exit", f"PIS global evaluation fallback for {symbol}: {e}")
                pis_decision = None

            runner_active = await self._manage_live_winner_exit(symbol, position, df, pis_decision)

            # All exits (including strategy take profit targets) are now handled exclusively by PIS
            # to guarantee PIS acts as the single source of truth for intelligent exits.

            if position.qty > 0:
                self._position_peaks[symbol] = max(self._position_peaks[symbol], high_val)
                self._position_troughs[symbol] = min(self._position_troughs[symbol], low_val)
            else:
                self._position_peaks[symbol] = min(self._position_peaks[symbol], low_val)
                self._position_troughs[symbol] = max(self._position_troughs[symbol], high_val)

            # Legacy local trailing stops removed to give PIS sole authority over exits
        else:
            if symbol in self._position_peaks:
                del self._position_peaks[symbol]
            if symbol in self._position_troughs:
                del self._position_troughs[symbol]
            if symbol in self._position_initial_stops:
                del self._position_initial_stops[symbol]
            self._position_take_profits.pop(symbol, None)
            self._live_runner_symbols.discard(symbol)
            self._live_runner_floor.pop(symbol, None)
            self._live_runner_floor.pop(symbol, None)
            self.pis_adapter.handle_position_closed(symbol)

        sctx = StrategyContext(position=position, equity=equity, regime=regime, now=now)
        signals = []
        for strat in self.strategies:
            if not strat.enabled:
                continue
            decision = getattr(self, "last_safety_decision", None)
            if decision is not None and strat.name not in decision.allowed_strategies:
                continue
            try:
                sig = strat.on_bar(sctx, symbol, df)
            except Exception as exc:  # noqa: BLE001
                applog.error("engine", f"strategy {strat.name} failed on {symbol}: {exc}")
                continue
            if sig is not None:
                sig.strategy_params = getattr(strat, "params", {})
                signals.append(sig)
        if not signals:
            return []

        # Feature 2: Relative Strength vs SPY — multi-timeframe (40% ret_5 + 60% ret_15)
        spy_df = None
        rs_score: float | None = None
        try:
            spy_df = await asyncio.to_thread(enrich, self.data.frame("SPY", "1Min", limit=600))
            rs_score = compute_relative_strength(df, spy_df)
            if rs_score is not None:
                applog.debug("engine", f"{symbol} RS vs SPY: rs_score={rs_score:.3f}")
        except Exception:
            rs_score = None  # graceful fallback — pipeline treats None as neutral 50

        # Assign a unique candidate ID to all candidate entry signals first (shared UUID)
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                if getattr(sig, "candidate_id", None) is None:
                    prefix = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
                    sig.candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()

        # Compute slot_rvol_val for the current symbol
        slot_rvol_val: float | None = None
        try:
            slot_engine = SlotVolumeEngine(df, lookback_sessions=20, min_observations=5)
            slot_rvol_val = slot_engine.get_slot_rvol(now)
        except Exception:
            slot_rvol_val = None

        # Estimate SPY 30-bar rolling volatility (standard deviation of returns)
        spy_vol: float | None = None
        if spy_df is not None and not spy_df.empty:
            try:
                spy_vol = float(spy_df["close"].pct_change().rolling(30).std().iloc[-1] * 100)
            except Exception:
                spy_vol = None

        # Fetch Level 1 Context expectancy for each strategy and log/adapt (CR-146 Phase 5)
        # SAFETY: adaptive_scores is a RESEARCH-ZONE side-dict. sig.score is NEVER mutated here.
        # The Champion's SignalPipeline always receives the original, unmodified sig object.
        from app.strategies import get_contextual_expectancy, get_adaptive_ml_threshold, get_adaptive_signal_score
        from app.core.phase import classify_clock_phase
        clock_phase = classify_clock_phase(now)
        adaptive_ml_thresholds: dict[str, float] = {}
        adaptive_scores: dict[str, float] = {}  # research-zone only — never written back to sig
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                try:
                    exp_info = get_contextual_expectancy(
                        db=self.db,
                        strategy=sig.strategy,
                        direction="long" if sig.action == SignalAction.ENTER_LONG else "short",
                        regime=regime.name if hasattr(regime, "name") else str(regime),
                        clock_phase=clock_phase.value if hasattr(clock_phase, "value") else str(clock_phase),
                        rvol=slot_rvol_val,
                        spy_vol=spy_vol,
                    )
                    applog.info(
                        "engine.memory",
                        f"Context memory lookup for {sig.symbol} {sig.strategy} {sig.action}: "
                        f"trades_count={exp_info['trades_count']}, expectancy={exp_info['expectancy']}, "
                        f"shrunk_expectancy={exp_info['shrunk_expectancy']} (source: {exp_info['source']})"
                    )

                    # Compute adaptive ML threshold (research-zone, fed only to ParallelEvidenceEvaluator)
                    dyn_ml_threshold = get_adaptive_ml_threshold(self.pipeline.ml_threshold, exp_info)
                    adaptive_ml_thresholds[sig.candidate_id] = dyn_ml_threshold

                    # Compute adaptive score (research-zone, stored in side-dict — NOT written back to sig)
                    if sig.score is not None:
                        adaptive_scores[sig.candidate_id] = get_adaptive_signal_score(sig.score, exp_info)
                except Exception as exc:
                    applog.error("engine.memory", f"Failed memory expectancy lookup / adaptation: {exc}")

        # Run ParallelEvidenceEvaluator out-of-band to capture the parallel evidence (CR-146)
        parallel_evidences = {}
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                try:
                    midday_active = getattr(self.pipeline, "_midday_active", False)
                    is_event_day = getattr(self.pipeline, "_is_event_day", False)
                    evidence = self.evidence_evaluator.evaluate(
                        symbol=symbol,
                        df=df,
                        sig=sig,
                        regime=regime,
                        sector_strength=rs_score,
                        is_event_day=is_event_day,
                        midday_active=midday_active,
                        ml_threshold=adaptive_ml_thresholds.get(sig.candidate_id),
                    )
                    if evidence:
                        parallel_evidences[sig.candidate_id] = evidence
                except Exception as exc:
                    applog.error("engine.evaluator", f"ParallelEvidenceEvaluator failed for {symbol} {sig.strategy}: {exc}")

        accepted, rejected = await asyncio.to_thread(
            partial(self.pipeline.process, symbol, df, signals, regime, sector_strength=rs_score)
        )
        
        # Save signals to shadow ledger
        for sig in signals:
            if sig.action == SignalAction.EXIT:
                continue
            is_accepted = sig in accepted
            final_decision = "pipeline_approved" if is_accepted else "rejected"
            rejection_stage = "none"
            rejection_reason = ""
            if not is_accepted:
                rejection_reason = "rejected"
                for rsig, rreason in rejected:
                    if rsig.strategy == sig.strategy and rsig.action == sig.action:
                        rejection_reason = rreason
                        break
                if "ML confidence" in rejection_reason:
                    rejection_stage = "ml_gate"
                elif "Trade Score" in rejection_reason:
                    rejection_stage = "trade_score_gate"
                elif "Meta-label" in rejection_reason:
                    rejection_stage = "meta_label_gate"
                elif "lost conflict" in rejection_reason:
                    rejection_stage = "conflict_resolution"
                else:
                    rejection_stage = "other"

            try:
                slot_engine = SlotVolumeEngine(df, lookback_sessions=20, min_observations=5)
                slot_rvol_val = slot_engine.get_slot_rvol(now)
            except Exception:
                slot_rvol_val = None

            try:
                vwap_tel = compute_vwap_telemetry(df, now, slot_rvol_val)
            except Exception:
                vwap_tel = {}

            ShadowLedger.record_candidate(
                db_engine=self.db,
                symbol=symbol,
                ts=now,
                strategy=sig.strategy,
                direction="long" if sig.action == SignalAction.ENTER_LONG else "short",
                signal_price=sig.price or float(df["close"].iloc[-1]),
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                ml_confidence=sig.ml_probability,
                final_decision=final_decision,
                rejection_stage=rejection_stage,
                rejection_reason=rejection_reason,
                df=df,
                spy_df=spy_df if spy_df is not None else pd.DataFrame(),
                slot_rvol_val=slot_rvol_val,
                vwap_telemetry=vwap_tel,
                mode=self.mode,
                candidate_id=sig.candidate_id,
                strategy_params=getattr(sig, "strategy_params", {}),
                policy_id="champion",
                parent_policy_id=None,
                policy_version="1.0.0",
                gate_evidence_json=parallel_evidences.get(sig.candidate_id),
                spy_vol=spy_vol,
                # Fix A5 (Issue #176): Pass runtime-tuned alternative exits from the engine.
                alternative_exits=getattr(self.risk_config, "alternative_exits", None),
                market_regime=getattr(getattr(self, "last_safety_decision", None), "market_risk_state", "NORMAL"),
                vix_value=self._last_vix_value,
                vix_age=(now - self._last_vix_timestamp).total_seconds() if self._last_vix_timestamp else None,
                vix_feed_status=self._last_vix_status,
                regime_source="VIX",
            )

            # Run and record all active Challengers (CR-146 Phase 3)
            # SAFETY: Each challenger receives an independent deep copy of the original signal
            # so Champion's sig object is never shared with or contaminated by Challenger paths.
            from app.strategies.challenger import get_active_challengers
            for challenger in get_active_challengers():
                try:
                    challenger_sig = sig.model_copy(deep=True)  # pristine deep copy for this challenger
                    challenger_eval = ParallelEvidenceEvaluator(
                        model=self.model,
                        ml_threshold=challenger.ml_threshold,
                        meta_label_engine=self.pipeline.meta_label_engine,
                        trade_score_engine=challenger.create_custom_trade_score_engine(self.pipeline.trade_score_engine),
                    )
                    c_evidence = challenger_eval.evaluate(
                        symbol=symbol,
                        df=df,
                        sig=challenger_sig,
                        regime=regime,
                        sector_strength=rs_score,
                        is_event_day=is_event_day,
                        midday_active=midday_active,
                    )
                    
                    c_vetoes = c_evidence.get("vetoes", {})
                    c_accepted = len(c_vetoes) == 0
                    c_final_decision = "pipeline_approved" if c_accepted else "rejected"
                    
                    c_rejection_stage = "none"
                    c_rejection_reason = ""
                    if not c_accepted:
                        stage = list(c_vetoes.keys())[0]
                        c_rejection_stage = stage
                        c_rejection_reason = c_vetoes[stage]

                    ShadowLedger.record_candidate(
                        db_engine=self.db,
                        symbol=symbol,
                        ts=now,
                        strategy=challenger_sig.strategy,
                        direction="long" if challenger_sig.action == SignalAction.ENTER_LONG else "short",
                        signal_price=challenger_sig.price or float(df["close"].iloc[-1]),
                        stop_loss=challenger_sig.stop_loss,
                        take_profit=challenger_sig.take_profit,
                        ml_confidence=challenger_sig.ml_probability,
                        final_decision=c_final_decision,
                        rejection_stage=c_rejection_stage,
                        rejection_reason=c_rejection_reason,
                        df=df,
                        spy_df=spy_df if spy_df is not None else pd.DataFrame(),
                        slot_rvol_val=slot_rvol_val,
                        vwap_telemetry=vwap_tel,
                        mode="challenger",
                        candidate_id=sig.candidate_id,
                        strategy_params=getattr(sig, "strategy_params", {}),
                        policy_id=challenger.policy_id,
                        parent_policy_id="champion",
                        policy_version="1.0.0",
                        gate_evidence_json=c_evidence,
                        spy_vol=spy_vol,
                        # Fix A5 (Issue #176): Runtime-tuned alternative exits for challenger replays.
                        alternative_exits=getattr(self.risk_config, "alternative_exits", None),
                        market_regime=getattr(self.last_safety_decision, "market_risk_state", "NORMAL"),
                        vix_value=self._last_vix_value,
                        vix_age=(now - self._last_vix_timestamp).total_seconds() if self._last_vix_timestamp else None,
                        vix_feed_status=self._last_vix_status,
                        regime_source="VIX",
                    )
                except Exception as exc:
                    applog.error("engine.challenger", f"Challenger {challenger.policy_id} failed for {symbol}: {exc}")

        _resurrection_enabled = getattr(self.broker, "is_paper", False) is True
        for sig, reason in rejected:
            applog.debug("pipeline", f"{symbol} {sig.strategy} {sig.action}: rejected -- {reason}")
            if (
                sig.action != SignalAction.EXIT
                and _resurrection_enabled
                and sig.symbol.upper() not in {s.upper() for s in self._vix_symbols()}
            ):
                in_queue = any(
                    q["symbol"] == sig.symbol and q["strategy"] == sig.strategy
                    for q in self.resurrection_queue
                )
                if not in_queue:
                    # Enforce defensive queue bound (e.g. max 50 items)
                    if len(self.resurrection_queue) >= 50:
                        applog.warning("engine.resurrection", "Resurrection queue is full (max 50). Discarding oldest intent.")
                        self.resurrection_queue.pop(0)

                    # Store resurrection INTENT only — not the full sig object.
                    # Stale price/SL/TP are intentionally excluded to prevent stale-data orders.
                    # When retried, the live pipeline will gate fresh market data through all gates.
                    self.resurrection_queue.append({
                        "symbol": sig.symbol,
                        "strategy": sig.strategy,
                        "direction": "long" if sig.action == SignalAction.ENTER_LONG else "short",
                        "candidate_id": sig.candidate_id,
                        "original_rejection_reason": reason,
                        "ts_rejected": now,
                        "retry_count": 0,
                    })
                    applog.info(
                        "engine.resurrection",
                        f"Added rejected candidate {sig.symbol} {sig.strategy} to resurrection queue (rejection: {reason})"
                    )

        candidate_entries = []
        for sig in accepted:
            if sig.action == SignalAction.EXIT:
                if position is not None:
                    if symbol in self._live_runner_symbols:
                        # Intentionally subordinate ordinary lagging strategy exits once
                        # a broker-confirmed profit floor owns the winner.  Rule-41, EOD,
                        # hard-halt and the broker stop remain authoritative and are not
                        # routed through this strategy-signal branch.
                        applog.info("engine.exit", f"{symbol}: runner preserving winner; ignored ordinary strategy exit -- {sig.reason}")
                    else:
                        applog.info("engine", f"{symbol}: strategy exit -- {sig.reason}")
                        await self._close_position_guarded(symbol, reason=sig.reason)
                continue
            
            if getattr(self, "reconciliation_failed", False):
                applog.warning("engine", f"{symbol}: entry signal {sig.strategy} rejected -- engine position/broker reconciliation state is uncertain")
                continue

            candidate_entries.append((sig, df))
        return candidate_entries

