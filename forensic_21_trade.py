"""
Full 21-trade forensic reconstruction.

For each executed trade we need to determine which override mechanism fired.

The adaptive threshold logic (memory.py L316-344) has exactly 3 outcomes:
  - shrunk_expectancy > 0.8  → threshold = 0.48
  - shrunk_expectancy < 0.0  → threshold = 0.65
  - else                     → threshold = base (0.55)
  Plus: first 15 mins → threshold += 0.15

HARVEST BYPASS (prediction.py L1061-1064):
  - vol_z > 3.0 AND meta_prob < threshold AND meta_prob >= 0.45 → threshold = 0.45

These are applied sequentially:
  1. Adaptive threshold computed first (engine_evaluation.py)
  2. Passed to pipeline.py as meta_threshold
  3. MetaLabelEngine.predict() receives it as adaptive_threshold
  4. HARVEST BYPASS is applied AFTER the model predicts, inside predict()

So the effective threshold is:
  effective = min(adaptive_threshold, 0.45 if harvest_bypass else adaptive_threshold)

Data available in candidate records:
  - gate_evidence_json: evaluator uses BASE threshold (0.55), NOT adaptive
  - clock_phase: available → can compute opening handicap
  - spy_vol: available → feeds into contextual expectancy
  - slot_rvol: available → feeds into contextual expectancy
  - strategy, direction, regime: available

Data NOT available in candidate records:
  - shrunk_expectancy (from DB query at runtime)
  - vol_z (from feature row at runtime)
  - the actual adaptive threshold used

But we CAN determine:
  - clock_phase tells us if opening handicap was applied
  - For accepted trades where meta_prob < 0.55: some override MUST have fired
  - If meta_prob >= 0.45 and < effective_threshold: HARVEST BYPASS could explain it
  - If meta_prob < 0.45: neither mechanism would help (min threshold is 0.45)

Let's classify.
"""
import json

ledger = json.load(open('data/replays/rpl_65a9996ef51c/ledger.json'))

candidate_lookup = {}
for c in ledger.get('candidates', []):
    det = c.get('details', {})
    pdet = det.get('payload', det)
    sym = c.get('symbol', pdet.get('symbol', ''))
    strat = pdet.get('strategy', det.get('strategy', ''))
    ts = c.get('timestamp', '')
    ge = pdet.get('gate_evidence_json', det.get('gate_evidence_json', {})) or {}
    clock_phase = pdet.get('clock_phase', det.get('clock_phase', ''))
    spy_vol = pdet.get('spy_vol', det.get('spy_vol', None))
    slot_rvol = pdet.get('slot_rvol', det.get('slot_rvol', None))
    regime = pdet.get('regime', det.get('regime', ''))
    
    candidate_lookup[(sym, strat, ts[:16])] = {
        'gate_evidence': ge,
        'clock_phase': clock_phase,
        'spy_vol': spy_vol,
        'slot_rvol': slot_rvol,
        'regime': regime,
        'final_decision': pdet.get('final_decision', det.get('final_decision', '')),
        'rejection_reason': pdet.get('rejection_reason', det.get('rejection_reason', '')),
    }

trades = ledger.get('trades', [])

print(f"{'#':>2} {'Symbol':6} {'Side':6} {'Strategy':18} {'ML':>7} {'Meta':>7} {'MetaP':>6} {'ClockPh':>10} {'Regime':>10} {'PnL':>9} {'R':>7} {'Classification'}")
print("-" * 130)

for i, t in enumerate(trades):
    det = t['details']
    key = (det['symbol'], det['strategy'], det['entry_at'][:16])
    cand = candidate_lookup.get(key, {})
    ge = cand.get('gate_evidence', {})
    
    ml_prob = ge.get('ml_probability')
    meta_prob = ge.get('meta_probability')
    meta_passed_eval = ge.get('meta_passed')
    clock_phase = cand.get('clock_phase', '?')
    regime = cand.get('regime', '?')
    pnl = det.get('pnl', 0)
    r_mult = det.get('r_multiple', 0)
    
    ml_str = f"{ml_prob:.4f}" if ml_prob is not None else "None"
    meta_str = f"{meta_prob:.4f}" if meta_prob is not None else "None"
    mp_str = "Y" if meta_passed_eval else "N"
    
    # Classify
    if meta_passed_eval:
        classification = "1-CLEAN_META_PASS"
    else:
        # Meta was rejected at base 0.55. But trade was accepted.
        # Which mechanism let it through?
        
        if meta_prob is None:
            classification = "4-UNKNOWN_BYPASS"
        elif meta_prob >= 0.55:
            classification = "1-CLEAN_META_PASS (data mismatch?)"
        elif meta_prob >= 0.48:
            # Could be adaptive (shrunk_exp > 0.8 → threshold=0.48)
            # Could be HARVEST BYPASS (vol_z > 3 → threshold=0.45)
            # Both are possible. We can't distinguish without vol_z and shrunk_exp.
            classification = "2-ADAPTIVE_OR_HARVEST (meta≥0.48)"
        elif meta_prob >= 0.45:
            # Only HARVEST BYPASS could rescue this (threshold=0.45)
            # OR adaptive with opening handicap wouldn't help (it raises threshold)
            # adaptive alone (0.48) wouldn't pass this
            classification = "3-ONLY_HARVEST_POSSIBLE (0.45≤meta<0.48)"
        else:
            # meta < 0.45. Neither mechanism can rescue.
            # The min clamp in get_adaptive_ml_threshold is 0.45
            # HARVEST BYPASS requires prob >= 0.45
            # So this should be impossible... yet it was accepted.
            classification = "5-IMPOSSIBLE_OVERRIDE (meta<0.45)"
        
        # Append winner/loser
        if pnl > 0:
            classification += " → WINNER"
        else:
            classification += " → LOSER"
    
    print(f"{i+1:>2} {det['symbol']:6} {det['side']:6} {det['strategy']:18} {ml_str:>7} {meta_str:>7} {mp_str:>6} {clock_phase:>10} {regime:>10} {pnl:>9.2f} {r_mult:>7.2f} {classification}")

print()
print("=" * 130)
print("LEGEND:")
print("  1-CLEAN_META_PASS        : Meta prob ≥ base threshold (0.55). No override needed.")
print("  2-ADAPTIVE_OR_HARVEST    : Meta prob ∈ [0.48, 0.55). Could be adaptive (shrunk_exp>0.8→0.48) OR HARVEST (vol_z>3→0.45).")
print("  3-ONLY_HARVEST_POSSIBLE  : Meta prob ∈ [0.45, 0.48). Only HARVEST BYPASS (vol_z>3→0.45) could lower threshold enough.")
print("  4-UNKNOWN_BYPASS         : Meta prob is None.")
print("  5-IMPOSSIBLE_OVERRIDE    : Meta prob < 0.45. Neither mechanism can rescue (both bottom at 0.45).")
print()

# Summary by classification
from collections import Counter, defaultdict
class_counts = Counter()
class_pnl = defaultdict(float)
class_r = defaultdict(float)

for i, t in enumerate(trades):
    det = t['details']
    key = (det['symbol'], det['strategy'], det['entry_at'][:16])
    cand = candidate_lookup.get(key, {})
    ge = cand.get('gate_evidence', {})
    meta_prob = ge.get('meta_probability')
    meta_passed_eval = ge.get('meta_passed')
    pnl = det.get('pnl', 0)
    r_mult = det.get('r_multiple', 0)
    
    if meta_passed_eval:
        cls = "1-CLEAN_META_PASS"
    elif meta_prob is None:
        cls = "4-UNKNOWN"
    elif meta_prob >= 0.48:
        cls = "2-ADAPTIVE_OR_HARVEST"
    elif meta_prob >= 0.45:
        cls = "3-ONLY_HARVEST"
    else:
        cls = "5-IMPOSSIBLE"
    
    class_counts[cls] += 1
    class_pnl[cls] += pnl
    class_r[cls] += r_mult

print("\nSUMMARY BY CLASSIFICATION:")
print(f"{'Classification':30} {'Trades':>7} {'PnL':>12} {'Total R':>10}")
print("-" * 65)
for cls in sorted(class_counts.keys()):
    print(f"{cls:30} {class_counts[cls]:>7} {class_pnl[cls]:>12.2f} {class_r[cls]:>10.2f}")
