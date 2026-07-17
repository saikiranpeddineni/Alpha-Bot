import asyncio
import sys
import httpx
import json

sys.path.append("backend")
from app.config import Settings

async def test_gemini():
    settings = Settings()
    api_key = settings.gemini_api_key
    print(f"API Key read: {api_key[:10]}... (length: {len(api_key)})")
    
    gemini_models = ["gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash"]
    payload = {
        "mode": "shadow_trade_ranking",
        "instruction": "Rank only supplied candidates. Return JSON rankings only.",
        "candidates": [
            {"symbol": "AAPL", "action": "ENTER_LONG", "strategy": "momentum", "ml_probability": 0.62, "trade_score": 85.0}
        ]
    }
    
    # Define structured response schema
    schema = {
        "type": "OBJECT",
        "properties": {
            "rankings": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "symbol": {"type": "STRING"},
                        "score": {"type": "INTEGER"}
                    },
                    "required": ["symbol", "score"]
                }
            }
        },
        "required": ["rankings"]
    }
    
    for model in gemini_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        body = {
            "contents": [{"parts": [{"text": json.dumps(payload)}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 256,
                "responseMimeType": "application/json",
                "responseSchema": schema
            }
        }
        try:
            print(f"\nTrying model: {model}...")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=body)
                print(f"Response status: {response.status_code}")
                if response.status_code == 200:
                    data = response.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    print(f"Success! Response content: {text}")
                    return
                else:
                    print(f"Failed with body: {response.text}")
        except Exception as e:
            print(f"Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_gemini())
