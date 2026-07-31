# 🛡️ AI Trade Rejections Performance Report

This report evaluates how the trades rejected by the AI pipeline (ML low confidence and Meta-label vetoes) would have performed if we had overridden the AI and executed them blindly.

* **Saved Drawdown (Green)**: The trade would have lost money, meaning the AI correctly saved your capital.
* **Missed Profit (Red)**: The trade would have been profitable, meaning the AI was overly cautious.

| Time | Symbol | Direction | Strategy | Rejection Reason | Entry Price | Current Price | AI Impact (Net P&L) |
|---|---|---|---|---|---|---|---|
| 09:41 | **CRDO** | LONG | rsi2_reversion | ML confidence 0.2805 < 0.48 |  |  | 🔴 Missed +0.16% |
| 09:41 | **META** | LONG | rsi2_reversion | Specialist AUC 0.5254 < 0.53 |  |  | 🔴 Missed +0.49% |
| 10:24 | **CRDO** | SHORT | orb | META_MODEL_UNAVAILABLE |  |  | 🟢 **Saved +3.90%** |

### 📊 Financial Summary

- 🟢 **Total Drawdown Prevented:** +3.90% (Capital Saved)
- 🔴 **Total Profit Missed:** -0.65% (Opportunity Cost)
- 🚀 **Net AI Advantage:** +3.25%
