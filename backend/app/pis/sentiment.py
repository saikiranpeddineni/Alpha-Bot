from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class NewsContext:
    """Structured, symbol-specific news evidence for an entry/position decision."""

    sentiment: float = 0.0
    direction: str = "neutral"
    confidence: float = 0.0
    headline_count: int = 0
    fresh_headline_count: int = 0
    newest_age_seconds: float | None = None
    catalyst: str | None = None
    headlines: tuple[str, ...] = ()


class NewsSentimentAnalyzer:
    """Lightweight deterministic headline analyzer used by the live trading path."""

    def __init__(self):
        self.bullish_keywords = {
            "surge", "surges", "soar", "soars", "beat", "beats", "lift", "lifts", "jump", "jumps",
            "upgrade", "upgrades", "approval", "approves", "breakout", "rally", "rallies",
            "buy", "bullish", "record", "growth", "win", "wins", "partner", "partners", "acquire",
            "acquires", "launch", "launches", "expand", "expands", "raises", "raise", "guidance",
            "contract", "award", "awarded", "deal", "profit", "profits", "revenue", "outperform",
        }
        self.bearish_keywords = {
            "fall", "falls", "drop", "drops", "plunge", "plunges", "miss", "misses", "downgrade",
            "downgrades", "probe", "probes", "investigation", "sue", "sues", "lawsuit", "sell",
            "bearish", "crash", "crashes", "cut", "cuts", "delay", "delays", "fail", "fails",
            "halt", "halts", "bankrupt", "bankruptcy", "short", "shorts", "dilution", "dilute",
            "offering", "secondary", "recall", "warning", "warns", "guidance cut", "missed",
            "fraud", "resigns", "resignation", "layoffs", "layoff", "loss", "losses", "underperform",
        }
        self.negation_words = {"not", "no", "never", "without", "despite"}
        self.catalyst_keywords = {
            "earnings": {"earnings", "revenue", "profit", "guidance", "forecast"},
            "financing": {"dilution", "offering", "secondary", "convertible", "financing", "shares"},
            "regulatory": {"approval", "approved", "probe", "investigation", "regulator", "lawsuit", "recall"},
            "corporate": {"acquire", "acquires", "merger", "deal", "contract", "award", "partner"},
            "analyst": {"upgrade", "downgrade", "outperform", "underperform", "price target"},
        }

    def analyze_headline(self, text: str) -> float:
        """Return a deterministic sentiment score in [-1, 1]."""
        if not text:
            return 0.0
        words = re.findall(r"\b\w+\b", text.lower())
        score = 0.0
        for i, word in enumerate(words):
            multiplier = 1.0
            if i > 0 and words[i - 1] in self.negation_words:
                multiplier = -1.0
            elif i > 1 and words[i - 2] in self.negation_words:
                multiplier = -0.5
            if word in self.bullish_keywords:
                score += multiplier
            elif word in self.bearish_keywords:
                score -= multiplier
        return round(math.tanh(score), 2)

    def catalyst_for_headline(self, text: str) -> str | None:
        lowered = (text or "").lower()
        for catalyst, keywords in self.catalyst_keywords.items():
            if any(keyword in lowered for keyword in keywords):
                return catalyst
        return None

    def analyze_headlines(self, headlines: list[str]) -> float:
        if not headlines:
            return 0.0
        total_score = 0.0
        total_weight = 0.0
        weight = 1.0
        for text in headlines:
            total_score += self.analyze_headline(text) * weight
            total_weight += weight
            weight *= 0.8
        return round(total_score / total_weight, 2) if total_weight else 0.0

    def build_context(self, items: list[dict], now: datetime | None = None) -> NewsContext:
        now = now or datetime.now(UTC)
        normalized: list[tuple[datetime, str]] = []
        for item in items:
            headline = str(item.get("headline") or "").strip()
            if not headline:
                continue
            raw_ts = item.get("created_at") or item.get("updated_at")
            ts = None
            if raw_ts:
                try:
                    ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    ts = ts.astimezone(UTC)
                except (TypeError, ValueError):
                    ts = None
            if ts is None:
                ts = now
            normalized.append((ts, headline))

        normalized.sort(key=lambda pair: pair[0], reverse=True)
        headlines = [headline for _, headline in normalized[:10]]
        ages = [max(0.0, (now - ts).total_seconds()) for ts, _ in normalized]
        fresh_count = sum(age <= 6 * 3600 for age in ages)
        newest_age = ages[0] if ages else None
        score = self.analyze_headlines(headlines)
        direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
        confidence = min(1.0, abs(score) * 0.75 + min(fresh_count, 5) * 0.05)
        catalyst = None
        for headline in headlines:
            catalyst = self.catalyst_for_headline(headline)
            if catalyst:
                break
        return NewsContext(
            sentiment=score,
            direction=direction,
            confidence=round(confidence, 2),
            headline_count=len(headlines),
            fresh_headline_count=fresh_count,
            newest_age_seconds=newest_age,
            catalyst=catalyst,
            headlines=tuple(headlines),
        )
