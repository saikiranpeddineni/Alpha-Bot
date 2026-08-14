import json

def print_trades(ledger_path, label):
    with open(ledger_path, 'r') as f:
        ledger = json.load(f)
    fills = ledger.get('fills', [])
    
    print(f'=== {label} ===')
    positions = {}
    pnl_total = 0
    for f in fills:
        sym = f['symbol']
        side = f['details']['side']
        qty = float(f['details']['qty'])
        price = float(f['details']['price'])
        reason = f['details'].get('reason', '')
        
        if sym not in positions:
            positions[sym] = {'qty': 0, 'cost_basis': 0.0}
            
        pos = positions[sym]
        
        if side == 'buy':
            if pos['qty'] < 0: # covering short
                cover_qty = min(abs(pos['qty']), qty)
                trade_pnl = (pos['cost_basis'] - price) * cover_qty
                pnl_total += trade_pnl
                print(f'{sym} COVER {cover_qty} @ {price:.2f} (Entry: {pos["cost_basis"]:.2f}) -> PnL: ${trade_pnl:.2f} | Reason: {reason}')
                pos['qty'] += cover_qty
            if qty > 0 and pos['qty'] >= 0: # opening long
                pos['cost_basis'] = price
                pos['qty'] += qty
                print(f'{sym} LONG {qty} @ {price:.2f} | Reason: {reason}')
        else: # sell
            if pos['qty'] > 0: # closing long
                sell_qty = min(pos['qty'], qty)
                trade_pnl = (price - pos['cost_basis']) * sell_qty
                pnl_total += trade_pnl
                print(f'{sym} SELL {sell_qty} @ {price:.2f} (Entry: {pos["cost_basis"]:.2f}) -> PnL: ${trade_pnl:.2f} | Reason: {reason}')
                pos['qty'] -= sell_qty
            if qty > 0 and pos['qty'] <= 0: # opening short
                pos['cost_basis'] = price
                pos['qty'] -= qty
                print(f'{sym} SHORT {qty} @ {price:.2f} | Reason: {reason}')
                
    print(f'Total PnL: ${pnl_total:.2f}\n')

print_trades('c:/Users/peddi/Desktop/Bots/Bot_QA/data/replays/aug13_old/ledger.json', 'OLD CODE')
print_trades('c:/Users/peddi/Desktop/Bots/Bot_QA/data/replays/aug13_new/ledger.json', 'NEW CODE')
