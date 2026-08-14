import json
ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))
trades = ledger.get('trades', [])
ml_decisions = ledger.get('ml_decisions', [])

for t in trades:
    det = t['details']
    sym = t['symbol']
    # find ml_decision before the trade
    mls = [m for m in ml_decisions if m['symbol'] == sym and m['timestamp'] <= t['timestamp']][-1:]
    if mls:
        mld = mls[0].get('details', {})
        ge = mld.get('gate_evidence', {})
        print(f"{sym:5} | {det['side']:5} | PNL: {det.get('pnl', 0):6.2f} | Meta Passed: {ge.get('meta_passed')} | ML Passed: {ge.get('ml_passed')} | Vetoes: {ge.get('vetoes')}")
    else:
        print(f"{sym:5} | {det['side']:5} | No ML decision found")
