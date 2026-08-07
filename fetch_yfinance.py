import yfinance as yf

symbols = ['SKHY', 'DDOG', 'GOOGL', 'NBIS', 'PLTR']

for sym in symbols:
    print(f"Fetching data for {sym}...")
    try:
        data = yf.download(sym, interval='1m', period='1d')
        print(data.head(5))
        print(data.tail(5))
    except Exception as e:
        print(f"Error fetching {sym}: {e}")
