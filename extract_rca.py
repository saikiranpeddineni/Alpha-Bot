import json
import os
import sys

# Find the latest replay ledger
replays_dir = 'data/replays/'
replays = sorted(os.listdir(replays_dir), key=lambda x: os.path.getmtime(os.path.join(replays_dir, x)), reverse=True)
latest_replay = replays[0]
ledger_path = os.path.join(replays_dir, latest_replay, 'ledger.json')

with open(ledger_path) as f:
    ledger = json.load(f)

def generate_rca(symbol):
    print(f"\n{symbol} OPPORTUNITY RCA")
    print("-" * 50)
    
    # Discovery / Candidate
    cands = [c for c in ledger.get('candidates', []) if c.get('symbol') == symbol]
    if cands:
        print(f"Discovery: Discovered and added to candidate pool. ({len(cands)} candidate events)")
    else:
        print("Discovery: Failed to discover candidate.")
        
    # Signals
    signals = [s for s in ledger.get('signals', []) if s.get('symbol') == symbol]
    if signals:
        first_sig = signals[0]
        det = first_sig.get('details', {})
        print(f"Signal: {det.get('strategy')} ({det.get('action')}) generated at {first_sig.get('timestamp')}")
        print(f"Market Regime: {det.get('regime', 'UNKNOWN')}")
        # Note: sector and other context might be in candidate details or signal details
    else:
        print("Signal: No strategy signal generated.")
        
    # ML Decisions
    ml_decisions = [m for m in ledger.get('ml_decisions', []) if m.get('symbol') == symbol]
    if ml_decisions:
        first_ml = ml_decisions[0]
        det = first_ml.get('details', {})
        gate_evidence = det.get('gate_evidence', {})
        
        # News/Catalyst
        news_score = gate_evidence.get('news_sentiment_score', 'UNKNOWN')
        catalyst = gate_evidence.get('has_catalyst', 'UNKNOWN')
        print(f"News Sentiment: {news_score}")
        print(f"Catalyst Identified: {catalyst}")
        
        # Sector
        print(f"Sector Confirmation: {gate_evidence.get('sector_strength', 'UNKNOWN')}")
        
        # Price/Mover
        print(f"Mover Bias: {gate_evidence.get('mover_bias', 'UNKNOWN')}")
        
        # ML
        print(f"ML Probability: {det.get('ml_probability')}")
        print(f"Meta Probability: {det.get('meta_probability')}")
        print(f"TradeScore: {det.get('trade_score')}")
        
        print(f"ML Gate Decision: {det.get('ml_gate_decision')}")
        print(f"Rejection Reason (ML): {gate_evidence.get('rejection_reason', 'None')}")
    else:
        print("ML/Meta/TradeScore: No ML decision record.")
        
    # Risk
    risk_decisions = [r for r in ledger.get('risk_decisions', []) if r.get('symbol') == symbol]
    if risk_decisions:
        for r in risk_decisions:
            print(f"Risk Decision: {r.get('details', {}).get('decision')} - {r.get('details', {}).get('reason')}")
    else:
        print("Risk: No risk decision record.")
        
    # Orders
    orders = [o for o in ledger.get('orders', []) if o.get('symbol') == symbol]
    if orders:
        print(f"Orders created: {len(orders)}")
    else:
        print("Orders: No orders created.")
        
    # Full details
    print("\n--- DETAILED DUMP ---")
    if signals:
        print("First Signal:")
        print(json.dumps(signals[0], indent=2))
    if ml_decisions:
        print("First ML Decision:")
        print(json.dumps(ml_decisions[0], indent=2))
    if risk_decisions:
        print("First Risk Decision:")
        print(json.dumps(risk_decisions[0], indent=2))


print(f"Using ledger from: {latest_replay}")
generate_rca('NBIS')
generate_rca('COHR')
