import json
data=json.load(open('c:/Users/peddi/Desktop/Bots/Bot_QA/data/replays/rpl_2d05eeea03f7/ledger.json'))
trades=data.get('trades', [])
losers = sorted([t for t in trades if t['details']['pnl'] < 0], key=lambda x: x['details']['pnl'])
for t in losers[:5]:
    print(t['symbol'], t['details']['side'], t['timestamp'], t['details']['pnl'])
