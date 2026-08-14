import json

ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))

candidates = ledger.get('candidates', [])
trades = ledger.get('trades', [])

print("Looking for GLD long (0.4088) and BE short (0.4023) trades...")

target_cids = []

for t in trades:
    det = t['details']
    sym = det['symbol']
    if sym in ('GLD', 'BE'):
        print(f"Trade: {sym} {det['side']} {det['strategy']} at {det['entry_at']}")

# We want GLD long momentum and BE short orb
gld_cids = []
be_cids = []

for c in candidates:
    det = c.get('details', {})
    pdet = det.get('payload', det)
    sym = c.get('symbol', pdet.get('symbol', ''))
    strat = pdet.get('strategy', det.get('strategy', ''))
    ge = pdet.get('gate_evidence_json', det.get('gate_evidence_json', {})) or {}
    meta = ge.get('meta_probability')
    
    if sym == 'GLD' and meta is not None and abs(meta - 0.4088) < 0.001:
        print(f"\nFOUND GLD CANDIDATE: {pdet.get('candidate_id')}")
        print(json.dumps(c, indent=2))
        gld_cids.append(pdet.get('candidate_id'))
        
    if sym == 'BE' and meta is not None and abs(meta - 0.4023) < 0.001:
        print(f"\nFOUND BE CANDIDATE: {pdet.get('candidate_id')}")
        print(json.dumps(c, indent=2))
        be_cids.append(pdet.get('candidate_id'))
        
print("\nMatching Trades:")
for t in trades:
    # Need to match by time or something since trade doesn't have candidate_id directly
    pass
