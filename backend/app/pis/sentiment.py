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
    """Deterministic event-aware headline analyzer used by the live trading path."""

    def __init__(self):
        self.bullish_keywords = {
            "surge", "surges", "soar", "soars", "beat", "beats", "lift", "lifts", "jump", "jumps",
            "upgrade", "upgrades", "approval", "approves", "approved", "breakout", "rally", "rallies",
            "buy", "bullish", "record", "growth", "win", "wins", "partner", "partners", "acquire",
            "acquires", "launch", "launches", "expand", "expands", "raises", "raise", "guidance",
            "contract", "award", "awarded", "deal", "profit", "profits", "revenue", "outperform",
            "strong", "strength", "accelerates", "accelerate", "rebound", "recovery", "backlog",
        }
        self.bearish_keywords = {
            "fall", "falls", "drop", "drops", "plunge", "plunges", "miss", "misses", "downgrade",
            "downgrades", "probe", "probes", "investigation", "sue", "sues", "lawsuit", "sell",
            "bearish", "crash", "crashes", "cut", "cuts", "delay", "delays", "fail", "fails",
            "halt", "halts", "bankrupt", "bankruptcy", "short", "shorts", "dilution", "dilute",
            "offering", "secondary", "recall", "warning", "warns", "missed", "fraud", "resigns",
            "resignation", "layoffs", "layoff", "loss", "losses", "underperform", "weak", "weakness",
            "slump", "slumps", "decline", "declines", "reduces", "reduce", "withdraw", "default",
        }
        self.negation_words = {"not", "no", "never", "without", "denies", "deny", "denied", "rules", "rule"}
        self.contrast_words = {"but", "however", "despite", "although", "while", "yet"}
        self.catalyst_keywords = {
            "earnings": {"earnings", "revenue", "profit", "profits", "eps", "guidance", "forecast", "quarter"},
            "financing": {"dilution", "dilute", "offering", "secondary", "convertible", "financing", "shares", "capital raise"},
            "regulatory": {"approval", "approved", "probe", "investigation", "regulator", "regulatory", "lawsuit", "recall", "fda", "sec"},
            "corporate": {"acquire", "acquires", "acquisition", "merger", "deal", "contract", "award", "partner", "partnership"},
            "analyst": {"upgrade", "downgrade", "outperform", "underperform", "price target", "analyst"},
        }
        self.bullish_phrases = {
            "beats estimates": 1.4,
            "beats expectations": 1.4,
            "raises guidance": 1.5,
            "raises outlook": 1.5,
            "strong guidance": 1.2,
            "record revenue": 1.2,
            "record profit": 1.2,
            "positive outlook": 1.0,
            "wins contract": 1.0,
            "receives approval": 1.1,
            "no dilution": 1.2,
            "denies dilution": 1.2,
            "denies reports": 0.7,
            "shares rise": 0.8,
        }
        self.bearish_phrases = {
            "misses estimates": 1.4,
            "misses expectations": 1.4,
            "cuts guidance": 1.5,
            "lowers guidance": 1.5,
            "lowers outlook": 1.5,
            "weak outlook": 1.2,
            "going concern": 1.6,
            "secondary offering": 1.3,
            "stock offering": 1.1,
            "shares offered": 1.1,
            "regulatory investigation": 1.4,
            "fraud investigation": 1.6,
            "recall announced": 1.1,
            "shares fall": 0.8,
        }

    def _phrase_score(self, lowered: str) -> float:
        score = 0.0
        for phrase, weight in self.bullish_phrases.items():
            if phrase in lowered:
                score += weight
        for phrase, weight in self.bearish_phrases.items():
            if phrase in lowered:
                score -= weight
        return score

    def analyze_headline(self, text: str) -> float:
        """Return deterministic event-aware sentiment in [-1, 1]."""
        if not text:
            return 0.0
        lowered = text.lower()
        words = re.findall(r"\b\w+\b", lowered)
        score = self._phrase_score(lowered)
        clause_score = 0.0
        clause_weight = 1.0
        for i, word in enumerate(words):
            if word in self.contrast_words:
                clause_weight = 1.25
                continue
            multiplier = clause_weight
            if i > 0 and words[i - 1] in self.negation_words:
                multiplier *= -1.0
            elif i > 1 and words[i - 2] in self.negation_words:
                multiplier *= -0.5
            if word in self.bullish_keywords:
                clause_score += multiplier
            elif word in self.bearish_keywords:
                clause_score -= multiplier
            clause_weight = 1.0
        score += 0.55 * clause_score
        return round(math.tanh(score), 2)

    def catalyst_for_headline(self, text: str) -> str | None:
        lowered = (text or "").lower()
        priority = ("financing", "regulatory", "earnings", "corporate", "analyst")
        matches = {
            catalyst: sum(1 for keyword in keywords if keyword in lowered)
            for catalyst, keywords in self.catalyst_keywords.items()
        }
        candidates = [c for c in priority if matches[c] > 0]
        return max(candidates, key=lambda c: matches[c]) if candidates else None

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
        normalized: list[tuple[datetime | None, str]] = []
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
            # Missing timestamps are still valid contextual evidence, but they
            # must never be fabricated as "now" and therefore cannot become a
            # fresh-catalyst veto solely because the provider omitted metadata.
            normalized.append((ts, headline))

        normalized.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=UTC), reverse=True)
        headlines = [headline for _, headline in normalized[:10]]
        ages = [max(0.0, (now - ts).total_seconds()) for ts, _ in normalized if ts is not None]
        fresh_count = sum(age <= 6 * 3600 for age in ages)
        newest_age = min(ages) if ages else None
        score = self.analyze_headlines(headlines)
        direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"

        per_headline = [self.analyze_headline(h) for h in headlines[:5]]
        non_neutral = [s for s in per_headline if abs(s) >= 0.10]
        agreement = 0.0
        if non_neutral:
            same_direction = sum(1 for s in non_neutral if (s > 0) == (score > 0))
            agreement = same_direction / len(non_neutral)
        freshness_factor = min(1.0, fresh_count / 3.0)
        magnitude_factor = min(1.0, abs(score))
        confidence = min(1.0, 0.45 * magnitude_factor + 0.35 * agreement + 0.20 * freshness_factor)

        catalyst = None
        catalyst_scores: dict[str, float] = {}
        for headline in headlines:
            catalyst_name = self.catalyst_for_headline(headline)
            if catalyst_name:
                catalyst_scores[catalyst_name] = catalyst_scores.get(catalyst_name, 0.0) + 1.0
        if catalyst_scores:
            catalyst = max(catalyst_scores, key=catalyst_scores.get)

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
