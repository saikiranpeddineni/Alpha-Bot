import sys
sys.path.insert(0, "backend")
from app.replay.market_feed import ReplayMarketFeed

feed = ReplayMarketFeed()
try:
    bars = feed.load_bars("2026-08-12", "2026-08-12")
    print(f"Loaded {len(bars)} bars")
    vix_bars = [b for b in bars if b["symbol"] in ("VIX", "^VIX")]
    print(f"VIX bars: {len(vix_bars)}")
except Exception as e:
    print(f"Error: {e}")
