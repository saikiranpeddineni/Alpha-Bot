import yfinance as yf
import pandas as pd

trades = {
    'SKHY': ('2026-08-06 11:14:00-04:00', '2026-08-06 11:16:00-04:00'),
    'DDOG': ('2026-08-06 10:58:00-04:00', '2026-08-06 11:13:00-04:00'),
    'GOOGL': ('2026-08-06 10:41:00-04:00', '2026-08-06 10:56:00-04:00'),
    'NBIS': ('2026-08-06 10:28:00-04:00', '2026-08-06 10:50:00-04:00'),
    'PLTR': ('2026-08-06 10:10:00-04:00', '2026-08-06 10:17:00-04:00'),
}

for sym, (start, end) in trades.items():
    print(f"\n==================== {sym} ====================")
    try:
        data = yf.download(sym, interval='1m', start='2026-08-06', end='2026-08-07')
        mask = (data.index >= start) & (data.index <= end)
        print(data[mask])
    except Exception as e:
        print(f"Error fetching {sym}: {e}")
