"""
Candidate-level forensic reconciliation for AMD and NBIS trades
in replay rpl_65a9996ef51c.

Goal: Extract every field the user requested from ACTUAL replay data.
Mark anything not directly sourced as NOT VERIFIED.
"""
import json
from pprint import pprint

ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))

SYMBOLS = ['AMD', 'NBIS']

# 1. What top-level keys exist in the ledger?
print("Ledger top-level keys:", list(ledger.keys()))
print()

# 2. For each symbol, extract ALL related events across every category
for sym in SYMBOLS:
    print(f"\n{'='*70}")
    print(f"  SYMBOL: {sym}")
    print(f"{'='*70}")
    
    # --- SIGNALS ---
    signals = [s for s in ledger.get('signals', []) if s.get('symbol') == sym]
    print(f"\n--- SIGNALS ({len(signals)}) ---")
    for i, s in enumerate(signals):
        det = s.get('details', {})
        print(f"\nSignal {i+1}:")
        print(f"  timestamp:     {s.get('timestamp')}")
        print(f"  event_type:    {s.get('event_type')}")
        print(f"  candidate_id:  {det.get('candidate_id')}")
        print(f"  strategy:      {det.get('strategy')}")
        print(f"  action:        {det.get('action')}")
        print(f"  price:         {det.get('price')}")
        print(f"  confidence:    {det.get('confidence')}")
        print(f"  ml_probability:{det.get('ml_probability')}")
        print(f"  ml_gate_decision: {det.get('ml_gate_decision')}")
        print(f"  score:         {det.get('score')}")
        print(f"  stop_loss:     {det.get('stop_loss')}")
        print(f"  take_profit:   {det.get('take_profit')}")
        print(f"  regime:        {det.get('regime')}")
        print(f"  scanner_score: {det.get('scanner_score')}")
        print(f"  scanner_auc:   {det.get('scanner_auc')}")
        print(f"  reason:        {det.get('reason')}")
        print(f"  discovery_source: {det.get('discovery_source')}")
        print(f"  risk_multiplier:  {det.get('risk_multiplier')}")
        # Print ALL keys to ensure we don't miss anything
        print(f"  ALL KEYS: {sorted(det.keys())}")

    # --- ML DECISIONS ---
    ml_decisions = [m for m in ledger.get('ml_decisions', []) if m.get('symbol') == sym]
    print(f"\n--- ML DECISIONS ({len(ml_decisions)}) ---")
    for i, m in enumerate(ml_decisions):
        det = m.get('details', {})
        print(f"\nML Decision {i+1}:")
        print(f"  timestamp:      {m.get('timestamp')}")
        print(f"  ml_probability: {det.get('ml_probability')}")
        print(f"  meta_probability: {det.get('meta_probability')}")
        print(f"  ml_gate_decision: {det.get('ml_gate_decision')}")
        print(f"  trade_score:    {det.get('trade_score')}")
        print(f"  gate_evidence:  {json.dumps(det.get('gate_evidence', {}), indent=4)}")
        print(f"  ALL KEYS: {sorted(det.keys())}")

    # --- RISK DECISIONS ---
    risk_decisions = [r for r in ledger.get('risk_decisions', []) if r.get('symbol') == sym]
    print(f"\n--- RISK DECISIONS ({len(risk_decisions)}) ---")
    for i, r in enumerate(risk_decisions):
        det = r.get('details', {})
        print(f"\nRisk Decision {i+1}:")
        print(f"  timestamp: {r.get('timestamp')}")
        print(f"  ALL KEYS:  {sorted(det.keys())}")
        pprint(det)

    # --- ORDERS ---
    orders = [o for o in ledger.get('orders', []) if o.get('symbol') == sym]
    print(f"\n--- ORDERS ({len(orders)}) ---")
    for i, o in enumerate(orders):
        det = o.get('details', {})
        print(f"\nOrder {i+1}:")
        print(f"  timestamp:    {o.get('timestamp')}")
        print(f"  ALL KEYS:     {sorted(det.keys())}")
        pprint(det)

    # --- FILLS ---
    fills = [f for f in ledger.get('fills', []) if f.get('symbol') == sym]
    print(f"\n--- FILLS ({len(fills)}) ---")
    for i, f_item in enumerate(fills):
        det = f_item.get('details', {})
        print(f"\nFill {i+1}:")
        print(f"  timestamp: {f_item.get('timestamp')}")
        pprint(det)

    # --- CANDIDATES (if they exist as a separate key) ---
    candidates = [c for c in ledger.get('candidates', []) if c.get('symbol') == sym]
    print(f"\n--- CANDIDATES ({len(candidates)}) ---")
    for i, c in enumerate(candidates):
        det = c.get('details', {})
        print(f"\nCandidate {i+1}:")
        print(f"  timestamp: {c.get('timestamp')}")
        print(f"  ALL KEYS:  {sorted(det.keys())}")
        pprint(det)

    # --- TRADES (closed) ---
    trades = [t for t in ledger.get('trades', []) if t.get('symbol') == sym]
    print(f"\n--- TRADES ({len(trades)}) ---")
    for i, t in enumerate(trades):
        det = t.get('details', {})
        print(f"\nTrade {i+1}:")
        print(f"  timestamp:  {t.get('timestamp')}")
        print(f"  candidate_id: {det.get('candidate_id')}")
        print(f"  ALL KEYS:   {sorted(det.keys())}")
        pprint(det)
