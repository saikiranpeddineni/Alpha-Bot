import json
with open('data/replays/rpl_65a9996ef51c/ledger.json', 'r') as f:
    data = json.load(f)
shadow = data.get('shadow_signals_log', [])
for s in shadow:
    c = s.get('candidate', {})
    if c.get('symbol') == 'BE':
        ts = str(c.get('ts'))
        strat = str(c.get('strategy'))
        dec = str(c.get('final_decision'))
        print("TS: " + ts + " STRAT: " + strat + " DEC: " + dec)
