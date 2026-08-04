import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_API_SECRET")

url = "https://data.alpaca.markets/v2/stocks/NBIS/quotes/latest"
headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}

response = requests.get(url, headers=headers)
if response.status_code == 200:
    quote = response.json().get('quote', {})
    print(f"REAL-TIME NBIS QUOTE:")
    print(f"Bid: ${quote.get('bp')} | Ask: ${quote.get('ap')}")
    print(f"Timestamp: {quote.get('t')}")
else:
    print("Error:", response.text)
