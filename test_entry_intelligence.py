import sys
from pathlib import Path
sys.path.append(str(Path("backend").resolve()))

import datetime
import pandas as pd
from app.core.domain import Signal, SignalAction
from app.strategies.entry_intelligence import _entry_location_assessment

def create_df():
    data = []
    # 20 bars of history
    for i in range(20):
        data.append({
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000,
            "atr14": 1.0,
            "vwap": 100.0
        })
    # Add a huge exhaustion bar
    data.append({
        "open": 100.5,
        "high": 105.0,
        "low": 100.5,
        "close": 102.5,  # huge upper wick
        "volume": 5000,  # volume climax
        "atr14": 1.0,
        "vwap": 100.2
    })
    return pd.DataFrame(data)

def test_skhy_exhaustion():
    df = create_df()
    sig = Signal(
        timestamp=datetime.datetime(2026, 8, 13, 9, 30, tzinfo=datetime.timezone.utc),
        symbol="SKHY",
        action=SignalAction.ENTER_LONG,
        side="long",
        strategy="momentum",
        reason="test",
        confidence=1.0,
        mover_bias="gainer"
    )
    
    extension, reasons, evidence = _entry_location_assessment(sig, df)
    print("\nExhaustion Check:")
    print("Extension Penalty:", extension)
    print("Reasons:", reasons)
    
def test_skhy_continuation():
    df = create_df()
    # Add a nice retest setup
    df.iloc[-1] = {
        "open": 101.0,
        "high": 101.5,
        "low": 100.5,
        "close": 101.2,
        "volume": 2000,
        "atr14": 1.0,
        "vwap": 100.5
    }
    # Modify -2 and -3 to be decreasing
    df.iloc[-3, df.columns.get_loc('close')] = 101.0
    df.iloc[-2, df.columns.get_loc('close')] = 100.8
    df.iloc[-2, df.columns.get_loc('volume')] = 500
    
    sig = Signal(
        timestamp=datetime.datetime(2026, 8, 13, 9, 30, tzinfo=datetime.timezone.utc),
        symbol="SKHY",
        action=SignalAction.ENTER_LONG,
        side="long",
        strategy="momentum",
        reason="test",
        confidence=1.0,
        mover_bias="gainer"
    )
    
    extension, reasons, evidence = _entry_location_assessment(sig, df)
    print("\nContinuation Check:")
    print("Extension Penalty:", extension)
    print("Reasons:", reasons)

if __name__ == "__main__":
    test_skhy_exhaustion()
    test_skhy_continuation()
