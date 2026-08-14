"""
Candidate-level reconciliation: Extract exactly the fields requested.
For each AMD/NBIS candidate, produce the forensic table.
"""
import json

ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))
SYMBOLS = ['AMD', 'NBIS']

# Build a lookup: candidate_id -> trade details (for accepted+executed trades)
trade_lookup = {}
for t in ledger.get('trades', []):
    det = t['details']
    # The candidate_id in trades is the close order (e.g. "close_AMD_00000112")
    # We need to match by symbol+timestamp
    trade_lookup[(det['symbol'], det['entry_at'])] = det

for sym in SYMBOLS:
    candidates = [c for c in ledger.get('candidates', []) if c.get('symbol') == sym]
    
    print(f"\n{'='*80}")
    print(f"  {sym} — {len(candidates)} candidates")
    print(f"{'='*80}")
    
    for i, c in enumerate(candidates):
        det = c.get('details', {})
        # Some candidates have a 'payload' wrapper from shadow replay
        if 'payload' in det:
            payload_det = det.get('payload', {})
        else:
            payload_det = det
        
        cid = payload_det.get('candidate_id', det.get('candidate_id', 'UNKNOWN'))
        strategy = payload_det.get('strategy', det.get('strategy', 'UNKNOWN'))
        direction = payload_det.get('direction', det.get('direction', 'UNKNOWN'))
        final_decision = payload_det.get('final_decision', det.get('final_decision', 'UNKNOWN'))
        rejection_stage = payload_det.get('rejection_stage', det.get('rejection_stage', 'UNKNOWN'))
        rejection_reason = payload_det.get('rejection_reason', det.get('rejection_reason', 'UNKNOWN'))
        
        # Gate evidence
        ge = payload_det.get('gate_evidence_json', det.get('gate_evidence_json', {}))
        if ge is None:
            ge = {}
        
        ml_prob = ge.get('ml_probability', 'NOT IN DATA')
        ml_passed = ge.get('ml_passed', 'NOT IN DATA')
        meta_prob = ge.get('meta_probability', 'NOT IN DATA')
        meta_passed = ge.get('meta_passed', 'NOT IN DATA')
        trade_score = ge.get('trade_score', 'NOT IN DATA')
        ts_threshold = ge.get('trade_score_threshold', 'NOT IN DATA')
        ts_passed = ge.get('trade_score_passed', 'NOT IN DATA')
        
        # Critical: What threshold did the meta gate use?
        # The gate_evidence_json stores the meta_rejection_reason which includes
        # the threshold used. Let's extract it.
        meta_rej = ge.get('meta_rejection_reason', '')
        meta_threshold_used = 'NOT IN DATA'
        if meta_rej and 'below_threshold_' in str(meta_rej):
            meta_threshold_used = meta_rej.split('below_threshold_')[1] if 'below_threshold_' in meta_rej else 'NOT PARSED'
        elif meta_passed == True:
            meta_threshold_used = 'PASSED (threshold not logged for passes)'
        
        # Also check the pipeline rejection reason (the ACTUAL decision)
        pipeline_rej = payload_det.get('rejection_reason', det.get('rejection_reason', ''))
        pipeline_meta_threshold = 'NOT IN DATA'
        if pipeline_rej and 'below_threshold_' in str(pipeline_rej):
            pipeline_meta_threshold = pipeline_rej.split('below_threshold_')[1] if 'below_threshold_' in pipeline_rej else 'NOT PARSED'
        
        # ML confidence from the candidate record directly
        ml_confidence_direct = payload_det.get('ml_confidence', det.get('ml_confidence', 'NOT IN DATA'))
        
        # Find matching trade
        signal_price = payload_det.get('signal_price', det.get('signal_price', None))
        entry_at = c.get('timestamp', '')
        # Try to find matching trade by entry time approximately
        matched_trade = None
        for (tsym, tentry), tdet in trade_lookup.items():
            if tsym == sym and tdet['strategy'] == strategy:
                # Match by timestamp proximity
                if entry_at and tentry and entry_at[:16] == tentry[:16]:
                    matched_trade = tdet
                    break
        
        print(f"\n--- Candidate {i+1}: {cid} ---")
        print(f"  Strategy:             {strategy}")
        print(f"  Direction:            {direction}")
        print(f"  Final Decision:       {final_decision}")
        print(f"  Rejection Stage:      {rejection_stage}")
        print(f"  Rejection Reason:     {rejection_reason}")
        print(f"")
        print(f"  [PARALLEL EVALUATOR — gate_evidence_json]")
        print(f"  ML Probability:       {ml_prob}")
        print(f"  ML Passed:            {ml_passed}")
        print(f"  Meta Probability:     {meta_prob}")
        print(f"  Meta Passed:          {meta_passed}")
        print(f"  Meta Threshold (from gate_evidence rejection msg): {meta_threshold_used}")
        print(f"  Trade Score:          {trade_score}")
        print(f"  Score Threshold:      {ts_threshold}")
        print(f"  Score Passed:         {ts_passed}")
        print(f"")
        print(f"  [PIPELINE — rejection_reason field]")
        print(f"  Pipeline Rejection:   {pipeline_rej}")
        print(f"  Pipeline Meta Threshold (from pipeline rejection msg): {pipeline_meta_threshold}")
        print(f"")
        print(f"  [CANDIDATE RECORD]")
        print(f"  ml_confidence:        {ml_confidence_direct}")
        print(f"  signal_price:         {signal_price}")
        
        if matched_trade:
            print(f"")
            print(f"  [MATCHED TRADE — EXECUTED]")
            print(f"  Entry:                {matched_trade['entry_price']} at {matched_trade['entry_at']}")
            print(f"  Exit:                 {matched_trade['exit_price']} at {matched_trade['exit_at']}")
            print(f"  Exit Reason:          {matched_trade['exit_reason']}")
            print(f"  MAE:                  {matched_trade['mae']} ({matched_trade['mae_pct']}%)")
            print(f"  MFE:                  {matched_trade['mfe']} ({matched_trade['mfe_pct']}%)")
            print(f"  PnL:                  ${matched_trade['pnl']}")
            print(f"  Realized R:           {matched_trade['r_multiple']}")
        elif final_decision == 'accepted':
            print(f"")
            print(f"  [TRADE MATCH FAILED — accepted but no trade record found for this timestamp]")
        else:
            print(f"")
            print(f"  [NO TRADE — correctly rejected by pipeline]")
