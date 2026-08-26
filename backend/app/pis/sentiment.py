import re


class NewsSentimentAnalyzer:
    """Lightweight keyword-based sentiment analyzer for ultra-fast trading execution."""

    def __init__(self):
        self.bullish_keywords = {
            "surge", "surges", "soar", "soars", "beat", "beats", "lift", "lifts", "jump", "jumps",
            "upgrade", "upgrades", "approval", "approves", "breakout", "rally", "rallies",
            "buy", "bullish", "record", "growth", "win", "wins", "partner", "partners", "acquire",
            "acquires", "launch", "launches", "expand", "expands"
        }

        self.bearish_keywords = {
            "fall", "falls", "drop", "drops", "plunge", "plunges", "miss", "misses", "downgrade",
            "downgrades", "probe", "probes", "investigation", "sue", "sues", "lawsuit", "sell",
            "bearish", "crash", "crashes", "cut", "cuts", "delay", "delays", "fail", "fails",
            "halt", "halts", "bankrupt", "bankruptcy", "short", "shorts"
        }

        self.negation_words = {"not", "no", "never", "without", "despite"}

    def analyze_headline(self, text: str) -> float:
        """Returns a sentiment score between -1.0 and +1.0 for a given headline."""
        if not text:
            return 0.0

        words = re.findall(r'\b\w+\b', text.lower())
        score = 0.0

        for i, word in enumerate(words):
            multiplier = 1.0

            # Check for negation in the previous 2 words
            if i > 0 and words[i-1] in self.negation_words:
                multiplier = -1.0
            elif i > 1 and words[i-2] in self.negation_words:
                multiplier = -0.5

            if word in self.bullish_keywords:
                score += (1.0 * multiplier)
            elif word in self.bearish_keywords:
                score -= (1.0 * multiplier)

        # Normalize score between -1.0 and 1.0 (tanh-like compression for raw counts)
        # 1 strong keyword = 0.76, 2 = 0.96
        import math
        normalized = math.tanh(score)
        return round(normalized, 2)

    def analyze_headlines(self, headlines: list[str]) -> float:
        """Aggregates sentiment across multiple headlines (weighted heavily toward recent)."""
        if not headlines:
            return 0.0

        total_score = 0.0
        weight = 1.0
        total_weight = 0.0

        # Assume headlines are sorted newest first
        for text in headlines:
            s = self.analyze_headline(text)
            total_score += (s * weight)
            total_weight += weight
            weight *= 0.8  # Decay older headlines

        if total_weight == 0:
            return 0.0

        return round(total_score / total_weight, 2)
