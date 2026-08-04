import json

with open("replay_output.json") as f:
    data = json.load(f)

print("=" * 130)
print(f"{'#':<3} {'SYM':<5} {'STRAT':<15} {'SIDE':<5} {'ENTRY TIME & PX':<22} {'SL / TP':<18} {'LEGACY EXIT & PNL':<25} {'LATEST PIS EXIT & PNL':<28} {'DELTA'}")
print("=" * 130)

tot_leg = 0.0
tot_pis = 0.0

for i, item in enumerate(data, 1):
    t = item["trade"]
    p = item["pis"]
    
    leg_pnl = t["legacy_pnl"]
    pis_pnl = p["pnl"]
    delta = pis_pnl - leg_pnl
    tot_leg += leg_pnl
    tot_pis += pis_pnl
    
    entry_str = f"${t['entry_price']:.2f} ({t['entry_at'][11:19]}Z)"
    sltp_str = f"SL:{t['stop_loss']:.2f} TP:{t['take_profit']:.2f}" if t['take_profit'] else f"SL:{t['stop_loss']:.2f} TP:None"
    leg_str = f"${t['exit_price']:.2f} (${leg_pnl:>7.2f}) {t['exit_at'][11:19]}Z"
    pis_str = f"${p['exit_price']:.2f} (${pis_pnl:>7.2f}) {p['exit_at_utc'][11:19]}Z" if p['exit_price'] else "N/A"
    
    print(f"{i:<3} {t['symbol']:<5} {t['strategy']:<15} {t['side'].upper():<5} {entry_str:<22} {sltp_str:<18} {leg_str:<25} {pis_str:<28} {delta:>+8.2f}")
    print(f"    Legacy Reason: {t['legacy_reason']}")
    print(f"    PIS Reason:    {p['reason']}")
    print(f"    PIS Bar Progression ({len(p['bar_logs'])} bars):")
    for b in p["bar_logs"]:
        print(f"      Bar {b['bar_idx']:02d} [{b['time_et']}]: O={b['open']:.2f} H={b['high']:.2f} L={b['low']:.2f} C={b['close']:.2f} | R={b['r_mul']:>5.2f} PeakR={b['peak_r']:>5.2f} | Stop={b['new_stop']:>6.2f} | Health={b['health']:.2f} State={b['state']:<12} Action={b['action']:<10} Reasons={b['reasons']}")
    print("-" * 130)

print(f"\nTOTAL LEGACY PNL:   ${tot_leg:>8.2f}")
print(f"TOTAL LATEST PIS:   ${tot_pis:>8.2f}")
print(f"NET DIFFERENCE:     ${tot_pis - tot_leg:>+8.2f}")
