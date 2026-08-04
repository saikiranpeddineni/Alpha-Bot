import asyncio
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))

from app.config import get_settings
from app.data.alpaca_data import AlpacaDataProvider

async def check_sentiment():
    settings = get_settings()
    provider = AlpacaDataProvider(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret
    )
    
    for sym in ["INTC", "IREN"]:
        score = await provider.get_news_sentiment(sym)
        print(f"News Sentiment Score for {sym}: {score}")

if __name__ == "__main__":
    asyncio.run(check_sentiment())
