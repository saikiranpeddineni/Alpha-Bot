import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_API_SECRET")

url = "https://data.alpaca.markets/v1beta1/news?symbols=NBIS&limit=5"
headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}

response = requests.get(url, headers=headers)
print("Status Code:", response.status_code)
if response.status_code == 200:
    news = response.json().get('news', [])
    for n in news:
        print(f"[{n['created_at']}] {n['headline']}")
else:
    print(response.text)
