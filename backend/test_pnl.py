import json
with open('C:/Users/peddi/Desktop/Bots/Bot_QA/data/replays/rpl_ef40deff8239/ledger.json') as f:
    data = json.load(f)
    print('Total PnL:', sum(t['details'].get('pnl',0) for t in data['trades']))
    for t in data['trades'][:5]:
        print(t['symbol'], t['details']['side'], t['details']['pnl'])
