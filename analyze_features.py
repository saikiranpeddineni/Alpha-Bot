import json
import pandas as pd

def analyze_features(symbol, ledger):
    print(f"\n{'='*50}\nAnalyzing Features for {symbol}\n{'='*50}")
    
    trades = [t['details'] for t in ledger.get('trades', []) if t.get('symbol') == symbol]
    signals = [s['details'] for s in ledger.get('signals', []) if s.get('symbol') == symbol]
    
    for i, t in enumerate(trades):
        print(f"\nTrade {i+1}: {t['side']} at {t['entry_at']} | PNL: ${t.get('pnl', 0):.2f}")
        trade_signal = [s for s in signals if s.get('timestamp') and s['timestamp'] <= t['entry_at']][-1:]
        if trade_signal:
            feats = trade_signal[0].get('features', {})
            # Let's print some important features
            important_keys = [
                'trend_z', 'vol_z', 'macd_hist_1m', 'rsi_1m', 'rsi_5m', 'atr_pct_1m',
                'vwap_dist_pct', 'regime', 'rs_score'
            ]
            
            # Print features that have high absolute values (z-scores > 1.5) or are explicitly requested
            for k, v in feats.items():
                if any(x in k.lower() for x in important_keys):
                    print(f"  {k}: {v}")
                elif isinstance(v, (int, float)) and abs(v) > 2.0:
                    print(f"  {k} (High Z): {v}")

if __name__ == '__main__':
    ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))
    analyze_features('AMD', ledger)
    analyze_features('NBIS', ledger)
