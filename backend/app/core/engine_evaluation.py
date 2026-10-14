"""TradingEngineEvaluationMixin mixin."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from functools import partial

import pandas as pd

from app.ai.regime import detect_regime
from app.core.applog import applog
from app.core.domain import OrderStatus, Position, Signal, SignalAction
from app.indicators.ta import compute_relative_strength, enrich
from app.strategies import StrategyContext


class TradingEngineEvaluationMixin:
    async def _daily_sma20(self, symbol: str) -> float | None:
        """SMA20 of daily closes, cached per (symbol, session).

        Multi-day drift anchor for strategy filters. Fails soft to None --
        filters that consume it must treat None as 'filter unavailable'.
        """
        cache = getattr(self, "_daily_sma20_cache", None)
        if cache is None:
            cache = {}
            self._daily_sma20_cache = cache
        key = symbol.upper()
        hit = cache.get(key)
        if hit is not None:
            return hit
        try:
            from datetime import timedelta as _td, UTC as _UTC, datetime as _dt
            end = _dt.now(_UTC)
            daily = await self.data.history(key, "1Day", end - _td(days=60), end)
            if daily is None or len(daily) < 20 or "close" not in daily.columns:
                # Cache the miss too: one probe per symbol per session, no hammering.
                cache[key] = 0.0
                return None
            sma = round(float(daily["close"].iloc[-20:].mean()), 4)
            cache[key] = sma
            return sma
        except Exception:
            cache[key] = 0.0
            return None

    async def _evaluate_symbol(self, symbol: str, position, equity: float, now: datetime, effective_positions: list[Position] | None = None) -> list[tuple[Signal, pd.DataFrame]]:  # noqa: ANN001
        assert self.data and self.broker and self.risk and self.pipeline
        symbol = symbol.upper()
        # Reference volatility feeds are risk inputs only and must never enter the tradable pipeline.
        if symbol in {s.upper() for s in self._vix_symbols()}:
            applog.debug("engine", f"{symbol}: reference/risk symbol excluded from strategy evaluation")
            return []
        df = await asyncio.to_thread(enrich, self.data.frame(symbol, "1Min", limit=600))

        # Warmup backfill MUST run before any short-frame early-return: newly
        # scanned symbols start with empty/30-bar tails and would otherwise
        # starve below the feature threshold for hours -- ML never runs for
        # them and every candidate dies as 'feature row unavailable (warmup)'.
        # =============================================================================
        # [MODIFIED 2026-10-13]: Guard warmup backfill against replay provider & missing backfill
        # ORIGINAL INTENT: Guard excluded 'synthetic' provider from triggering live backfills.
        # REASON FOR CHANGE: In replay mode, provider is 'replay' and data is immutable.
        #                    Spawning asynchronous backfill tasks during replay attempts to
        #                    query live network providers or hits missing attributes.
        # SAFE ROLLBACK: Live trading continues to trigger backfill for non-synthetic/non-replay
        #                providers when hasattr(self.data, "backfill") is True.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if len(df) < 120 and not getattr(getattr(self.data, "provider", None), "name", "") == "synthetic":
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if (
            len(df) < 120
            and getattr(getattr(self.data, "provider", None), "name", "") not in ("synthetic", "replay")
            and hasattr(self.data, "backfill")
        ):
        # =============================================================================
            self._backfill_inflight = getattr(self, "_backfill_inflight", set())
            self._backfill_last_try = getattr(self, "_backfill_last_try", {})
            import time as _time

            _now_mono = _time.monotonic()
            if (
                symbol not in self._backfill_inflight
                and _now_mono - self._backfill_last_try.get(symbol, 0.0) > 600.0
            ):
                self._backfill_inflight.add(symbol)
                self._backfill_last_try[symbol] = _now_mono

                async def _do_backfill(sym: str = symbol) -> None:
                    try:
                        await self.data.backfill(sym)
                    finally:
                        self._backfill_inflight.discard(sym)

                applog.info("engine", f"{symbol}: thin frame ({len(df)} bars) -- scheduling warmup backfill")
                asyncio.create_task(_do_backfill())

        if df.empty or len(df) < 30:
            return []

        # ---- decision-input sanity gates (entries only; PIS exits below are
        # protective and must always run) ----
        # 1. Post-close gate: strategies reason about df.iloc[-1] as "now".
        #    After the bell the rolling frame freezes, yet strategy loops kept
        #    re-firing the same breakout candidate every tick (observed
        #    2026-08-24: phantom NVDA orb candidates at 16:57/17:26 ET).
        # 2. Stale-bar gate: a provider glitch can leave an ancient/alien bar
        #    as the tail row (same incident: NVDA priced 128.92 while trading
        #    ~210). Entries on such bars are garbage by construction.
        skip_entries_reason: str | None = None
        if position is None and not getattr(self.clock, "always_open", False):
            try:
                if not self.clock.is_open(now):
                    skip_entries_reason = "market closed"
            except Exception:
                pass
        if position is None and not getattr(self.clock, "always_open", False) and not getattr(self, "replay_fast_path", False):
            try:
                import os as _os

                _max_age_min = float(_os.environ.get("ALPHADESK_MAX_DECISION_BAR_AGE_MIN", "15"))
                _last_ts = df.index[-1]
                if getattr(_last_ts, "tzinfo", None) is None:
                    _last_ts = _last_ts.tz_localize("UTC")
                else:
                    _last_ts = _last_ts.tz_convert("UTC")
                _now = now if now.tzinfo else now.replace(tzinfo=UTC)
                _age_min = (_now - _last_ts.to_pydatetime()).total_seconds() / 60.0
                if _age_min > _max_age_min:
                    skip_entries_reason = (
                        f"decision bar stale ({_age_min:.0f}min > {_max_age_min:.0f}min)"
                    )
            except Exception:
                pass
        if position is None and skip_entries_reason and not getattr(self, "replay_fast_path", False):
            if symbol not in getattr(self, "_entry_gate_warned", set()):
                self._entry_gate_warned.add(symbol)
                applog.info("engine", f"{symbol}: entry evaluation skipped -- {skip_entries_reason}")
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
            _lot = getattr(self.portfolio, "_lots", {}).get(symbol)
            if symbol not in self._position_peaks:
                self._position_peaks[symbol] = position.avg_entry_price
                self._position_troughs[symbol] = position.avg_entry_price
                # --- PREVIOUS IMPLEMENTATION (Preserved for historical intent & audit) ---
                # self._position_initial_stops[symbol] = position.stop_loss
                # --------------------------------------------------------------------------
                # --- NEW IMPLEMENTATION ---
                # Broker Position objects (e.g. from Alpaca) do not expose a native stop_loss attribute.
                # Query the portfolio tracker's internal lot memory (_lot.initial_stop_loss or _lot.stop_loss)
                # to obtain the true initial risk stop rather than having PIS fall back to sub-noise ATR defaults.
                _init_stop = getattr(_lot, "initial_stop_loss", None) or getattr(_lot, "stop_loss", None) if _lot is not None else None
                self._position_initial_stops[symbol] = _init_stop if _init_stop is not None else getattr(position, "stop_loss", None)
            elif self._position_initial_stops.get(symbol) is None and _lot is not None:
                _init_stop = getattr(_lot, "initial_stop_loss", None) or getattr(_lot, "stop_loss", None)
                if _init_stop is not None:
                    self._position_initial_stops[symbol] = _init_stop

            # Track excursion memory FIRST so the bar that triggers an exit is
            # itself included. Trade records derive mfe/mae from these maps at
            # fill-drain time; updating after the PIS block used to drop the
            # exit bar and, worse, the PIS-exit path below used to delete the
            # maps before the closing fill ever arrived -- corrupting every
            # exited trade's mfe/mae into a copy of its final move.
            if position.qty > 0:
                self._position_peaks[symbol] = max(self._position_peaks[symbol], high_val)
                self._position_troughs[symbol] = min(self._position_troughs[symbol], low_val)
            else:
                self._position_peaks[symbol] = min(self._position_peaks[symbol], low_val)
                self._position_troughs[symbol] = max(self._position_troughs[symbol], high_val)

            # Fetch live news sentiment if provider supports it
            news_sentiment = 0.0
            if hasattr(self.data, "get_news_sentiment"):
                news_sentiment = await self.data.get_news_sentiment(symbol)

            # Route all positions (winners and losers) through PIS for local safety-net evaluation
            pis_order = None
            pis_decision = None
            # Feed PIS the position's REAL protective stop so its protection
            # floors reason about broker truth instead of a virtual world.
            _lot = getattr(self.portfolio, "_lots", {}).get(symbol)
            _current_stop = getattr(_lot, "stop_loss", None) if _lot is not None else None
            try:
                pis_order, pis_decision = await self.pis_adapter.evaluate_position_exit(
                    position=position,
                    bar_df=df,
                    initial_stop=self._position_initial_stops.get(symbol),
                    take_profit=self._position_take_profits.get(symbol),
                    current_stop=_current_stop,
                    timestamp=now,
                    news_sentiment=news_sentiment,
                    mode=self.mode,
                )
            except Exception as e:
                applog.warning("engine.exit", f"PIS global evaluation crashed for {symbol}: {e}")

                # ==================================================================================================
                # WARNING TO FUTURE AI AGENTS: DO NOT REVERT THIS FALLBACK CIRCUIT BREAKER!
                # If PIS throws an exception (due to network failure, API downtime, or a code bug),
                # this position MUST NOT BE ABANDONED. Without this fallback, the position will become completely
                # orphaned and bleed capital until the end-of-day timeout.
                # The Dumb Fallback Trailing Stop guarantees that code bugs never cause runaway market losses.
                # ==================================================================================================

                # Engage a mechanical dumb fallback trailing stop if the AI (PIS) goes offline
                peak = self._position_peaks.get(symbol, position.avg_entry_price)
                trough = self._position_troughs.get(symbol, position.avg_entry_price)
                atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else (close * 0.02)

                if position.qty > 0 and close < peak - (3.0 * atr):
                    applog.critical("engine.exit", f"Dumb Fallback triggered for {symbol}: -3.0 ATR trailing stop breached while PIS was offline.")
                    await self._close_position_guarded(symbol, reason="CRITICAL: PIS Offline - Fallback Trailing Stop", qty=abs(position.qty))
                    return []
                elif position.qty < 0 and close > trough + (3.0 * atr):
                    applog.critical("engine.exit", f"Dumb Fallback triggered for {symbol}: +3.0 ATR trailing stop breached while PIS was offline.")
                    await self._close_position_guarded(symbol, reason="CRITICAL: PIS Offline - Fallback Trailing Stop", qty=abs(position.qty))
                    return []

            # =============================================================================
            # [MODIFIED 2026-10-14]: CR-15 - Intelligent EOD Pre-Harvest
            # ORIGINAL INTENT: Positions rode underwater into end-of-day flatten (flatten_all at market).
            # REASON FOR CHANGE: Positions holding loss < -0.30R with poor health at the entry-window cutoff
            # should be scratched early to avoid end-of-day market-order slippage and free risk budget.
            # SAFE ROLLBACK: The original code is preserved below as comments.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # # No EOD pre-harvest check
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            if pis_order is None and position is not None and _lot is not None and hasattr(self, "risk") and hasattr(self.risk, "clock"):
                try:
                    no_entry_mins = int(getattr(self.risk.config, "no_entry_minutes_before_close", 15) or 15)
                    in_preharvest_window = not self.risk.clock.in_entry_window(no_entry_mins, now)
                    if in_preharvest_window:
                        # Calculate current R
                        init_risk = getattr(_lot, "initial_risk_per_share", 0.0) or 0.0
                        if init_risk > 1e-8:
                            current_r = (close - _lot.avg_price) / init_risk if position.qty > 0 else (_lot.avg_price - close) / init_risk
                            if current_r <= -0.30:
                                applog.warning("engine.exit", f"CR-15: EOD pre-harvest triggered for {symbol} at {current_r:.2f}R (near close cutoff)")
                                await self._close_position_guarded(symbol, reason="PIS Exit Reason: EOD_PREHARVEST", qty=abs(position.qty))
                                return []
                except Exception as _eod_exc:
                    applog.debug("engine.exit", f"EOD pre-harvest evaluation skipped: {_eod_exc}")
            # =============================================================================

            if pis_order is not None:
                try:
                    applog.info("engine", f"{symbol}: PIS Unified Exit triggered: {pis_order.reason} (qty={pis_order.qty})")
                    await self._close_position_guarded(symbol, reason=pis_order.reason, qty=pis_order.qty)
                    # Keep peak/trough/stop/TP memory alive until the closing
                    # fill is drained: _drain_fills reads them to stamp true
                    # mfe/mae onto the trade record and cleans the maps up
                    # afterwards. Deleting here (before the fill existed) made
                    # every PIS-exited trade report mfe == mae == final move.
                    return []
                except Exception as e:
                    # ==================================================================================================
                    # WARNING TO FUTURE AI AGENTS:
                    # Do NOT wrap the above `_close_position_guarded` routing call inside the PIS evaluation try-block.
                    # Routing failures (like Alpaca 502s) must NOT be masked as PIS AI evaluation crashes.
                    # ==================================================================================================
                    applog.error("engine.exit", f"Execution failed for PIS exit on {symbol}: {e}")
                    # The scale-out never executed. Revert the brain's decision-time
                    # flags so it cannot phantom-trade as a runner with ratcheted
                    # floors built on a partial exit that did not happen.
                    try:
                        self.pis_adapter.revert_scale_out(symbol)
                    except Exception:
                        pass
            # Synchronize ratcheted protective stop floor to broker truth first,
            # then update the local lot.  Updating only _lot.stop_loss made the
            # PIS floor look protected locally while Alpaca's original OTO stop
            # remained live at the broker, defeating peak-R harvesting.
            if pis_decision is not None and pis_decision.target_stop_loss is not None and _lot is not None:
                new_floor = float(pis_decision.target_stop_loss)
                improves_floor = (
                    position.qty > 0
                    and (_lot.stop_loss is None or new_floor > _lot.stop_loss)
                ) or (
                    position.qty < 0
                    and (_lot.stop_loss is None or new_floor < _lot.stop_loss)
                )
                if improves_floor:
                    if await self._sync_broker_protective_stop(symbol, new_floor):
                        _lot.stop_loss = new_floor

            # All exits (including strategy take profit targets) are now handled exclusively by PIS
            # to guarantee PIS acts as the single source of truth for intelligent exits.
            # (Excursion memory was already updated above, before PIS evaluation.)

        else:
            if symbol in self._position_peaks:
                del self._position_peaks[symbol]
            if symbol in self._position_troughs:
                del self._position_troughs[symbol]
            if symbol in self._position_initial_stops:
                del self._position_initial_stops[symbol]
            self._position_take_profits.pop(symbol, None)
            self.pis_adapter.handle_position_closed(symbol)

        if skip_entries_reason and not getattr(self, "replay_fast_path", False):
            if symbol not in getattr(self, "_entry_gate_warned", set()):
                self._entry_gate_warned.add(symbol)
                applog.info("engine", f"{symbol}: entry evaluation skipped -- {skip_entries_reason}")
            return []


        # Look up mover bias from scanner context (top gainer → long bias, top loser → short bias)
        _scanner = getattr(self, "_scanner", None) or getattr(self, "scanner", None)
        _mover_ctx = getattr(_scanner, "mover_context", {}) if _scanner else {}
        _mover_entry = _mover_ctx.get(symbol, {})
        _mover_bias = _mover_entry.get("direction")   # "gainer" | "loser" | None
        _mover_chg  = float(_mover_entry.get("change_pct", 0.0))

        # Feature 2: Relative Strength vs SPY — multi-timeframe (40% ret_5 + 60% ret_15)
        spy_df = None
        rs_score: float | None = None

        # Determine if cache is fresh for this tick
        tick_id = getattr(self, "_tick_count", 0)
        cached_spy = getattr(self, "_spy_cache", None)
        if cached_spy is not None and cached_spy.get("tick") == tick_id:
            spy_df = cached_spy.get("df")
        else:
            try:
                spy_df = await asyncio.to_thread(enrich, self.data.frame("SPY", "1Min", limit=600))
                self._spy_cache = {"tick": tick_id, "df": spy_df}
            except Exception:
                spy_df = None

        try:
            rs_score = compute_relative_strength(df, spy_df) if spy_df is not None else None
            # Log only meaningful divergence: parity-hugging scores (0.44-0.56)
            # flooded the ring buffer at ~800 lines/10min and flushed rejection
            # forensics out of it.
            if rs_score is not None and abs(rs_score - 0.5) > 0.15:
                applog.debug("engine", f"{symbol} RS vs SPY: rs_score={rs_score:.3f}")
        except Exception:
            rs_score = None  # graceful fallback — pipeline treats None as neutral 50

        sctx = StrategyContext(
            position=position, equity=equity, regime=regime, now=now,
            mover_bias=_mover_bias, mover_change_pct=_mover_chg, rs_score=rs_score,
            daily_sma20=await self._daily_sma20(symbol),
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
            except Exception as exc:  # noqa: BLE001
                applog.error("engine", f"strategy {strat.name} failed on {symbol}: {exc}")
                continue
            if sig is not None:
                sig.strategy_params = getattr(strat, "params", {})
                signals.append(sig)
        if not signals:
            return []

        # Resolve strategy overlap: If multiple entry signals trigger on the same bar,
        # pick the one with the highest priority to prevent signal correlation and AI confusion.
        entry_signals = [sig for sig in signals if sig.action != SignalAction.EXIT]
        exit_signals = [sig for sig in signals if sig.action == SignalAction.EXIT]

        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-08 & L26 - Shadow Ledger Recording for Overlap Drops
        # ORIGINAL INTENT: Kept entry_signals[0] and silently discarded all other signals.
        # REASON FOR CHANGE: Discarded overlap signals left zero forensic trace. Record every
        # discarded signal to shadow_ledger with gate="strategy_overlap" and winner details.
        # SAFE ROLLBACK: The original code is preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if len(entry_signals) > 1:
        #     STRATEGY_PRIORITY = {
        #         "gap_and_go": 1,
        #         "liquidity_sweep": 2,
        #         "bb_squeeze": 3,
        #         "relative_strength": 4,
        #         "trend_pullback": 5,
        #         "orb": 6,
        #         "rsi_reversion": 7,
        #         "vwap_reversion": 8,
        #         "momentum": 9,
        #     }
        #     # Sort ascending by priority (1 is best). Unknown strategies default to 99.
        #     entry_signals.sort(key=lambda s: STRATEGY_PRIORITY.get(s.strategy, 99))
        #     entry_signals = [entry_signals[0]] # Keep only the absolute best one
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if len(entry_signals) > 1:
            STRATEGY_PRIORITY = {
                "gap_and_go": 1,
                "liquidity_sweep": 2,
                "bb_squeeze": 3,
                "relative_strength": 4,
                "trend_pullback": 5,
                "pullback_rearm": 6,
                "orb": 7,
                "rsi_reversion": 8,
                "vwap_reversion": 9,
                "momentum": 10,
            }
            # Sort ascending by priority (1 is best). Unknown strategies default to 99.
            entry_signals.sort(key=lambda s: STRATEGY_PRIORITY.get(s.strategy, 99))
            winner = entry_signals[0]
            for loser in entry_signals[1:]:
                applog.info("pipeline", f"{symbol} {loser.strategy} {loser.action}: rejected -- lost overlap conflict to {winner.strategy}")
                try:
                    from app.core.shadow_ledger import shadow_ledger
                    shadow_ledger.record(
                        symbol=symbol,
                        action=str(loser.action.value if hasattr(loser.action, "value") else loser.action),
                        strategy=loser.strategy,
                        gate="strategy_overlap",
                        reason=f"lost conflict to {winner.strategy} (priority {STRATEGY_PRIORITY.get(winner.strategy, 99)} vs {STRATEGY_PRIORITY.get(loser.strategy, 99)})",
                        price=loser.price or (float(df["close"].iloc[-1]) if len(df) else None),
                        stop_loss=loser.stop_loss,
                        take_profit=loser.take_profit,
                        atr14=float(df["atr14"].iloc[-1]) if "atr14" in df.columns and len(df) else None,
                        confidence=loser.confidence,
                        sig=loser,
                    )
                except Exception:
                    pass
            entry_signals = [winner]
        # =============================================================================

        signals = exit_signals + entry_signals

        if not signals:
            return []

        # NOTE: portfolio sector/correlation gating happens ONCE inside
        # process_portfolio_allocations() (engine tick) -- its version overlays
        # same-cycle approvals onto the broker snapshot. A second pre-ML gate
        # here ran against stale positions only and could contradict the
        # authoritative verdict; removed as redundant.

        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-08 & CR-20 - Decision Bar Timestamp & Reconciliation Shadow Recording
        # ORIGINAL INTENT: Generated candidate_id but left decision_bar_timestamp_utc NULL;
        # dropped candidates during reconciliation failure with log warning only without shadow ledger record.
        # REASON FOR CHANGE: Populate decision_bar_timestamp_utc for backtest/live forensics;
        # record reconciliation rejections to shadow_ledger so no accepted pipeline candidate disappears without trace.
        # SAFE ROLLBACK: The original code is preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # for sig in signals:
        #     if sig.action != SignalAction.EXIT:
        #         if getattr(sig, "candidate_id", None) is None:
        #             prefix = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
        #             sig.candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()
        # ...
        #     if getattr(self, "reconciliation_failed", False):
        #         applog.warning("engine", f"{symbol}: entry signal {sig.strategy} rejected -- engine position/broker reconciliation state is uncertain")
        #         continue
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                if getattr(sig, "candidate_id", None) is None:
                    prefix = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
                    sig.candidate_id = f"{prefix}_{uuid.uuid4().hex[:6]}".upper()
                if getattr(sig, "decision_bar_timestamp_utc", None) is None and len(df) > 0:
                    last_idx = df.index[-1]
                    sig.decision_bar_timestamp_utc = pd.Timestamp(last_idx).to_pydatetime() if hasattr(last_idx, "to_pydatetime") else last_idx

        accepted, rejected = await asyncio.to_thread(
            partial(self.pipeline.process, symbol, df, signals, regime, sector_strength=rs_score)
        )

        for sig, reason in rejected:
            # INFO: rejection forensics must survive in the console/UI stream.
            # DEBUG here made gate decisions invisible (see 2026-08-24 session
            # analysis) while per-tick RS spam flushed the ring buffer.
            applog.info("pipeline", f"{symbol} {sig.strategy} {sig.action}: rejected -- {reason}")
            try:
                from app.core.shadow_ledger import shadow_ledger
                shadow_ledger.record(
                    symbol=symbol,
                    action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                    strategy=sig.strategy,
                    gate="pipeline",
                    reason=reason,
                    price=sig.price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    atr14=float(df["atr14"].iloc[-1]) if "atr14" in df.columns and len(df) else None,
                    confidence=sig.confidence,
                    sig=sig,
                )
            except Exception:
                pass

        candidate_entries = []
        for sig in accepted:
            if sig.action == SignalAction.EXIT:
                applog.debug("engine", f"{symbol}: Discarding legacy strategy EXIT signal for {sig.strategy}. PIS exclusively owns exits.")
                continue

            if getattr(self, "reconciliation_failed", False):
                applog.warning("engine", f"{symbol}: entry signal {sig.strategy} rejected -- engine position/broker reconciliation state is uncertain")
                try:
                    from app.core.shadow_ledger import shadow_ledger
                    shadow_ledger.record(
                        symbol=symbol,
                        action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                        strategy=sig.strategy,
                        gate="reconciliation",
                        reason="engine position/broker reconciliation state is uncertain",
                        price=sig.price,
                        stop_loss=sig.stop_loss,
                        take_profit=sig.take_profit,
                        atr14=float(df["atr14"].iloc[-1]) if "atr14" in df.columns and len(df) else None,
                        confidence=sig.confidence,
                        sig=sig,
                    )
                except Exception:
                    pass
                continue

            candidate_entries.append((sig, df))
        # =============================================================================
        return candidate_entries

    async def _sync_broker_protective_stop(self, symbol: str, stop_price: float) -> bool:
        """Synchronize a monotonic PIS stop floor with broker-side protection.

        PaperBroker stores the protective stop on the in-memory position and has
        no replacement API, so the caller may update its lot directly.  The
        hardened Alpaca adapter exposes ``replace_exit_leg``; in that path the
        child stop order must be found and broker-verified before local state is
        advanced.  A failed/ambiguous replacement leaves the old local floor in
        place and retries on a later bar rather than claiming protection that
        the broker has not confirmed.
        """
        broker = getattr(self, "broker", None)
        replace_exit_leg = getattr(broker, "replace_exit_leg", None)
        if not callable(replace_exit_leg):
            # PaperBroker has no network replacement API; keep its authoritative
            # simulated position synchronized so bracket checks use the same
            # monotonic floor as the portfolio/PIS state.
            simulated_positions = getattr(broker, "_positions", None)
            simulated_position = simulated_positions.get(symbol.upper()) if simulated_positions is not None else None
            if simulated_position is not None:
                simulated_position.stop_loss = stop_price
            return True
        try:
            open_orders = await broker.open_orders()
            stop_orders = [
                order for order in open_orders
                if str(getattr(order, "symbol", "")).upper() == symbol.upper()
                and str(getattr(order, "leg", "")).lower() == "stop"
                and getattr(order, "status", None) not in {
                    OrderStatus.CANCELED,
                    OrderStatus.REJECTED,
                    OrderStatus.FILLED,
                }
            ]
            if not stop_orders:
                applog.warning(
                    "engine.exit",
                    f"{symbol}: PIS stop floor {stop_price:.4f} not synchronized; no open broker stop leg found",
                )
                return False
            for order in stop_orders:
                replacement = await replace_exit_leg(str(order.id), stop_price=stop_price)
                if replacement is not None:
                    return True
            applog.warning(
                "engine.exit",
                f"{symbol}: broker rejected or could not verify PIS stop replacement at {stop_price:.4f}",
            )
            return False
        except Exception as exc:  # noqa: BLE001
            applog.warning("engine.exit", f"{symbol}: protective stop synchronization failed: {exc}")
            return False
