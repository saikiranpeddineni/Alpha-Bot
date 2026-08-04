import json

with open("replay_output.json") as f:
    data = json.load(f)

for i, item in enumerate(data, 1):
    t = item["trade"]
    p = item["pis"]
    leg_pnl = t["legacy_pnl"]
    pis_pnl = p["pnl"]
    delta = pis_pnl - leg_pnl
    
    print(f"[{i:02d}] {t['symbol']:<5} ({t['strategy']:<15} {t['side'].upper():<5} Qty:{t['qty']})")
    print(f"     Entry:       ${t['entry_price']:<9.4f} @ {t['entry_at']}  |  SL: ${t['stop_loss']}  |  TP: ${t['take_profit']}")
    print(f"     Legacy Exit:  ${t['exit_price']:<9.4f} @ {t['exit_at']}  |  PnL: ${t['legacy_pnl']:>8.2f}  |  Reason: {t['legacy_reason']}")
    print(f"     PIS Exit:     ${p['exit_price']:<9.4f} @ {p['exit_at_utc']}  |  PnL: ${p['pnl']:>8.2f}  |  Reason: {p['reason']}")
    print(f"     Delta:        ${delta:>+8.2f}")
    print(f"     PIS Bar Breakdown ({len(p['bar_logs'])} bars):")
    for b in p["bar_logs"]:
        print(f"       Bar {b['bar_idx']:02d} [{b['time_et']}]: O={b['open']:.2f} H={b['high']:.2f} L={b['low']:.2f} C={b['close']:.2f} | R={b['r_mul']:>5.2f} PeakR={b['peak_r']:>5.2f} Stop={b['new_stop']:>6.2f} | Health={b['health']:.2f} State={b['state']:<12} Action={b['action']:<10} Reasons={b['reasons']}")
    print("=" * 110)
