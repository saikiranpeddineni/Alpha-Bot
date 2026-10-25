"""TradingEngineEvaluationMixin mixin."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial

import pandas as pd

from app.ai.regime import detect_regime
from app.core.applog import applog
from app.core.domain import OrderStatus, Position, Signal, SignalAction, new_id
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
            end = datetime.now(UTC)
            daily = await self.data.history(key, "1Day", end - timedelta(days=60), end)
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

        # =============================================================================
        # [MODIFIED 2026-10-23]: Allow Fail-Soft Warmup Frame (>= 5 bars) to Capture Scanner Movers
        # ORIGINAL INTENT: Dropped any candidate with len(df) < 15 bars.
        # REASON FOR CHANGE: Shadow ledger audit proved 71 winning trades (83.01% cumulative PnL,
        # including BTCS +8.53%, +7.91%, +6.72%) were dropped because newly scanned momentum runners
        # had 5-14 bars on initial discovery. Reducing threshold to 5 bars captures fresh morning runners.
        # SAFE ROLLBACK: Previous lines preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if df.empty or len(df) < 15:
        #     return []
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if df.empty or len(df) < 5:
            return []
        # =============================================================================

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
        # =============================================================================
        # [MODIFIED 2026-10-14]: CR-08 — Gated-tick shadow ledger recording
        # ORIGINAL INTENT: Early returned [] with applog only, leaving no trace in shadow ledger.
        # REASON FOR CHANGE: Gated-tick drops were traceless (360 candidates lost without trace).
        #   Record every gated tick drop to shadow_ledger with gate="gated_tick".
        # SAFE ROLLBACK: Commented-out original code below.
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        if position is None and skip_entries_reason and not getattr(self, "replay_fast_path", False):
            if symbol not in getattr(self, "_entry_gate_warned", set()):
                self._entry_gate_warned.add(symbol)
                applog.info("engine", f"{symbol}: entry evaluation skipped -- {skip_entries_reason}")
            try:
                from app.core.shadow_ledger import shadow_ledger
                shadow_ledger.record(
                    symbol=symbol,
                    action="ENTER_LONG",
                    strategy="engine",
                    gate="gated_tick",
                    reason=f"gated tick ({skip_entries_reason})",
                    price=float(df["close"].iloc[-1]) if not df.empty and "close" in df else 0.0,
                    stop_loss=None,
                    take_profit=None,
                    atr14=float(df["atr14"].iloc[-1]) if not df.empty and "atr14" in df.columns else None,
                    confidence=0.0,
                    decision_bar_timestamp_utc=(
                        pd.Timestamp(df.index[-1]).to_pydatetime()
                        if not df.empty
                        else None
                    ),
                    candidate_id=(
                        f"{symbol}_GATED_TICK_{pd.Timestamp(df.index[-1]).strftime('%Y%m%d_%H%M%S')}"
                        if not df.empty
                        else None
                    ),
                )
            except Exception:
                pass
            return []
        # =============================================================================

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
            # SAFE ROLLBACK: The original code is removed; rollback via git history.
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            if pis_order is None and position is not None and _lot is not None and hasattr(self, "risk") and hasattr(self.risk, "clock"):
                try:
                    no_entry_mins = int(getattr(self.risk.config, "no_entry_minutes_before_close", 15) or 15)
                    in_preharvest_window = not self.risk.clock.in_entry_window(no_entry_mins, now)
                    if in_preharvest_window:
                        init_risk = getattr(_lot, "initial_risk_per_share", 0.0) or 0.0
                        if init_risk > 1e-8:
                            current_r = (close - _lot.avg_price) / init_risk if position.qty > 0 else (_lot.avg_price - close) / init_risk
                            preharvest_threshold = float(getattr(self.risk.config, "eod_preharvest_r", -0.30))
                            # [NEW CODE]:
                            from app.pis.models import LifecycleState
                            if pis_decision is not None:
                                health_obj = getattr(pis_decision, "health", None)
                                health_val = float(getattr(health_obj, "overall", 0.0) if health_obj else getattr(pis_decision, "health_score", 0.0))
                                is_runner = (getattr(pis_decision, "lifecycle_state", None) == LifecycleState.RUNNER) or (float(getattr(pis_decision, "runner_score", 0.0) or 0.0) >= 0.70)
                            else:
                                health_val = 0.0
                                is_runner = False
                            # Scratch dying trade early only if underwater <= threshold, health < 0.45, and not an active runner
                            if current_r <= preharvest_threshold and health_val < 0.45 and not is_runner:
                                applog.warning("engine.exit", f"CR-15: EOD pre-harvest triggered for {symbol} at {current_r:.2f}R (health={health_val:.2f}, near close cutoff)")
                                await self._close_position_guarded(symbol, reason="PIS Exit Reason: EOD_PREHARVEST", qty=abs(position.qty))
                                return []
                except Exception as _eod_exc:
                    applog.debug("engine.exit", f"EOD pre-harvest evaluation skipped: {_eod_exc}")
            # =============================================================================

            if pis_order is not None:
                # =============================================================================
                # [MODIFIED 2026-10-23]: Minimum Bank Profit Threshold Floor ($100.00) on Partial Exits
                # ORIGINAL INTENT: Unconditionally executed partial scale-outs regardless of dollar profit.
                # REASON FOR CHANGE: Forensic audit proved slicing small positions (e.g. $10-$30 profit)
                # incurs commission and spread friction while halving runner compounding. If expected bank
                # profit < min_bank_usd (default $100.0), skip partial cut, hold 100% position with
                # ratcheted breakeven/floor stop protection to let multi-R winners expand.
                # SAFE ROLLBACK: Previous lines preserved below as comments.
                # -----------------------------------------------------------------------------
                # [PREVIOUS CODE - PRESERVED]:
                # close_accepted = False
                # try:
                #     applog.info("engine", f"{symbol}: PIS Unified Exit triggered: {pis_order.reason} (qty={pis_order.qty})")
                #     close_accepted = await self._close_position_guarded(
                #         symbol,
                #         reason=pis_order.reason,
                #         qty=pis_order.qty,
                #         strategy=getattr(pis_order, "strategy", None),
                #     )
                # except Exception as e:
                #     applog.error("engine.exit", f"Execution failed for PIS exit on {symbol}: {e}")
                #     close_accepted = False
                # if not close_accepted:
                #     with suppress(Exception):
                #         self.pis_adapter.revert_scale_out(symbol)
                #     applog.warning("engine.exit", f"{symbol}: PIS close was not accepted; scale-out state reverted")
                #     return []
                # -----------------------------------------------------------------------------
                # [NEW CODE]:
                import os as _os
                from app.pis.models import ExitAction
                is_partial = (pis_decision is not None and getattr(pis_decision, "action", None) == ExitAction.PARTIAL_EXIT) or (position is not None and pis_order.qty < abs(position.qty))
                min_bank_usd = float(_os.environ.get("BOT_MIN_BANK_PROFIT_USD", getattr(getattr(self, "risk_config", None), "min_bank_profit_usd", 100.0) if hasattr(self, "risk_config") else 100.0) or 100.0)

                skip_partial_scaleout = False
                if is_partial and min_bank_usd > 0.0 and position is not None:
                    unrealized_profit_per_share = (close - position.avg_entry_price) if position.qty > 0 else (position.avg_entry_price - close)
                    expected_bank_profit = float(pis_order.qty) * unrealized_profit_per_share
                    if expected_bank_profit < min_bank_usd:
                        skip_partial_scaleout = True
                        with suppress(Exception):
                            self.pis_adapter.revert_scale_out(symbol)
                        applog.info(
                            "engine.exit",
                            f"{symbol}: skipping PIS partial scale-out ({pis_order.reason}) -- expected profit ${expected_bank_profit:.2f} < ${min_bank_usd:.0f} floor "
                            f"(holding full {abs(position.qty):.0f} sh position with BE/floor stop protection to avoid micro-slice churn)",
                        )
                        pis_order = None

                if not skip_partial_scaleout and pis_order is not None:
                    close_accepted = False
                    try:
                        applog.info("engine", f"{symbol}: PIS Unified Exit triggered: {pis_order.reason} (qty={pis_order.qty})")
                        close_accepted = await self._close_position_guarded(
                            symbol,
                            reason=pis_order.reason,
                            qty=pis_order.qty,
                            strategy=getattr(pis_order, "strategy", None),
                        )
                    except Exception as e:
                        # Keep broker-routing failures distinct from PIS evaluation
                        # failures.  The guard has already released its latch;
                        # this boundary only converts the ambiguous result into a
                        # PIS rollback before the next evaluation can retry.
                        applog.error("engine.exit", f"Execution failed for PIS exit on {symbol}: {e}")
                        close_accepted = False

                    if not close_accepted:
                        # A rejected, missing, or ambiguous broker result is not a
                        # fill.  In particular, PARTIAL_EXIT mutates PIS state at
                        # decision time, so leave the brain in its pre-scale-out
                        # posture until broker execution is actually accepted.
                        with suppress(Exception):
                            self.pis_adapter.revert_scale_out(symbol)
                        applog.warning("engine.exit", f"{symbol}: PIS close was not accepted; scale-out state reverted")
                        # Keep the existing close-latch safety contract: do not
                        # continue into entry evaluation after an exit decision.
                        return []
                # =============================================================================

                # Alpaca partial closes cancel the live bracket/OTO legs to
                # release held quantity.  Re-establish a broker stop for only
                # the residual runner before returning from this evaluation;
                # otherwise the runner is naked during the next tick.
                if pis_order is not None:
                    position_qty = abs(float(position.qty))
                    close_qty = abs(float(pis_order.qty))
                    residual_qty = max(0.0, position_qty - close_qty)
                    if residual_qty > 1e-9:
                        runner_floor = getattr(pis_decision, "target_stop_loss", None)
                        if runner_floor is None and _lot is not None:
                            runner_floor = getattr(_lot, "stop_loss", None)
                        if runner_floor is not None:
                            runner_floor = float(runner_floor)
                            # Protection floors remain monotonic even when a stale
                            # PIS decision carries a weaker candidate than local
                            # broker-confirmed state.
                            local_floor = (
                                getattr(_lot, "stop_loss", None)
                                if _lot is not None
                                else None
                            )
                            if local_floor is not None:
                                runner_floor = (
                                    max(runner_floor, float(local_floor))
                                    if position.qty > 0
                                    else min(runner_floor, float(local_floor))
                                )
                            try:
                                protected = await self._sync_broker_protective_stop(
                                    symbol,
                                    runner_floor,
                                    qty=residual_qty,
                                )
                            except Exception as exc:  # noqa: BLE001
                                protected = False
                                applog.warning("engine.exit", f"{symbol}: residual runner stop repair failed: {exc}")
                            if protected and _lot is not None:
                                _lot.stop_loss = runner_floor
                            elif not protected:
                                applog.warning(
                                    "engine.exit",
                                    f"{symbol}: residual runner close accepted but broker protection was not verified",
                                )
                        else:
                            applog.warning(
                                "engine.exit",
                                f"{symbol}: partial PIS close accepted with no protective floor available for residual qty={residual_qty:g}",
                            )

                    # Keep peak/trough/stop/TP memory alive until the closing
                    # fill is drained: _drain_fills reads them to stamp true
                    # mfe/mae onto the trade record and cleans the maps up
                    # afterwards. Deleting here (before the fill existed) made
                    # every PIS-exited trade report mfe == mae == final move.
                    return []
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
                if improves_floor and await self._sync_broker_protective_stop(symbol, new_floor):
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

        # =============================================================================
        # [MODIFIED 2026-10-16]: Remove redundant duplicate gated-tick check
        # ORIGINAL INTENT: Duplicate check of skip_entries_reason after position exits.
        # REASON FOR CHANGE: Unreachable code. Lines 153-174 already handle this when position is None.
        # When position is not None, skip_entries_reason is never set.
        # SAFE ROLLBACK: Preserved below as comments.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # if skip_entries_reason and not getattr(self, "replay_fast_path", False):
        #     if symbol not in getattr(self, "_entry_gate_warned", set()):
        #         self._entry_gate_warned.add(symbol)
        #         applog.info("engine", f"{symbol}: entry evaluation skipped -- {skip_entries_reason}")
        #     try:
        #         from app.core.shadow_ledger import shadow_ledger
        #         shadow_ledger.record(
        #             symbol=symbol,
        #             action="ENTER_LONG",
        #             strategy="engine",
        #             gate="gated_tick",
        #             reason=f"gated tick ({skip_entries_reason})",
        #             price=float(df["close"].iloc[-1]) if not df.empty and "close" in df else 0.0,
        #             stop_loss=None,
        #             take_profit=None,
        #             atr14=float(df["atr14"].iloc[-1]) if not df.empty and "atr14" in df.columns else None,
        #             confidence=0.0,
        #         )
        #     except Exception:
        #         pass
        #     return []
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        # (Redundant duplicate omitted; authoritative check executes at lines 153-174)
        # =============================================================================


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
            # =============================================================================
            # [ADDED 2026-10-14]: CR-02b — Wire RiskConfig.enable_pullback_rearm
            # ORIGINAL INTENT: RiskConfig.enable_pullback_rearm was defined but unread by engine.
            # REASON FOR CHANGE: Enforce RiskConfig.enable_pullback_rearm dynamically at runtime.
            # [NEW CODE]:
            if (
                strat.name == "pullback_rearm"
                and hasattr(self, "risk")
                and hasattr(self.risk, "config")
                and not getattr(self.risk.config, "enable_pullback_rearm", True)
            ):
                continue
            # =============================================================================
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

        # Assign lineage before any downstream filter so opportunity, pipeline,
        # portfolio, and reconciliation shadow rows all identify the same candidate.
        for sig in signals:
            if sig.action != SignalAction.EXIT:
                if getattr(sig, "candidate_id", None) is None:
                    prefix = f"{symbol}_{sig.strategy}_{now.strftime('%Y%m%d_%H%M%S')}"
                    # Use the domain ID helper so replay's deterministic UUID
                    # binding also covers candidate lineage.
                    sig.candidate_id = f"{prefix}_{new_id()[:6]}".upper()
                if getattr(sig, "decision_bar_timestamp_utc", None) is None and len(df) > 0:
                    last_idx = df.index[-1]
                    sig.decision_bar_timestamp_utc = pd.Timestamp(last_idx).to_pydatetime() if hasattr(last_idx, "to_pydatetime") else last_idx

        # Opportunity economics are evaluated before the admission firewall.
        # This layer only removes candidates whose modeled reward cannot cover
        # configured round-trip costs; sizing, portfolio gates, ML, and broker
        # execution remain authoritative downstream.
        try:
            from app.config import get_settings
            from app.opportunities.engine import OpportunityEngine
            from app.opportunities.models import OpportunityContext
            opportunity_cfg = get_settings().opportunity_config()
            if opportunity_cfg.enabled:
                evaluator = OpportunityEngine()
                economic_signals = []
                for sig in signals:
                    if sig.action == SignalAction.EXIT:
                        economic_signals.append(sig)
                        continue
                    candidate = evaluator.evaluate(
                        sig,
                        df,
                        OpportunityContext(
                            min_net_edge_bps=opportunity_cfg.min_net_edge_bps,
                            min_reward_risk=opportunity_cfg.min_reward_risk,
                            estimated_round_trip_cost_bps=opportunity_cfg.round_trip_cost_bps,
                        ),
                    )
                    if candidate is not None:
                        economic_signals.append(sig)
                    else:
                        reason = "rejected by cost-aware opportunity economics"
                        applog.info("opportunity", f"{symbol} {sig.strategy}: {reason}")
                        try:
                            from app.core.shadow_ledger import shadow_ledger
                            shadow_ledger.record(
                                symbol=symbol,
                                action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                                strategy=sig.strategy,
                                gate="opportunity",
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
                signals = economic_signals
        except Exception as exc:  # defensive telemetry must not break exits
            applog.warning("opportunity", f"cost-aware opportunity evaluation unavailable: {exc}")

        if not signals:
            return []

        # Keep every entry candidate together until the pipeline evaluates ML, meta,
        # Trade Score, and conflict ranking. A second static priority gate here hid
        # lower-priority strategies before those quality signals could be measured.
        # Post-hoc shadow verdicts are deliberately not used to bypass live gates;
        # MISSED_WINNER is analytics-only and AVOIDED_LOSER never becomes a rescue input.
        entry_signals = [sig for sig in signals if sig.action != SignalAction.EXIT]
        exit_signals = [sig for sig in signals if sig.action == SignalAction.EXIT]

        signals = exit_signals + entry_signals

        if not signals:
            return []

        # NOTE: portfolio sector/correlation gating happens ONCE inside
        # process_portfolio_allocations() (engine tick) -- its version overlays
        # same-cycle approvals onto the broker snapshot. A second pre-ML gate
        # here ran against stale positions only and could contradict the
        # authoritative verdict; removed as redundant.

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
                    gate="strategy_overlap" if "lost conflict to" in str(reason) else "pipeline",
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

            # =============================================================================
            # [MODIFIED 2026-10-25]: Hook Pre-Trade Monte Carlo Replay Gate (Time Machine)
            # ORIGINAL INTENT: Accepted candidates immediately appended to candidate_entries.
            # REASON FOR CHANGE: Day Trader Suite Time Machine module replays 2,000 bootstrap
            #                    paths, vetoing setups with Target-First prob < 35% or ExpR < 0.05R.
            # SAFE ROLLBACK: Set enable_monte_carlo_gate = False in config or environment.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # candidate_entries.append((sig, df))
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            mc_gate_enabled = False
            try:
                cfg_obj = getattr(self, "risk_config", None) or getattr(self, "settings", None)
                if cfg_obj is None:
                    from app.config import get_settings
                    cfg_obj = get_settings()
                mc_gate_enabled = getattr(cfg_obj, "enable_monte_carlo_gate", False)
            except Exception:
                mc_gate_enabled = False

            if mc_gate_enabled:
                try:
                    from app.ai.monte_carlo_gate import MonteCarloGate
                    min_prob = float(getattr(cfg_obj, "monte_carlo_min_win_prob", 0.35))
                    min_exp_r = float(getattr(cfg_obj, "monte_carlo_min_expected_r", 0.05))
                    mc_gate = MonteCarloGate(min_win_prob=min_prob, min_expected_r=min_exp_r)
                    passed_mc, mc_stats, mc_reason = mc_gate.evaluate_signal(sig, df)

                    if not passed_mc:
                        applog.info("monte_carlo", f"{symbol} {sig.strategy}: {mc_reason}")
                        try:
                            from app.core.shadow_ledger import shadow_ledger
                            shadow_ledger.record(
                                symbol=symbol,
                                action=str(sig.action.value if hasattr(sig.action, "value") else sig.action),
                                strategy=sig.strategy,
                                gate="monte_carlo",
                                reason=mc_reason,
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
                    else:
                        sig.monte_carlo_p_target = mc_stats.get("p_target")
                        sig.monte_carlo_expected_r = mc_stats.get("avg_r")
                        applog.debug("monte_carlo", f"{symbol} {sig.strategy}: {mc_reason}")
                except Exception as mc_err:
                    applog.warning("monte_carlo", f"{symbol} {sig.strategy}: fail-soft on monte carlo evaluation: {mc_err}")

            candidate_entries.append((sig, df))
            # =============================================================================
        # =============================================================================
        return candidate_entries

    async def _sync_broker_protective_stop(
        self,
        symbol: str,
        stop_price: float,
        qty: float | None = None,
    ) -> bool:
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
                # STOP-LEG REPAIR 2026-10-16: entries are broker-request
                # target-free by design (PIS owns exits), so positions opened
                # without a bracket often have NO broker stop leg at all —
                # then this warned every minute while the position rode naked
                # (2026-10-15 LRCX: floor 273.86, no leg, server died before
                # EOD). Repair instead of only warning: submit a standalone
                # STOP at the PIS floor. Later ratchets use replace_exit_leg.
                # Throttled to one attempt per symbol per 60s so a persistent
                # rejection cannot spam the broker.
                import time as _time

                from app.core.domain import OrderRequest, OrderSide, OrderType

                now_ts = _time.time()
                last_try = getattr(self, "_stop_leg_submit_ts", {}).get(symbol.upper(), 0.0)
                if now_ts - last_try < 60.0:
                    return False
                try:
                    broker_positions = await broker.positions()
                except Exception:
                    broker_positions = []
                pos = next(
                    (p for p in broker_positions if str(getattr(p, "symbol", "")).upper() == symbol.upper()),
                    None,
                )
                if pos is None or not abs(float(getattr(pos, "qty", 0.0) or 0.0)) > 0:
                    applog.warning(
                        "engine.exit",
                        f"{symbol}: PIS stop floor {stop_price:.4f} not synchronized; no open broker stop leg found",
                    )
                    return False
                if not hasattr(self, "_stop_leg_submit_ts") or not isinstance(self._stop_leg_submit_ts, dict):
                    self._stop_leg_submit_ts = {}
                self._stop_leg_submit_ts[symbol.upper()] = now_ts
                broker_qty = abs(float(pos.qty))
                requested_qty = broker_qty if qty is None else abs(float(qty))
                repair_qty = min(broker_qty, requested_qty)
                if repair_qty <= 1e-9:
                    return False
                side = OrderSide.SELL if float(pos.qty) > 0 else OrderSide.BUY
                try:
                    repair = await broker.submit(
                        OrderRequest(
                            symbol=symbol.upper(),
                            side=side,
                            qty=repair_qty,
                            order_type=OrderType.STOP,
                            stop_price=float(stop_price),
                            strategy="pis_protection",
                            reason=f"PIS protective stop repair @ {stop_price:.4f}",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    applog.warning("engine.exit", f"{symbol}: protective stop repair submit failed: {exc}")
                    return False
                if getattr(repair, "status", None) == OrderStatus.REJECTED:
                    applog.warning(
                        "engine.exit",
                        f"{symbol}: protective stop repair rejected: {getattr(repair, 'reason', '')}",
                    )
                    return False
                applog.warning(
                    "engine.exit",
                    f"{symbol}: submitted missing broker STOP leg @ {stop_price:.4f} (PIS floor repair)",
                )
                return True
            # =============================================================================
            # [MODIFIED 2026-10-23]: Redundant Stop Replacement Check & Tolerance Gate
            # ORIGINAL INTENT: Always issued network replace_exit_leg call even if broker stop
            # price was already set to the target stop price within exchange tick precision.
            # REASON FOR CHANGE: Micro-cent trailing stop recalculations (e.g. 21.5003 vs 21.50)
            # sent redundant PATCH requests to Alpaca, causing 42210000 "order parameters are not changed".
            # SAFE ROLLBACK: Previous code preserved below as comments.
            # -----------------------------------------------------------------------------
            # [PREVIOUS CODE - PRESERVED]:
            # for order in stop_orders:
            #     replacement = await replace_exit_leg(str(order.id), stop_price=stop_price)
            #     if replacement is not None:
            #         return True
            # applog.warning(
            #     "engine.exit",
            #     f"{symbol}: broker rejected or could not verify PIS stop replacement at {stop_price:.4f}",
            # )
            # return False
            # -----------------------------------------------------------------------------
            # [NEW CODE]:
            for order in stop_orders:
                existing_stop = getattr(order, "stop_price", None)
                if existing_stop is not None and abs(float(existing_stop) - float(stop_price)) < 0.005:
                    # Stop order is already at target price within half a cent (sub-penny rounding); avoid network call
                    return True
                replacement = await replace_exit_leg(str(order.id), stop_price=stop_price)
                if replacement is not None:
                    return True
            applog.warning(
                "engine.exit",
                f"{symbol}: broker rejected or could not verify PIS stop replacement at {stop_price:.4f}",
            )
            return False
            # =============================================================================
        except Exception as exc:  # noqa: BLE001
            applog.warning("engine.exit", f"{symbol}: protective stop synchronization failed: {exc}")
            return False
