"""TradingEngineEvaluationMixin mixin."""

from __future__ import annotations
import asyncio
import json
import uuid
from functools import partial
from datetime import datetime, timedelta, timezone, UTC
from typing import Any
import pandas as pd
from app.core.domain import Signal, SignalAction, Position, OrderRequest, OrderSide, OrderType, Order, OrderRole
from app.core.applog import applog
from app.core.shadow_ledger import ShadowLedger
from app.indicators.ta import enrich, compute_relative_strength
from app.indicators.vwap_telemetry import compute_vwap_telemetry
from app.indicators.slot_volume import SlotVolumeEngine
from app.ai.regime import detect_regime
from app.strategies import SignalPipeline, StrategyContext
from app.strategies.evaluator import ParallelEvidenceEvaluator
from app.strategies.entry_intelligence import assess_entry_context, assess_thesis_continuity
from app.pis.models import ExitAction

class TradingEngineEvaluationMixin:
    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime, effective_positions: list[Position] | None = None) -> list[tuple[Signal, pd.DataFrame]]:  # noqa: ANN001
        assert self.data and self.broker and self.risk and self.pipeline
        symbol = symbol.upper()
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

        # Structured news is fetched once per symbol evaluation and cached by the provider.
        # The same evidence is attached to every candidate signal for this symbol/tick.
        news_context = None
        news_sentiment = 0.0
        if hasattr(self.data, "get_news_context"):
            try:
                news_context = await self.data.get_news_context(symbol)
                news_sentiment = float(getattr(news_context, "sentiment", 0.0) or 0.0)
            except Exception as exc:
                applog.warning("engine.news", f"{symbol}: structured news context unavailable: {exc}")
        elif hasattr(self.data, "get_news_sentiment"):
            try:
                news_sentiment = float(await self.data.get_news_sentiment(symbol))
            except Exception as exc:
                applog.warning("engine.news", f"{symbol}: scalar news sentiment unavailable: {exc}")

        if position is not None:
            close = float(df["close"].iloc[-1])
            high_val = float(df["high"].iloc[-1])
            low_val = float(df["low"].iloc[-1])
            if symbol not in self._position_peaks:
                self._position_peaks[symbol] = position.avg_entry_price
                self._position_troughs[symbol] = position.avg_entry_price
                self._position_initial_stops[symbol] = position.stop_loss
            if getattr(self, "_position_current_stops", None) is not None and symbol not in self._position_current_stops:
                self._position_current_stops[symbol] = position.stop_loss

            pis_order = None
            pis_decision = None
            try:
                pis_order, pis_decision = await self.pis_adapter.evaluate_position_exit(
                    position=position,
                    bar_df=df,
                    initial_stop=self._position_initial_stops.get(symbol),
                    take_profit=self._position_take_profits.get(symbol),
                    current_stop=self._position_current_stops.get(symbol),
                    news_sentiment=news_sentiment,
                )
            except Exception as e:
                applog.warning("engine.exit", f"PIS global evaluation crashed for {symbol}: {e}")
                peak = self._position_peaks.get(symbol, position.avg_entry_price)
                trough = self._position_troughs.get(symbol, position.avg_entry_price)
                atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else (close * 0.02)
                if position.qty > 0 and close < peak - (3.0 * atr):
                    applog.error("engine.exit", f"Dumb Fallback triggered for {symbol}: -3.0 ATR trailing stop breached while PIS was offline.")
                    await self._close_position_guarded(symbol, reason="CRITICAL: PIS Offline - Fallback Trailing Stop", qty=abs(position.qty), strategy=position.strategy, role=OrderRole.EXIT)
                    return []
                elif position.qty < 0 and close > trough + (3.0 * atr):
                    applog.error("engine.exit", f"Dumb Fallback triggered for {symbol}: +3.0 ATR trailing stop breached while PIS was offline.")
                    await self._close_position_guarded(symbol, reason="CRITICAL: PIS Offline - Fallback Trailing Stop", qty=abs(position.qty), strategy=position.strategy, role=OrderRole.EXIT)
                    return []

            if pis_order is not None:
                try:
                    if pis_order.order_type == OrderType.STOP:
                        if pis_order.stop_price is not None:
                            broker = getattr(self, "broker", None)
                            if broker and hasattr(broker, "update_protection"):
                                order = await broker.update_protection(symbol, stop_loss=pis_order.stop_price, side=(OrderSide.BUY if position.qty > 0 else OrderSide.SELL), qty=abs(position.qty))
                                if order is not None:
                                    self._position_current_stops[symbol] = pis_order.stop_price
                    else:
                        applog.info("engine", f"{symbol}: PIS Unified Exit triggered: {pis_order.reason} (qty={pis_order.qty})")
                        order = await self._close_position_guarded(symbol, reason=pis_order.reason, qty=pis_order.qty, strategy=position.strategy, role=OrderRole.EXIT)
                        if order and getattr(pis_order, "metadata", None):
                            order.metadata.update(pis_order.metadata)
                        if (
                            pis_decision is not None
                            and getattr(pis_decision, "action", None) != "FULL_EXIT"
                            and getattr(pis_decision, "target_stop", None) is not None
                        ):
                            applog.debug("engine.exit", f"{symbol}: deferring runner protection replacement until exit fill reconciliation (requested_qty={pis_order.qty})")
                        if pis_decision is None or getattr(pis_decision, "action", None) == "FULL_EXIT" or pis_order.qty >= abs(position.qty) - 1e-6:
                            self._position_peaks.pop(symbol, None)
                            self._position_troughs.pop(symbol, None)
                            self._position_initial_stops.pop(symbol, None)
                            if hasattr(self, "_position_current_stops"):
                                self._position_current_stops.pop(symbol, None)
                            self._position_take_profits.pop(symbol, None)
                        return []
                except Exception as e:
                    if "ProtectionRecreationError" in str(type(e).__name__):
                        applog.error("engine.exit", f"CRITICAL: PROTECTION_DEGRADED for {symbol}. Forced reconciliation required: {e}")
                        if not hasattr(self, "_degraded_symbols"):
                            self._degraded_symbols = set()
                        self._degraded_symbols.add(symbol)
                        try:
                            await self.broker.close_position(symbol, reason="CRITICAL: PROTECTION_DEGRADED (Full Flatten)", strategy=position.strategy, role=OrderRole.EXIT)
                        except Exception as flatten_e:
                            applog.error("engine.exit", f"Failed to fully flatten degraded position {symbol}: {flatten_e}")
                    applog.error("engine.exit", f"Execution failed for PIS action on {symbol}: {e}")

            if position.qty > 0:
                self._position_peaks[symbol] = max(self._position_peaks[symbol], high_val)
                self._position_troughs[symbol] = min(self._position_troughs[symbol], low_val)
            else:
                self._position_peaks[symbol] = min(self._position_peaks[symbol], low_val)
                self._position_troughs[symbol] = max(self._position_troughs[symbol], high_val)
        else:
            self._position_peaks.pop(symbol, None)
            self._position_troughs.pop(symbol, None)
            self._position_initial_stops.pop(symbol, None)
            if hasattr(self, "_position_current_stops"):
                self._position_current_stops.pop(symbol, None)
            self._position_take_profits.pop(symbol, None)
            self.pis_adapter.handle_position_closed(symbol)

        _scanner = getattr(self, "_scanner", None) or getattr(self, "scanner", None)
        _mover_ctx = getattr(_scanner, "mover_context", {}) if _scanner else {}
        _mover_entry = _mover_ctx.get(symbol, {})
        _mover_bias = _mover_entry.get("direction")
        _mover_chg = float(_mover_entry.get("change_pct", 0.0))

        spy_df = None
        rs_score: float | None = None
        tick_id = getattr(self, "_tick_count", 0)
        cached_spy = getattr(self, "_spy_cache", None)
        if cached_spy is not None and cached_spy.get("tick") == tick_id:
            spy_df = cached_spy.get("df")
        else:
            try:
                spy_df = await asyncio.to_thread(enrich, self.data.frame("SPY", "1Min", limit=600))
                setattr(self, "_spy_cache", {"tick": tick_id, "df": spy_df})
            except Exception:
                spy_df = None
        try:
            rs_score = compute_relative_strength(df, spy_df) if spy_df is not None else None
            if rs_score is not None:
                applog.debug("engine", f"{symbol} RS vs SPY: rs_score={rs_score:.3f}")
        except Exception:
            rs_score = None

        news_dir = "neutral"
        news_conf = 0.0
        news_cat = None
        if news_context is not None:
            news_dir = str(getattr(news_context, "direction", "neutral") or "neutral")
            news_conf = float(getattr(news_context, "confidence", 0.0) or 0.0)
            news_cat = getattr(news_context, "catalyst", None)
            
        sctx = StrategyContext(
            position=position, equity=equity, regime=regime, now=now,
            mover_bias=_mover_bias, mover_change_pct=_mover_chg, rs_score=rs_score,
            news_direction=news_dir, news_confidence=news_conf, event_state=news_cat
        )
        signals = []
        for strat in self.strategies:
            if not strat.enabled:
                continue
            decision = getattr(self, "last_safety_decision", None)
            if decision is not None and strat.name not in decision.allowed_strategies:
                continue
            try:
                sig = strat.on_bar(sctx, symbol, df)
            except Exception as exc:
                applog.error("engine", f"strategy {strat.name} failed on {symbol}: {exc}")
                continue
            if sig is not None:
                sig.strategy_params = getattr(strat, "params", {})
                sig.regime = sig.regime or regime
                sig.mover_bias = sctx.mover_bias
                sig.mover_change_pct = sctx.mover_change_pct
                sig.rs_score = sctx.rs_score
                if news_context is not None:
                    sig.news_sentiment = float(getattr(news_context, "sentiment", 0.0) or 0.0)
                    sig.news_direction = str(getattr(news_context, "direction", "neutral") or "neutral")
                    sig.news_confidence = float(getattr(news_context, "confidence", 0.0) or 0.0)
                    sig.news_headline_count = int(getattr(news_context, "headline_count", 0) or 0)
                    sig.news_fresh_headline_count = int(getattr(news_context, "fresh_headline_count", 0) or 0)
                    sig.news_age_seconds = getattr(news_context, "newest_age_seconds", None)
                    sig.news_catalyst = getattr(news_context, "catalyst", None)
                    sig.news_context_available = sig.news_headline_count > 0
                signals.append(sig)
        if not signals:
            return []

        entry_signals = [sig for sig in signals if sig.action != SignalAction.EXIT]
        exit_signals = [sig for sig in signals if sig.action == SignalAction.EXIT]
        signals = exit_signals + entry_signals
        if not signals:
            return []

        has_exits = any(sig.action == SignalAction.EXIT for sig in signals)
        decision = getattr(self, "last_safety_decision", None)
        market_state = getattr(decision, "market_state", None) if decision else None
        if not has_exits and effective_positions is not None and getattr(self, "portfolio_gate", None) is not None:
            approved, reason = self.portfolio_gate.gate(symbol, effective_positions, market_state=market_state)
            if not approved:
                applog.info("portfolio.gate", f"{symbol} rejected pre-ML: {reason}")
                return []

        for sig in signals:
            if sig.action != SignalAction.EXIT and getattr(sig, "candidate_id", None) is None:
                prefix = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
                sig.candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()

        slot_rvol_val: float | None = None
        try:
            slot_engine = SlotVolumeEngine(df, lookback_sessions=20, min_observations=5)
            slot_rvol_val = slot_engine.get_slot_rvol(now)
        except Exception:
            slot_rvol_val = None

        spy_vol: float | None = None
        if spy_df is not None and not spy_df.empty:
            try:
                spy_vol = float(spy_df["close"].pct_change().rolling(30).std().iloc[-1] * 100)
            except Exception:
                spy_vol = None

        from app.strategies import get_contextual_expectancy, get_adaptive_ml_threshold, get_adaptive_signal_score
        from app.core.phase import classify_clock_phase
        clock_phase = classify_clock_phase(now)
        adaptive_ml_thresholds: dict[str, float] = {}
        adaptive_scores: dict[str, float] = {}
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
                    applog.info("engine.memory", f"Context memory lookup for {sig.symbol} {sig.strategy} {sig.action}: trades_count={exp_info['trades_count']}, expectancy={exp_info['expectancy']}, shrunk_expectancy={exp_info['shrunk_expectancy']} (source: {exp_info['source']})")
                    adaptive_ml_thresholds[sig.candidate_id] = get_adaptive_ml_threshold(self.pipeline.ml_threshold, exp_info, sctx)
                    if sig.score is not None:
                        adaptive_scores[sig.candidate_id] = get_adaptive_signal_score(sig.score, exp_info)
                except Exception as exc:
                    applog.error("engine.memory", f"Failed memory expectancy lookup / adaptation: {exc}")

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
            partial(self.pipeline.process, symbol, df, signals, regime, sector_strength=rs_score, adaptive_ml_thresholds=adaptive_ml_thresholds)
        )

        # Final contextual boundary: existing ML/Meta/Score decisions remain authoritative,
        # while a materially contradictory news/context thesis is prevented from reaching Risk.
        context_rejected = []
        context_survivors = []
        for sig in accepted:
            if sig.action == SignalAction.EXIT:
                context_survivors.append(sig)
                continue
            context = assess_entry_context(sig, df, regime)
            continuity = assess_thesis_continuity(self.db, sig, now)
            reasons = list(context.reasons)
            evidence = dict(context.evidence)
            if not continuity.allowed:
                reasons.extend(continuity.reasons)
                evidence.update(continuity.evidence)
                sig.thesis_continuity_blocked = True
            sig.context_conflict_count = len(reasons)
            sig.context_conflict_reasons = reasons
            if not context.allowed or not continuity.allowed:
                reason = "; ".join(reasons) or "entry_context_conflict"
                context_rejected.append((sig, reason))
                applog.info("engine.entry", f"{symbol} {sig.strategy} blocked at contextual boundary: {reason} | evidence={evidence}")
            else:
                context_survivors.append(sig)
        accepted = context_survivors
        rejected.extend(context_rejected)

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
                if "contextual boundary" in rejection_reason or "context_conflict" in rejection_reason or "thesis_continuity" in rejection_reason or "news_context_conflict" in rejection_reason:
                    rejection_stage = "entry_context"
                elif "ML confidence" in rejection_reason:
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
                alternative_exits=getattr(self.risk_config, "alternative_exits", None),
                market_regime=getattr(getattr(self, "last_safety_decision", None), "market_risk_state", "NORMAL"),
                vix_value=self._last_vix_value,
                vix_age=(now - self._last_vix_timestamp).total_seconds() if self._last_vix_timestamp else None,
                vix_feed_status=self._last_vix_status,
                regime_source="VIX",
            )

        for sig, reason in rejected:
            applog.debug("pipeline", f"{symbol} {sig.strategy} {sig.action}: rejected -- {reason}")

        candidate_entries = []
        for sig in accepted:
            if sig.action == SignalAction.EXIT:
                applog.debug("engine", f"{symbol}: Discarding legacy strategy EXIT signal for {sig.strategy}. PIS exclusively owns exits.")
                continue
            if getattr(self, "reconciliation_failed", False):
                applog.warning("engine", f"{symbol}: entry signal {sig.strategy} rejected -- engine position/broker reconciliation state is uncertain")
                continue
            candidate_entries.append((sig, df))
        return candidate_entries
