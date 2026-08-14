import json
from datetime import datetime

def analyze_ledger(ledger_path):
    with open(ledger_path, 'r') as f:
        ledger = json.load(f)
    
    fills = ledger.get('fills', [])
    
    positions = {}
    trades = []
    
    for f in fills:
        sym = f['symbol']
        side = f['details']['side']
        qty = float(f['details']['qty'])
        price = float(f['details']['price'])
        reason = f['details'].get('reason', '')
        ts_str = f['timestamp']
        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        
        if sym not in positions:
            positions[sym] = {'qty': 0, 'cost_basis': 0.0, 'entry_ts': None, 'entry_reason': ''}
            
        pos = positions[sym]
        
        if side == 'buy':
            if pos['qty'] < 0: # covering short
                cover_qty = min(abs(pos['qty']), qty)
                trade_pnl = (pos['cost_basis'] - price) * cover_qty
                pnl_pct = ((pos['cost_basis'] - price) / pos['cost_basis']) * 100
                duration = (ts - pos['entry_ts']).total_seconds()
                trades.append({
                    'symbol': sym,
                    'type': 'SHORT',
                    'entry_time': pos['entry_ts'].isoformat(),
                    'exit_time': ts.isoformat(),
                    'duration_sec': duration,
                    'qty': cover_qty,
                    'entry_price': pos['cost_basis'],
                    'exit_price': price,
                    'pnl': trade_pnl,
                    'pnl_pct': pnl_pct,
                    'entry_reason': pos['entry_reason'],
                    'exit_reason': reason
                })
                pos['qty'] += cover_qty
            if qty > 0 and pos['qty'] >= 0: # opening long
                pos['cost_basis'] = price
                pos['qty'] += qty
                pos['entry_ts'] = ts
                pos['entry_reason'] = reason
        else: # sell
            if pos['qty'] > 0: # closing long
                sell_qty = min(pos['qty'], qty)
                trade_pnl = (price - pos['cost_basis']) * sell_qty
                pnl_pct = ((price - pos['cost_basis']) / pos['cost_basis']) * 100
                duration = (ts - pos['entry_ts']).total_seconds()
                trades.append({
                    'symbol': sym,
                    'type': 'LONG',
                    'entry_time': pos['entry_ts'].isoformat(),
                    'exit_time': ts.isoformat(),
                    'duration_sec': duration,
                    'qty': sell_qty,
                    'entry_price': pos['cost_basis'],
                    'exit_price': price,
                    'pnl': trade_pnl,
                    'pnl_pct': pnl_pct,
                    'entry_reason': pos['entry_reason'],
                    'exit_reason': reason
                })
                pos['qty'] -= sell_qty
            if qty > 0 and pos['qty'] <= 0: # opening short
                pos['cost_basis'] = price
                pos['qty'] -= qty
                pos['entry_ts'] = ts
                pos['entry_reason'] = reason
                
    # Sort into winners and losers
    winners = [t for t in trades if t['pnl'] > 0]
    losers = [t for t in trades if t['pnl'] <= 0]
    
    print(f"# Detailed Trade Analysis (New Code / Alphadesk_005)")
    print(f"\n## Winners (Total: {len(winners)})")
    for t in sorted(winners, key=lambda x: x['pnl'], reverse=True):
        print(f"\n### {t['symbol']} ({t['type']}) - Profit: ${t['pnl']:.2f} (+{t['pnl_pct']:.2f}%)")
        print(f"- **Entry:** {t['entry_time']} @ ${t['entry_price']:.2f}")
        print(f"- **Exit:**  {t['exit_time']} @ ${t['exit_price']:.2f} (Duration: {t['duration_sec']/60:.1f} mins)")
        print(f"- **Entry Reason:** {t['entry_reason']}")
        print(f"- **Exit Reason:** {t['exit_reason']}")
        
    print(f"\n## Losers (Total: {len(losers)})")
    for t in sorted(losers, key=lambda x: x['pnl']):
        print(f"\n### {t['symbol']} ({t['type']}) - Loss: ${t['pnl']:.2f} ({t['pnl_pct']:.2f}%)")
        print(f"- **Entry:** {t['entry_time']} @ ${t['entry_price']:.2f}")
        print(f"- **Exit:**  {t['exit_time']} @ ${t['exit_price']:.2f} (Duration: {t['duration_sec']/60:.1f} mins)")
        print(f"- **Entry Reason:** {t['entry_reason']}")
        print(f"- **Exit Reason:** {t['exit_reason']}")

analyze_ledger('c:/Users/peddi/Desktop/Bots/Bot_QA/data/replays/aug13_new/ledger.json')
