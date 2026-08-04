import asyncio
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))

from app.config import get_settings
from app.data.alpaca_data import AlpacaDataProvider

async def check_all_sentiment():
    settings = get_settings()
    provider = AlpacaDataProvider(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret
    )
    
    symbols = ["INTC", "CRWV", "NBIS", "NOW", "SPCX", "META", "TQQQ", "AMZN", "BABA", "NVDA"]
    results = {}
    for sym in symbols:
        try:
            score = await provider.get_news_sentiment(sym)
            results[sym] = score
            print(f"{sym:<6}: {score}")
        except Exception as e:
            print(f"{sym:<6}: Error {e}")
            results[sym] = 0.0
            
    print("\nSummary Dictionary:", results)

if __name__ == "__main__":
    asyncio.run(check_all_sentiment())
