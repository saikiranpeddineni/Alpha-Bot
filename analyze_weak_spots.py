import json

def analyze_symbol(symbol, ledger):
    print(f"\n{'='*50}\nAnalyzing {symbol}\n{'='*50}")
    
    trades = [t['details'] for t in ledger.get('trades', []) if t.get('symbol') == symbol]
    ml_decisions = [d for d in ledger.get('ml_decisions', []) if d.get('symbol') == symbol]
    signals = [s for s in ledger.get('signals', []) if s.get('symbol') == symbol]
    timeline = [e for e in ledger.get('timeline', []) if e.get('symbol') == symbol]
    
    print(f"Total Trades: {len(trades)}")
    for i, t in enumerate(trades):
        print(f"\nTrade {i+1}: {t['side']} at {t['entry_at']} | PNL: ${t.get('pnl', 0):.2f} | R: {t.get('r_multiple', 0):.2f}")
        print(f"  Strategy: {t.get('strategy', 'unknown')}")
        print(f"  Exit: {t['exit_at']} | Reason: {t['exit_reason']}")
        
        # Find the signal that triggered this trade
        trade_signal = [s for s in signals if s.get('timestamp') and s['timestamp'] <= t['entry_at']][-1:]
        if trade_signal:
            s_det = trade_signal[0].get('details', {})
            print(f"  Signal Confidence: {s_det.get('confidence', 'N/A')}")
            print(f"  Signal Strength: {s_det.get('strength', 'N/A')}")
            if 'features' in s_det:
                print(f"  Signal Features: {s_det['features']}")
        
        trade_ml = [m for m in ml_decisions if m.get('timestamp') and m['timestamp'] <= t['entry_at']][-1:]
        if trade_ml:
            ml_det = trade_ml[0].get('details', {})
            print(f"  ML Prob: {ml_det.get('ml_probability', 'N/A')} | Meta Prob: {ml_det.get('meta_probability', 'N/A')}")
            print(f"  Gate Decision: {ml_det.get('ml_gate_decision', 'N/A')}")
            if 'gate_evidence' in ml_det:
                print(f"  Gate Evidence: {ml_det['gate_evidence']}")

        # Look for telemetry events just prior to exit to see why thesis degraded
        trade_timeline = [e for e in timeline if e.get('timestamp', '') >= t['entry_at'] and e.get('timestamp', '') <= t['exit_at']]
        degrade_events = [e for e in trade_timeline if e.get('event_type') == 'THESIS_DEGRADED' or 'degrad' in str(e).lower()]
        for e in degrade_events:
             print(f"  Degrade telemetry: {e.get('details', e)}")

if __name__ == '__main__':
    ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))
    analyze_symbol('AMD', ledger)
    analyze_symbol('NBIS', ledger)
