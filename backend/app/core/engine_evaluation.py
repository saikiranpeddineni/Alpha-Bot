"""AlphaDesk 007 production evaluation path.

Production deliberately has one entry decision:

    market data -> strategy -> ML confirmation -> candidate

Portfolio eligibility and sizing remain downstream authorities. PIS is the
only intelligent exit authority. Research systems (meta-label, adaptive
expectancy, parallel evidence, thesis memory and trade-score gating) are not
allowed to veto production entries.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, UTC
from typing import Any

import pandas as pd

from app.ai.features import latest_feature_row
from app.ai.model import _validate_signal_probability
from app.ai.regime import detect_regime
from app.core.applog import applog
from app.core.domain import OrderRole, OrderSide, OrderType, Position, Regime, SignalAction, Signal
from app.indicators.ta import compute_relative_strength, enrich
from app.pis.models import ExitAction
from app.strategies import StrategyContext


# Production policy: intentionally small and frozen. Research can propose new
# values, but it cannot silently alter a running production decision.
ENTRY_ML_THRESHOLD = 0.50
ENTRY_ML_HARD_FLOOR = 0.46
DEFAULT_STOP_ATR = 1.50
DEFAULT_TARGET_ATR = 2.00


class TradingEngineEvaluationMixin:
    """Small production decision surface used by live/paper execution."""

    async def _evaluate_symbol(
        self,
        symbol: str,
        position: Position | None,
        equity: float,
        now: datetime,
        effective_positions: list[Position] | None = None,
    ) -> list[tuple[Signal, pd.DataFrame]]:
        assert self.data and self.broker and self.risk
        symbol = symbol.upper()

        if symbol in {s.upper() for s in self._vix_symbols()}:
            return []

        # One market-data transformation for the symbol. No parallel evidence,
        # contextual-memory lookups or adaptive threshold calculations here.
        df = await asyncio.to_thread(enrich, self.data.frame(symbol, "1Min", limit=600))
        if df.empty or len(df) < 30:
            return []

        regime, details = await asyncio.to_thread(detect_regime, df)
        if self._regimes.get(symbol) != regime:
            self._regimes[symbol] = regime
            self.publish("insight", self.insights.on_regime_change(symbol, regime, details).model_dump(mode="json"))

        # Existing-position management is intentionally separate from entry.
        # PIS owns intelligent exits; strategy EXIT signals are ignored.
        news_context = None
        news_sentiment = 0.0
        try:
            if hasattr(self.data, "get_news_context"):
                news_context = await self.data.get_news_context(symbol)
                news_sentiment = float(getattr(news_context, "sentiment", 0.0) or 0.0)
            elif hasattr(self.data, "get_news_sentiment"):
                news_sentiment = float(await self.data.get_news_sentiment(symbol))
        except Exception as exc:
            applog.debug("engine.news", f"{symbol}: news unavailable: {exc}")

        if position is not None:
            await self._manage_position(symbol, position, df, news_sentiment, now)
            return []

        # No new entries when reconciliation is uncertain.
        if getattr(self, "reconciliation_failed", False):
            return []

        scanner = getattr(self, "_scanner", None) or getattr(self, "scanner", None)
        mover_context = getattr(scanner, "mover_context", {}) if scanner else {}
        mover = mover_context.get(symbol, {}) or {}
        mover_bias = mover.get("direction")
        mover_change = float(mover.get("change_pct", 0.0) or 0.0)

        # Relative strength is useful strategy context; it is not a second gate.
        rs_score = None
        try:
            spy_df = await asyncio.to_thread(enrich, self.data.frame("SPY", "1Min", limit=600))
            if spy_df is not None and not spy_df.empty:
                rs_score = compute_relative_strength(df, spy_df)
        except Exception:
            pass

        news_direction = str(getattr(news_context, "direction", "neutral") or "neutral") if news_context else "neutral"
        news_confidence = float(getattr(news_context, "confidence", 0.0) or 0.0) if news_context else 0.0
        news_catalyst = getattr(news_context, "catalyst", None) if news_context else None

        ctx = StrategyContext(
            position=None,
            equity=equity,
            regime=regime,
            now=now,
            mover_bias=mover_bias,
            mover_change_pct=mover_change,
            rs_score=rs_score,
            news_direction=news_direction,
            news_confidence=news_confidence,
            event_state=news_catalyst,
        )

        # Strategies are already restricted to the 007 production set by
        # create_strategies(). Safety/risk systems may still disable entries
        # globally, but they do not mutate the strategy definition.
        raw_signals: list[Signal] = []
        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            safety = getattr(self, "last_safety_decision", None)
            if safety is not None and strategy.name not in safety.allowed_strategies:
                continue
            try:
                sig = strategy.on_bar(ctx, symbol, df)
            except Exception as exc:
                applog.error("engine.strategy", f"{strategy.name} failed for {symbol}: {exc}")
                continue
            if sig is None or sig.action == SignalAction.EXIT:
                continue

            sig.strategy_params = getattr(strategy, "params", {}) or {}
            sig.regime = sig.regime or regime
            sig.mover_bias = mover_bias
            sig.mover_change_pct = mover_change
            sig.rs_score = rs_score
            if news_context is not None:
                sig.news_sentiment = float(getattr(news_context, "sentiment", 0.0) or 0.0)
                sig.news_direction = news_direction
                sig.news_confidence = news_confidence
                sig.news_catalyst = news_catalyst
            if getattr(sig, "candidate_id", None) is None:
                sig.candidate_id = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}".upper()
            raw_signals.append(sig)

        if not raw_signals:
            return []

        features = latest_feature_row(df)
        if features is None:
            return []

        accepted: list[tuple[Signal, pd.DataFrame]] = []
        for sig in raw_signals:
            action = sig.action
            direction = "short" if action == SignalAction.ENTER_SHORT else "long"
            regime_key = regime.name.lower() if hasattr(regime, "name") else str(regime).lower()

            try:
                result = self.model.score_action_detailed(features, action, symbol=symbol, regime_key=regime_key)
                probability = result.probability
            except Exception as exc:
                applog.error("engine.ml", f"{symbol}/{sig.strategy}: ML scoring failed: {exc}")
                continue

            if probability is None:
                sig.ml_gate_decision = "REJECT"
                applog.info("entry", f"{symbol} {sig.strategy}: REJECT ML unavailable")
                continue

            probability, validation_reason = _validate_signal_probability(probability)
            if validation_reason is not None or probability is None:
                sig.ml_gate_decision = "REJECT"
                continue

            sig.ml_probability = round(probability, 4)
            # Keep a simple comparable score for ranking/telemetry. It is not a
            # second approval gate.
            sig.score = round(probability * 100.0, 2)
            sig.ml_gate_decision = "PASS" if probability >= ENTRY_ML_THRESHOLD else "REVIEW"

            # A narrow gray zone exists only for high-quality momentum setups.
            # This preserves strong momentum trades without reopening the old
            # multi-gate architecture.
            quality = 0.0
            try:
                quality = float(sig.strategy_params.get("entry_quality_score", 0.0) or 0.0)
            except Exception:
                quality = 0.0
            momentum_exception = (
                sig.strategy == "momentum"
                and probability >= ENTRY_ML_HARD_FLOOR
                and quality >= 80.0
            )
            if probability < ENTRY_ML_THRESHOLD and not momentum_exception:
                sig.ml_gate_decision = "REJECT"
                applog.info(
                    "entry",
                    f"{symbol} {sig.strategy}: REJECT ML {probability:.3f} < {ENTRY_ML_THRESHOLD:.2f}",
                )
                continue

            # Structural trade geometry comes from the strategy. If a strategy
            # omitted it, derive a transparent ATR-based fallback rather than
            # invoking another prediction model.
            entry = float(sig.price or df["close"].iloc[-1])
            atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else entry * 0.01
            atr = max(atr, entry * 0.002)
            if sig.stop_loss is None:
                sig.stop_loss = round(entry - DEFAULT_STOP_ATR * atr if direction == "long" else entry + DEFAULT_STOP_ATR * atr, 4)
            if sig.take_profit is None:
                sig.take_profit = round(entry + DEFAULT_TARGET_ATR * atr if direction == "long" else entry - DEFAULT_TARGET_ATR * atr, 4)

            # Reject only malformed trade geometry. This is an execution safety
            # invariant, not a prediction gate.
            if direction == "long" and not (sig.stop_loss < entry < sig.take_profit):
                applog.info("entry", f"{symbol} {sig.strategy}: REJECT invalid long geometry")
                continue
            if direction == "short" and not (sig.take_profit < entry < sig.stop_loss):
                applog.info("entry", f"{symbol} {sig.strategy}: REJECT invalid short geometry")
                continue

            sig.entry_decision = "ACCEPT"
            sig.entry_decision_reason = (
                f"{sig.strategy}: ML={probability:.3f}; "
                f"regime={getattr(regime, 'name', regime)}"
            )
            accepted.append((sig, df))
            applog.info(
                "entry",
                f"{symbol} {sig.strategy}: ACCEPT {direction} ML={probability:.3f} "
                f"entry={entry:.2f} stop={sig.stop_loss:.2f} target={sig.take_profit:.2f}",
            )

        # One symbol -> one candidate. Prefer the strongest probability rather
        # than letting strategy iteration order decide which signal wins.
        accepted.sort(key=lambda item: float(item[0].ml_probability or 0.0), reverse=True)
        return accepted[:1]

    async def _manage_position(
        self,
        symbol: str,
        position: Position,
        df: pd.DataFrame,
        news_sentiment: float,
        now: datetime,
    ) -> None:
        """Run PIS only; strategy entries never manage exits."""
        close = float(df["close"].iloc[-1])
        high_val = float(df["high"].iloc[-1])
        low_val = float(df["low"].iloc[-1])

        if symbol not in self._position_peaks:
            self._position_peaks[symbol] = position.avg_entry_price
            self._position_troughs[symbol] = position.avg_entry_price
            self._position_initial_stops[symbol] = position.stop_loss
            self._position_current_stops[symbol] = position.stop_loss

        try:
            pis_order, pis_decision = await self.pis_adapter.evaluate_position_exit(
                position=position,
                bar_df=df,
                initial_stop=self._position_initial_stops.get(symbol),
                take_profit=self._position_take_profits.get(symbol),
                current_stop=self._position_current_stops.get(symbol),
                news_sentiment=news_sentiment,
            )
        except Exception as exc:
            applog.error("engine.exit", f"PIS failed for {symbol}: {exc}")
            # Hard safety fallback only. This is deliberately dumb and cannot
            # generate entries or alter the normal PIS path.
            atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else close * 0.02
            peak = self._position_peaks.get(symbol, position.avg_entry_price)
            trough = self._position_troughs.get(symbol, position.avg_entry_price)
            if position.qty > 0 and close < peak - 3.0 * atr:
                await self._close_position_guarded(symbol, "PIS unavailable - emergency trailing stop", abs(position.qty), position.strategy, OrderRole.EXIT)
            elif position.qty < 0 and close > trough + 3.0 * atr:
                await self._close_position_guarded(symbol, "PIS unavailable - emergency trailing stop", abs(position.qty), position.strategy, OrderRole.EXIT)
            return

        if pis_order is not None:
            try:
                if pis_order.order_type == OrderType.STOP and pis_order.stop_price is not None:
                    broker = getattr(self, "broker", None)
                    if broker and hasattr(broker, "update_protection"):
                        updated = await broker.update_protection(
                            symbol,
                            stop_loss=pis_order.stop_price,
                            side=OrderSide.BUY if position.qty > 0 else OrderSide.SELL,
                            qty=abs(position.qty),
                        )
                        if updated is not None:
                            self._position_current_stops[symbol] = pis_order.stop_price
                else:
                    await self._close_position_guarded(
                        symbol,
                        reason=pis_order.reason,
                        qty=pis_order.qty,
                        strategy=position.strategy,
                        role=OrderRole.EXIT,
                    )
                    if pis_decision is None or getattr(pis_decision, "action", None) == "FULL_EXIT" or pis_order.qty >= abs(position.qty) - 1e-6:
                        self._position_peaks.pop(symbol, None)
                        self._position_troughs.pop(symbol, None)
                        self._position_initial_stops.pop(symbol, None)
                        self._position_current_stops.pop(symbol, None)
                        self._position_take_profits.pop(symbol, None)
                    return
            except Exception as exc:
                applog.error("engine.exit", f"PIS action execution failed for {symbol}: {exc}")

        if position.qty > 0:
            self._position_peaks[symbol] = max(self._position_peaks[symbol], high_val)
            self._position_troughs[symbol] = min(self._position_troughs[symbol], low_val)
        else:
            self._position_peaks[symbol] = min(self._position_peaks[symbol], low_val)
            self._position_troughs[symbol] = max(self._position_troughs[symbol], high_val)
