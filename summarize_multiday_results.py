import pandas as pd

# Regular trading sessions from July 14 to August 03
data = [
    {"date": "2026-08-03", "trades": 12, "master": 944.63, "old_pis": 249.35, "intel_pis": 319.47, "delta": -625.16, "regime": "Breakout Morning / Chop Afternoon"},
    {"date": "2026-07-31", "trades": 7, "master": 741.88, "old_pis": 247.09, "intel_pis": 489.54, "delta": -252.34, "regime": "Trend Extension"},
    {"date": "2026-07-30", "trades": 7, "master": -1080.72, "old_pis": -347.18, "intel_pis": -538.04, "delta": +542.68, "regime": "Heavy Distribution / Selloff"},
    {"date": "2026-07-29", "trades": 10, "master": -1129.07, "old_pis": -350.90, "intel_pis": -377.75, "delta": +751.32, "regime": "Heavy Distribution / Selloff"},
    {"date": "2026-07-28", "trades": 9, "master": -199.31, "old_pis": 106.08, "intel_pis": 26.64, "delta": +225.95, "regime": "Sideways Range Chop"},
    {"date": "2026-07-27", "trades": 4, "master": 1544.44, "old_pis": 146.59, "intel_pis": 146.59, "delta": -1397.85, "regime": "Gap & Run"},
    {"date": "2026-07-24", "trades": 5, "master": 879.50, "old_pis": 464.83, "intel_pis": 427.03, "delta": -452.47, "regime": "Trend Up"},
    {"date": "2026-07-23", "trades": 5, "master": -500.96, "old_pis": -3.14, "intel_pis": -63.92, "delta": +437.04, "regime": "False Breakouts / Reversals"},
    {"date": "2026-07-22", "trades": 8, "master": -303.69, "old_pis": -169.75, "intel_pis": -211.24, "delta": +92.45, "regime": "Chop & Bleed"},
    {"date": "2026-07-21", "trades": 9, "master": 480.48, "old_pis": 180.48, "intel_pis": 934.82, "delta": +454.34, "regime": "Strong Afternoon Momentum"},
    {"date": "2026-07-17", "trades": 4, "master": -251.02, "old_pis": -64.05, "intel_pis": -225.28, "delta": +25.74, "regime": "Range Bound"},
    {"date": "2026-07-16", "trades": 4, "master": 374.45, "old_pis": 88.68, "intel_pis": 273.18, "delta": -101.27, "regime": "Modest Trend"},
    {"date": "2026-07-15", "trades": 5, "master": 1828.85, "old_pis": 543.49, "intel_pis": 1026.79, "delta": -802.06, "regime": "Strong Trend Up"},
    {"date": "2026-07-14", "trades": 4, "master": 521.77, "old_pis": 150.69, "intel_pis": 713.61, "delta": +191.84, "regime": "Afternoon Expansion"},
]

df = pd.DataFrame(data)
print(df.to_string(index=False))

master_tot = df["master"].sum()
old_tot = df["old_pis"].sum()
intel_tot = df["intel_pis"].sum()

master_losses = df[df["master"] < 0]["master"].sum()
intel_losses = df[df["intel_pis"] < 0]["intel_pis"].sum()

print("\n" + "=" * 80)
print(f"Total Master P&L (14 Days): ${master_tot:.2f}")
print(f"Total Old Brittle PIS P&L:  ${old_tot:.2f}")
print(f"Total Intelligent PIS P&L:  ${intel_tot:.2f}")
print(f"Net Variance (Intel vs Master): ${intel_tot - master_tot:+.2f}")
print(f"Down-Day Capital Protection: Master Lost ${master_losses:.2f} vs Intel Lost ${intel_losses:.2f} (PIS Saved ${abs(master_losses) - abs(intel_losses):+.2f} in Drawdown!)")
print("=" * 80)
