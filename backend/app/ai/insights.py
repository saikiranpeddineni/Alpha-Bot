"""AI insight generation.

Turns engine state (regimes, gated signals, risk posture, performance,
model status) into human-readable insight cards for the UI. Fully local;
if an Anthropic API key is configured, a compact analyst narrative is
additionally generated via Claude -- strictly optional.
"""

from __future__ import annotations

from collections import deque

import httpx

from app.ai.regime import REGIME_LABELS
from app.core.domain import InsightItem, Regime, Signal
from app.core.logging import applog


class InsightEngine:
    def __init__(self, gemini_api_key: str = "", anthropic_api_key: str = "", max_items: int = 60, narrative_test_mode: bool = False) -> None:
        self.gemini_api_key = gemini_api_key
        self.anthropic_api_key = anthropic_api_key
        self.items: deque[InsightItem] = deque(maxlen=max_items)
        self.is_research_mode: bool = True
        self.narrative_test_mode: bool = narrative_test_mode

    def recent(self, limit: int = 30) -> list[InsightItem]:
        return list(self.items)[-limit:]

    def _add(self, item: InsightItem) -> InsightItem:
        self.items.append(item)
        return item

    # ---- generators (called by the engine) ----

    def on_regime_change(self, symbol: str, regime: Regime, details: dict) -> InsightItem:
        pieces = []
        if details.get("er") is not None:
            pieces.append(f"efficiency {details['er']:.2f}")
        if details.get("vol_percentile") is not None:
            pieces.append(f"vol percentile {details['vol_percentile'] * 100:.0f}%")
        severity = "warning" if regime == Regime.VOLATILE else "info"
        return self._add(
            InsightItem(
                kind="regime",
                symbol=symbol,
                title=f"{symbol}: {REGIME_LABELS[regime]}",
                detail=f"Regime shifted to {REGIME_LABELS[regime]} ({', '.join(pieces) or 'warmup'}). "
                f"Strategy weights adjusted.",
                severity=severity,
            )
        )

    def on_signal(self, signal: Signal, gated: bool, gate_reason: str = "") -> InsightItem:
        ml = f"{signal.ml_probability * 100:.0f}%" if signal.ml_probability is not None else "n/a"
        action = signal.action.replace("_", " ")
        if gated:
            title = f"{signal.symbol}: {action} candidate rejected"
            detail = (
                f"{signal.strategy} proposed {action} ({signal.reason}) with ML confidence {ml}, "
                f"but was rejected: {gate_reason}."
            )
            severity = "info"
        else:
            title = f"{signal.symbol}: {action} signal accepted"
            detail = (
                f"{signal.strategy}: {signal.reason}. ML confidence {ml}, "
                f"composite score {signal.score:.2f}." if signal.score is not None else
                f"{signal.strategy}: {signal.reason}. ML confidence {ml}."
            )
            severity = "success"
        return self._add(
            InsightItem(kind="signal", symbol=signal.symbol, title=title, detail=detail, severity=severity)
        )

    def on_risk_event(self, title: str, detail: str, severity: str = "warning") -> InsightItem:
        return self._add(InsightItem(kind="risk", title=title, detail=detail, severity=severity))

    def on_performance(self, stats: dict, day_pnl: float) -> InsightItem:
        pf = stats.get("profit_factor")
        detail = (
            f"Session P&L {day_pnl:+,.2f}. {stats['total_trades']} trades, "
            f"win rate {stats['win_rate']}%"
        )
        if pf is not None:
            detail += f", profit factor {pf}"
        detail += "."
        severity = "success" if day_pnl >= 0 else "warning"
        return self._add(InsightItem(kind="performance", title="Session performance", detail=detail, severity=severity))

    def on_model_status(self, info: dict) -> InsightItem:
        if info.get("trained"):
            title = "ML model active"
            detail = (
                f"Gradient boosting model trained {info.get('trained_at', '?')[:10]} on "
                f"{info.get('samples', '?')} samples (validation AUC {info.get('validation_auc')})."
            )
            severity = "success"
        else:
            title = "Heuristic AI gate active"
            detail = (
                "No trained model found -- signals are gated by a transparent momentum heuristic. "
                "Train a model from the AI panel for a data-driven gate."
            )
            severity = "info"
        return self._add(InsightItem(kind="model", title=title, detail=detail, severity=severity))

    # ---- optional LLM narrative ----

    async def llm_narrative(self, context: str) -> str | None:
        """One-paragraph analyst note via Gemini or Claude. Returns None when no keys are set."""
        if self.is_research_mode and not self.narrative_test_mode:
            return None

        if self.gemini_api_key:
            gemini_models = ["gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash"]
            last_exception = None
            for model in gemini_models:
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        resp = await client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_api_key}",
                            headers={"content-type": "application/json"},
                            json={
                                "contents": [
                                    {
                                        "parts": [
                                            {
                                                "text": (
                                                    "You are the analyst narrator inside a day-trading dashboard. "
                                                    "Write a tight 3-4 sentence market note based on this engine "
                                                    "state. No advice disclaimers, no hedging boilerplate.\n\n"
                                                    + context
                                                )
                                            }
                                        ]
                                    }
                                ]
                            },
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            if text:
                                self._add(
                                    InsightItem(kind="narrative", title="Analyst note", detail=text.strip(), severity="info")
                                )
                                return text.strip()
                except Exception as exc:  # noqa: BLE001
                    last_exception = exc
                    applog.warning("ai.insights", f"Gemini model {model} failed: {exc}")
                    continue

            # If Gemini fails, we can fall back to Anthropic if available
            if last_exception and not self.anthropic_api_key:
                applog.warning("ai.insights", f"All Gemini models failed. Last error: {last_exception}")
                return None

        if self.anthropic_api_key:
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": self.anthropic_api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json={
                            "model": "claude-3-5-haiku-latest",
                            "max_tokens": 300,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "You are the analyst narrator inside a day-trading dashboard. "
                                        "Write a tight 3-4 sentence market note based on this engine "
                                        "state. No advice disclaimers, no hedging boilerplate.\n\n"
                                        + context
                                    ),
                                }
                            ],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    text = "".join(b.get("text", "") for b in data.get("content", []))
                    if text:
                        self._add(
                            InsightItem(kind="narrative", title="Analyst note", detail=text.strip(), severity="info")
                        )
                        return text.strip()
            except Exception as exc:  # noqa: BLE001
                applog.warning("ai.insights", f"Anthropic narrative failed: {exc}")
                return None

        return None


