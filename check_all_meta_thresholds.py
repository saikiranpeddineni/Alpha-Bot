"""
Check Meta probability and threshold for ALL executed trades in rpl_65a9996ef51c.
Match each trade back to its candidate record to get the gate_evidence_json.
"""
import json

ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))

# Build candidate lookup by (symbol, strategy, timestamp)
candidate_lookup = {}
for c in ledger.get('candidates', []):
    det = c.get('details', {})
    # Some candidates have payload wrapper
    if 'payload' in det:
        pdet = det['payload']
    else:
        pdet = det
    
    cid = pdet.get('candidate_id', det.get('candidate_id', ''))
    sym = c.get('symbol', pdet.get('symbol', ''))
    strat = pdet.get('strategy', det.get('strategy', ''))
    ts = c.get('timestamp', '')
    fd = pdet.get('final_decision', det.get('final_decision', ''))
    
    ge = pdet.get('gate_evidence_json', det.get('gate_evidence_json', {}))
    if ge is None:
        ge = {}
    
    rej_reason = pdet.get('rejection_reason', det.get('rejection_reason', ''))
    rej_stage = pdet.get('rejection_stage', det.get('rejection_stage', ''))
    
    candidate_lookup[(sym, strat, ts[:16])] = {
        'candidate_id': cid,
        'final_decision': fd,
        'rejection_stage': rej_stage,
        'rejection_reason': rej_reason,
        'gate_evidence': ge,
    }

# Now walk every trade and find its candidate
trades = ledger.get('trades', [])
print(f"{'Symbol':6} {'Side':6} {'Strategy':18} {'ML Prob':>8} {'Meta Prob':>10} {'Meta Thresh(Eval)':>18} {'Meta Passed(Eval)':>18} {'Pipeline Decision':>18} {'Pipeline Meta Thresh':>22} {'PnL':>10} {'R':>7}")
print("-" * 170)

for t in trades:
    det = t['details']
    sym = det['symbol']
    strat = det['strategy']
    side = det['side']
    entry_at = det['entry_at']
    pnl = det.get('pnl', 0)
    r_mult = det.get('r_multiple', 0)
    
    # Find candidate
    key = (sym, strat, entry_at[:16])
    cand = candidate_lookup.get(key, None)
    
    if cand:
        ge = cand['gate_evidence']
        ml_prob = ge.get('ml_probability', None)
        meta_prob = ge.get('meta_probability', None)
        meta_passed_eval = ge.get('meta_passed', None)
        
        # Extract threshold from evaluator rejection message
        meta_rej = ge.get('meta_rejection_reason', '')
        eval_threshold = '—'
        if meta_rej and 'below_threshold_' in str(meta_rej):
            eval_threshold = meta_rej.split('below_threshold_')[1]
        elif meta_passed_eval == True:
            eval_threshold = '≤meta_prob (PASS)'
        
        # Extract threshold from pipeline rejection reason
        pipe_rej = cand.get('rejection_reason', '')
        pipe_threshold = '—'
        if pipe_rej and 'below_threshold_' in str(pipe_rej):
            pipe_threshold = pipe_rej.split('below_threshold_')[1]
        elif cand['final_decision'] == 'accepted':
            pipe_threshold = '≤meta_prob (ACCEPTED)'
        
        ml_str = f"{ml_prob:.4f}" if ml_prob is not None else "None"
        meta_str = f"{meta_prob:.4f}" if meta_prob is not None else "None"
        
        print(f"{sym:6} {side:6} {strat:18} {ml_str:>8} {meta_str:>10} {eval_threshold:>18} {str(meta_passed_eval):>18} {cand['final_decision']:>18} {pipe_threshold:>22} {pnl:>10.2f} {r_mult:>7.2f}")
    else:
        print(f"{sym:6} {side:6} {strat:18} {'?':>8} {'?':>10} {'CANDIDATE NOT FOUND':>18} {'?':>18} {'?':>18} {'?':>22} {pnl:>10.2f} {r_mult:>7.2f}")
