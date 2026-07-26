"""One-shot deterministic cutover for issue #276.

This script is intentionally assertion-heavy: it refuses to edit engine.py unless
all expected pre-migration anchors are present exactly once. It changes research
wiring only; Champion gate/risk/broker logic is not rewritten.
"""
from pathlib import Path

PATH = Path("backend/app/core/engine.py")
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one engine anchor, found {count}: {old[:100]!r}")
    text = text.replace(old, new, 1)


replace_once(
    "from app.ai.gemini_ranker import GeminiShadowRanker\nfrom app.ai.shadow_bridge import GeminiShadowBridge\nfrom app.research.spawn import SpawnShadowCoordinator\n",
    "from app.research.event_builder import candidate_event_from_signal, market_bar_event\n"
    "from app.research.shadow_runtime import ShadowResearchRuntime\n",
)

replace_once(
    '''        self.gemini_shadow = GeminiShadowBridge(\n            GeminiShadowRanker(settings.gemini_api_key),\n            observation_sink=self._persist_gemini_shadow_observation,\n        )\n        self.spawn_coordinator = SpawnShadowCoordinator(db=self.db)\n''',
    '''        # One explicit production-to-research boundary. Research receives only\n        # immutable value snapshots and has no execution-result channel.\n        self.shadow_research = ShadowResearchRuntime(\n            db=self.db,\n            gemini_api_key=settings.gemini_api_key,\n            gemini_observation_sink=self._persist_gemini_shadow_observation,\n        )\n''',
)

replace_once("        await self.spawn_coordinator.start()\n", "        await self.shadow_research.start()\n")
# stop() method: use minimal unique surrounding context (flatten_all + state check) to disambiguate
replace_once(
    "                applog.error(\"engine\", f\"flatten on stop failed: {exc}\")\n"
    "        await self.spawn_coordinator.stop()\n"
    "        if self.state != EngineState.HALTED:\n",
    "                applog.error(\"engine\", f\"flatten on stop failed: {exc}\")\n"
    "        await self.shadow_research.stop()\n"
    "        if self.state != EngineState.HALTED:\n",
)

old_bar = '''                    # Publish closed bar to shadow spawn army (non-blocking, fail-open)\n                    try:\n                        bar_df = new_closed[sym_upper]\n                        last_row = bar_df.iloc[-1]\n                        bar_ts = bar_df.index[-1].to_pydatetime()\n                        if bar_ts.tzinfo is None:\n                            from datetime import timezone as _tz\n                            bar_ts = bar_ts.replace(tzinfo=_tz.utc)\n                        self.spawn_coordinator.publish_market_bar(\n                            sym_upper,\n                            float(last_row.get("open", last_row.get("close", 0))),\n                            float(last_row.get("high", last_row.get("close", 0))),\n                            float(last_row.get("low", last_row.get("close", 0))),\n                            float(last_row.get("close", 0)),\n                            bar_ts,\n                        )\n                    except Exception:\n                        pass  # fail-open: shadow bar loss never blocks Champion\n'''
new_bar = '''                    # Mirror the exact completed market bar through the canonical\n                    # non-blocking research boundary. Never manufacture missing OHLC.\n                    try:\n                        bar_df = new_closed[sym_upper]\n                        last_row = bar_df.iloc[-1]\n                        bar_ts = bar_df.index[-1].to_pydatetime()\n                        if bar_ts.tzinfo is None:\n                            bar_ts = bar_ts.replace(tzinfo=timezone.utc)\n                        required = ("open", "high", "low", "close")\n                        if all(name in last_row and pd.notna(last_row[name]) for name in required):\n                            self.shadow_research.publish(market_bar_event(\n                                symbol=sym_upper,\n                                ts=bar_ts,\n                                open_=float(last_row["open"]),\n                                high=float(last_row["high"]),\n                                low=float(last_row["low"]),\n                                close=float(last_row["close"]),\n                                atr=float(last_row["atr14"]) if "atr14" in last_row and pd.notna(last_row["atr14"]) else None,\n                                rsi=float(last_row["rsi2"]) if "rsi2" in last_row and pd.notna(last_row["rsi2"]) else None,\n                            ))\n                    except Exception as exc:\n                        applog.debug("shadow_research", f"market-bar mirror failed open for {sym_upper}: {exc}")\n'''
replace_once(old_bar, new_bar)

old_candidate = '''                # Shadow research observes only deterministic post-allocation candidates.\n                # The bridge has no return channel into risk, order construction, or submission.\n                self.gemini_shadow.schedule(\n                    [sig for sig, _ in decision_outcome.approved_entries],\n                    session=session,\n                )\n                \n                # Publish immutable candidate observations to SpawnShadowCoordinator out-of-band\n                for sig, df in candidates:\n                    try:\n                        latest_row = df.iloc[-1] if not df.empty else None\n                        open_px = float(latest_row["open"]) if latest_row is not None and "open" in latest_row else float(sig.price)\n                        high_px = float(latest_row["high"]) if latest_row is not None and "high" in latest_row else float(sig.price)\n                        low_px = float(latest_row["low"]) if latest_row is not None and "low" in latest_row else float(sig.price)\n                        close_px = float(latest_row["close"]) if latest_row is not None and "close" in latest_row else float(sig.price)\n                        \n                        cand_id = getattr(sig, "candidate_id", f"{sig.symbol}_{sig.strategy}_{int(now.timestamp())}")\n                        is_approved = any(getattr(s, "candidate_id", None) == getattr(sig, "candidate_id", None) for s, _ in decision_outcome.approved_entries)\n                        champ_decision = "accepted" if is_approved else "rejected"\n                        \n                        obs = {\n                            "symbol": sig.symbol,\n                            "strategy": sig.strategy,\n                            "direction": sig.side,\n                            "price": sig.price,\n                            "open": open_px,\n                            "high": high_px,\n                            "low": low_px,\n                            "close": close_px,\n                            "atr": float(latest_row.get("atr", 1.0)) if latest_row is not None and "atr" in latest_row else 1.0,\n                            "rsi": float(latest_row.get("rsi", 50.0)) if latest_row is not None and "rsi" in latest_row else None,\n                            "stop_loss": sig.stop_loss,\n                            "take_profit": sig.take_profit,\n                            "ts": now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc),\n                            "champion_candidate": {\n                                "id": cand_id,\n                                "ml_probability": getattr(sig, "ml_confidence", None),\n                                "meta_probability": getattr(sig, "meta_probability", None),\n                                "trade_score": getattr(sig, "trade_score", None),\n                                "final_decision": champ_decision,\n                            }\n                        }\n                        self.spawn_coordinator.publish_observation(obs)\n                    except Exception as spawn_pub_err:\n                        applog.warning("engine", f"Failed publishing shadow observation: {spawn_pub_err}")\n\n'''
new_candidate = '''                # Mirror every post-allocation candidate once through the typed bus.\n                # This preserves Champion behavior: publishing is put_nowait/fail-open and\n                # no research result is read by portfolio, risk, or broker code.\n                approved_ids = {getattr(sig, "candidate_id", None) for sig, _ in decision_outcome.approved_entries}\n                gated_reasons = {getattr(sig, "candidate_id", None): reason for sig, reason in decision_outcome.gated_entries}\n                for ordinal, (sig, df) in enumerate(candidates):\n                    try:\n                        latest_row = df.iloc[-1] if not df.empty else None\n                        cid = getattr(sig, "candidate_id", None)\n                        approved = cid in approved_ids\n                        event = candidate_event_from_signal(\n                            sig,\n                            final_decision="accepted" if approved else "rejected",\n                            rejection_reason=None if approved else gated_reasons.get(cid, "portfolio/allocation rejected"),\n                            open_=float(latest_row["open"]) if latest_row is not None and "open" in latest_row and pd.notna(latest_row["open"]) else None,\n                            high=float(latest_row["high"]) if latest_row is not None and "high" in latest_row and pd.notna(latest_row["high"]) else None,\n                            low=float(latest_row["low"]) if latest_row is not None and "low" in latest_row and pd.notna(latest_row["low"]) else None,\n                            close=float(latest_row["close"]) if latest_row is not None and "close" in latest_row and pd.notna(latest_row["close"]) else None,\n                            atr=float(latest_row["atr14"]) if latest_row is not None and "atr14" in latest_row and pd.notna(latest_row["atr14"]) else None,\n                            rsi=float(latest_row["rsi2"]) if latest_row is not None and "rsi2" in latest_row and pd.notna(latest_row["rsi2"]) else None,\n                            meta_probability=getattr(sig, "meta_probability", None),\n                            portfolio_decision="PASS" if approved else "REJECT",\n                            equity=float(account.equity),\n                            cash=float(account.cash),\n                            buying_power=float(account.buying_power),\n                            vix=self._last_vix_value,\n                            vix_status=self._last_vix_status,\n                            ordinal=ordinal,\n                        )\n                        self.shadow_research.publish(event)\n                    except Exception as exc:\n                        applog.debug("shadow_research", f"candidate mirror failed open for {sig.symbol}: {exc}")\n\n'''
replace_once(old_candidate, new_candidate)

# shutdown() used to stop each direct experiment and then call stop(), which also
# stopped Spawn. The runtime owns all research cleanup now; stop() is idempotent.
replace_once(
    '''    async def shutdown(self) -> None:\n        await self.gemini_shadow.shutdown()\n        await self.spawn_coordinator.stop()\n        await self.stop(flatten=False)\n''',
    '''    async def shutdown(self) -> None:\n        await self.stop(flatten=False)\n''',
)

# Replace the single status() telemetry call that still references gemini_shadow directly.
replace_once(
    '            "gemini_shadow": self.gemini_shadow.ranker.telemetry(),\n',
    '            "shadow_research": self.shadow_research.telemetry(),\n',
)

# The engine must no longer know concrete Gemini/Spawn implementations.
for forbidden in ("GeminiShadowBridge", "GeminiShadowRanker", "SpawnShadowCoordinator", "self.gemini_shadow", "self.spawn_coordinator"):
    if forbidden in text:
        raise SystemExit(f"direct research ownership remains in engine.py: {forbidden}")

PATH.write_text(text, encoding="utf-8")
print("issue #276 engine cutover applied")
