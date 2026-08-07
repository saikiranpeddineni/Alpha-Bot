import sys
sys.path.append('c:/Users/peddi/Desktop/Bots/Bot_QA/backend')
from app.db.database import Database, TradeRow
from sqlalchemy import select
from datetime import datetime, UTC, timedelta
import json
import logging
logging.basicConfig(level=logging.INFO)

db = Database(Path('c:/Users/peddi/Desktop/Bots/Bot_QA/data/alphadesk.db'))
now = datetime.now(UTC)
cutoff_24h = now - timedelta(hours=24)

with db._session() as s:
    stmt = select(TradeRow).where(TradeRow.exit_at >= cutoff_24h)
    rows = s.scalars(stmt).all()
    
    total_volume = 0.0
    for r in rows:
        try:
            trade_data = json.loads(r.payload)
        except Exception:
            continue
        entry_price = trade_data.get("entry_price", 0.0)
        qty = trade_data.get("qty", 0.0)
        total_volume += abs(qty * entry_price)
    
    print(f"Total rows matched: {len(rows)}")
    print(f"Total Volume: {total_volume}")
