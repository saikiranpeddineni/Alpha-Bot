"""Synthetic market data provider -- powers the zero-setup Demo mode.

Generates realistic regime-switching intraday price action, deterministic per
(symbol, date) so demo backtests are reproducible. In live-demo operation a
virtual session clock advances one 1-minute bar per engine poll, giving a
time-compressed but structurally faithful market experience with no network
access and no API keys.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.core.domain import Quote
from app.data.base import DataProvider, empty_bars, normalize_bars, resample_bars

ET = ZoneInfo("America/New_York")
MINUTES_PER_SESSION = 390  # 9:30 -> 16:00

BASE_PRICES: dict[str, float] = {
    "SPY": 560.0, "QQQ": 480.0, "IWM": 220.0, "DIA": 420.0,
    "AAPL": 230.0, "MSFT": 430.0, "NVDA": 135.0, "TSLA": 250.0,
    "AMD": 160.0, "META": 560.0, "AMZN": 200.0, "GOOGL": 175.0,
    "NFLX": 700.0, "COIN": 240.0, "SMCI": 45.0, "PLTR": 60.0,
}


def _seed(*parts: object) -> int:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def base_price(symbol: str) -> float:
    if symbol in BASE_PRICES:
        return BASE_PRICES[symbol]
    return 30.0 + (_seed("price", symbol) % 47000) / 100.0


def _daily_vol(symbol: str) -> float:
    """Deterministic per-symbol daily volatility between ~0.8% and ~3%."""
    return 0.008 + (_seed("vol", symbol) % 1000) / 1000.0 * 0.022


def _base_minute_volume(symbol: str) -> float:
    return 20_000.0 + (_seed("volume", symbol) % 480_000)


def _day_anchor(symbol: str, d: date) -> float:
    """Deterministic opening level: slow weekly wander + daily gap around the base."""
    weekly = np.random.default_rng(_seed("week", symbol, d.toordinal() // 5)).normal(0.0, 0.03)
    daily = np.random.default_rng(_seed("gap", symbol, d)).normal(0.0, 0.008)
    return base_price(symbol) * float(np.exp(weekly + daily))


def generate_day(symbol: str, d: date) -> pd.DataFrame:
    """One full deterministic 390-bar session of 1-minute OHLCV for (symbol, date)."""
    rng = np.random.default_rng(_seed("day", symbol, d))
    n = MINUTES_PER_SESSION
    sigma_1m = _daily_vol(symbol) / np.sqrt(n)
    day_vol_mult = float(rng.lognormal(0.0, 0.35))

    # Regime-switching drift/vol in 30-90 minute blocks
    mu = np.zeros(n)
    sig = np.full(n, sigma_1m)
    i = 0
    while i < n:
        block = int(rng.integers(30, 91))
        direction = rng.choice([-1.0, 0.0, 0.0, 1.0])  # bias toward chop
        strength = float(rng.uniform(0.2, 1.0))
        mu[i : i + block] = direction * strength * sigma_1m * 0.6
        sig[i : i + block] = sigma_1m * float(rng.uniform(0.7, 2.0))
        i += block
    sig *= day_vol_mult

    rets = rng.normal(mu, sig)

    # Occasional news-style jump with a volume spike
    jump_minute = -1
    if rng.random() < 0.18:
        jump_minute = int(rng.integers(15, n - 20))
        rets[jump_minute] += float(rng.choice([-1.0, 1.0]) * rng.uniform(2.5, 5.0) * sigma_1m * day_vol_mult)

    anchor = _day_anchor(symbol, d)
    closes = anchor * np.exp(np.cumsum(rets))
    opens = np.empty(n)
    opens[0] = anchor
    opens[1:] = closes[:-1]

    intrabar = np.abs(rng.normal(0.0, sig)) * 0.8
    hi = np.maximum(opens, closes) * (1.0 + intrabar)
    lo = np.minimum(opens, closes) * (1.0 - intrabar)

    # U-shaped volume profile with lognormal noise
    t = np.arange(n, dtype=float)
    profile = 1.0 + 1.6 * np.exp(-t / 25.0) + 1.1 * np.exp(-(n - 1 - t) / 35.0)
    volume = _base_minute_volume(symbol) * profile * rng.lognormal(0.0, 0.35, n)
    if jump_minute >= 0:
        volume[jump_minute : jump_minute + 5] *= 3.0

    start_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    idx = pd.date_range(start=start_et, periods=n, freq="1min").tz_convert(UTC)
    df = pd.DataFrame(
        {"open": opens, "high": hi, "low": lo, "close": closes, "volume": np.round(volume)},
        index=idx,
    )
    df.index.name = "ts"
    return df


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _weekdays_between(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


class SyntheticProvider(DataProvider):
    """Deterministic offline market. `advance()` steps the shared virtual clock."""

    name = "synthetic"

    def __init__(self, lookback_days: int = 5, start_minute: int = 90) -> None:
        self.lookback_days = lookback_days
        today = datetime.now(ET).date()
        self._virtual_date: date = today if today.weekday() < 5 else _prev_weekday(today)
        self._minute_idx: int = min(start_minute, MINUTES_PER_SESSION - 1)
        self._live: dict[str, pd.DataFrame] = {}
        self._today_frames: dict[str, pd.DataFrame] = {}
        self._max_live_rows = MINUTES_PER_SESSION * (lookback_days + 2)

    # ---- virtual clock ----

    def _ensure(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol in self._live:
            return
        days: list[date] = []
        d = self._virtual_date
        for _ in range(self.lookback_days):
            d = _prev_weekday(d)
            days.append(d)
        days.reverse()
        frames = [generate_day(symbol, day) for day in days]
        today_frame = generate_day(symbol, self._virtual_date)
        self._today_frames[symbol] = today_frame
        frames.append(today_frame.iloc[: self._minute_idx + 1])
        self._live[symbol] = pd.concat(frames)

    def advance(self) -> None:
        """Append the next virtual 1-minute bar for every tracked symbol."""
        if not self._live:
            return
        self._minute_idx += 1
        if self._minute_idx >= MINUTES_PER_SESSION:
            self._virtual_date = _next_weekday(self._virtual_date)
            self._minute_idx = 0
            for symbol in self._live:
                self._today_frames[symbol] = generate_day(symbol, self._virtual_date)
        for symbol in list(self._live):
            bar = self._today_frames[symbol].iloc[[self._minute_idx]]
            merged = pd.concat([self._live[symbol], bar])
            if len(merged) > self._max_live_rows:
                merged = merged.iloc[-self._max_live_rows :]
            self._live[symbol] = merged

    # ---- DataProvider interface ----

    async def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        symbol = symbol.upper()
        if start is not None:
            end = end or datetime.now(UTC)
            days = _weekdays_between(start.astimezone(ET).date(), end.astimezone(ET).date())
            if not days:
                return empty_bars()
            df = pd.concat([generate_day(symbol, d) for d in days])
            df = df[(df.index >= start) & (df.index <= end)]
        else:
            self._ensure(symbol)
            df = self._live[symbol]
        df = normalize_bars(df)
        df = resample_bars(df, timeframe)
        if limit:
            df = df.iloc[-limit:]
        return df

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in symbols:
            symbol = symbol.upper()
            self._ensure(symbol)
            df = self._live[symbol]
            if df.empty:
                continue
            last = df.iloc[-1]
            session = df.index.tz_convert(ET).date
            today_mask = session == session[-1]
            day_open = float(df.loc[today_mask, "open"].iloc[0])
            price = float(last["close"])
            out[symbol] = Quote(
                symbol=symbol,
                price=round(price, 2),
                change_pct=round((price / day_open - 1.0) * 100.0, 3) if day_open else 0.0,
                volume=float(df.loc[today_mask, "volume"].sum()),
                ts=df.index[-1].to_pydatetime(),
            )
        return out
