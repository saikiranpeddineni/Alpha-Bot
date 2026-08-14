import asyncio
from datetime import datetime, UTC, timedelta
from backend.app.data.synthetic import SyntheticProvider
import time

async def main():
    sp = SyntheticProvider()
    t0 = time.time()
    now = datetime(2026,1,5,10,0,tzinfo=UTC)
    for i in range(100):
        await sp.get_bars('SPY', '1Min', now - timedelta(days=20), now)
        now += timedelta(minutes=1)
    print(f'100 calls took {time.time()-t0:.2f}s')

asyncio.run(main())
