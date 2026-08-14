"""
Summarize: Meta-passed (clean) vs Meta-overridden (adaptive) trades.
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
    candidate_lookup[(sym, strat, ts[:16])] = ge

trades = ledger.get('trades', [])

meta_passed_trades = []
meta_overridden_trades = []

for t in trades:
    det = t['details']
    key = (det['symbol'], det['strategy'], det['entry_at'][:16])
    ge = candidate_lookup.get(key, {})
    meta_passed = ge.get('meta_passed', None)
    meta_prob = ge.get('meta_probability', None)
    
    row = {
        'symbol': det['symbol'],
        'side': det['side'],
        'strategy': det['strategy'],
        'meta_prob': meta_prob,
        'meta_passed': meta_passed,
        'pnl': det.get('pnl', 0),
        'r': det.get('r_multiple', 0),
    }
    
    if meta_passed == True:
        meta_passed_trades.append(row)
    else:
        meta_overridden_trades.append(row)

print("=" * 80)
print("  GROUP A: Meta-Passed Trades (Clean — Meta gate approved at base threshold)")
print("=" * 80)
total_pnl_a = 0
total_r_a = 0
wins_a = 0
losses_a = 0
for r in meta_passed_trades:
    w = "W" if r['pnl'] > 0 else "L"
    if r['pnl'] > 0: wins_a += 1
    else: losses_a += 1
    total_pnl_a += r['pnl']
    total_r_a += r['r']
    print(f"  {r['symbol']:6} {r['side']:6} {r['strategy']:18} Meta={r['meta_prob']:.4f}  PnL=${r['pnl']:>8.2f}  R={r['r']:>6.2f}  {w}")

print(f"\n  Subtotal: {len(meta_passed_trades)} trades | {wins_a}W / {losses_a}L | PnL=${total_pnl_a:.2f} | R={total_r_a:.2f}")
print(f"  Win Rate: {wins_a/len(meta_passed_trades)*100:.1f}%")
print(f"  Avg R:    {total_r_a/len(meta_passed_trades):.2f}")

print()
print("=" * 80)
print("  GROUP B: Meta-Overridden Trades (Adaptive — Meta vetoed, pipeline accepted)")
print("=" * 80)
total_pnl_b = 0
total_r_b = 0
wins_b = 0
losses_b = 0
for r in meta_overridden_trades:
    w = "W" if r['pnl'] > 0 else "L"
    if r['pnl'] > 0: wins_b += 1
    else: losses_b += 1
    total_pnl_b += r['pnl']
    total_r_b += r['r']
    print(f"  {r['symbol']:6} {r['side']:6} {r['strategy']:18} Meta={r['meta_prob']:.4f}  PnL=${r['pnl']:>8.2f}  R={r['r']:>6.2f}  {w}")

print(f"\n  Subtotal: {len(meta_overridden_trades)} trades | {wins_b}W / {losses_b}L | PnL=${total_pnl_b:.2f} | R={total_r_b:.2f}")
print(f"  Win Rate: {wins_b/len(meta_overridden_trades)*100:.1f}%")
print(f"  Avg R:    {total_r_b/len(meta_overridden_trades):.2f}")

print()
print("=" * 80)
print("  COMPARISON")
print("=" * 80)
print(f"  Meta-Passed:    {len(meta_passed_trades):>2} trades | Win Rate {wins_a/len(meta_passed_trades)*100:>5.1f}% | PnL ${total_pnl_a:>9.2f} | Total R {total_r_a:>6.2f} | Avg R {total_r_a/len(meta_passed_trades):>6.2f}")
print(f"  Meta-Overridden:{len(meta_overridden_trades):>2} trades | Win Rate {wins_b/len(meta_overridden_trades)*100:>5.1f}% | PnL ${total_pnl_b:>9.2f} | Total R {total_r_b:>6.2f} | Avg R {total_r_b/len(meta_overridden_trades):>6.2f}")
print(f"  Overall:        {len(trades):>2} trades | Win Rate {(wins_a+wins_b)/len(trades)*100:>5.1f}% | PnL ${total_pnl_a+total_pnl_b:>9.2f} | Total R {total_r_a+total_r_b:>6.2f} | Avg R {(total_r_a+total_r_b)/len(trades):>6.2f}")
