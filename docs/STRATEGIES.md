# Strategies & the AI Signal Pipeline

Strategies are pure decision functions: `(context, symbol, enriched_frame) -> Signal | None`.
They never size positions or place orders — the pipeline gates them and the risk manager
sizes them. All parameters below are editable via `PUT /api/strategies/{name}` and
persist across restarts.

## Built-in strategies

### `orb` — Opening Range Breakout
After the first 15 minutes establish the session's opening range, trade the first
volume-confirmed close beyond it (either direction). One attempt per symbol per session,
no fresh entries after minute 180.
- **Stop**: opposite side of the range, capped at 2×ATR from entry.
- **Target**: 2R.
- **Params**: `range_minutes`, `entry_cutoff_minute`, `breakout_buffer_bps`,
  `volume_z_min`, `atr_stop_mult`, `reward_risk`.
- **Best regime**: trending / volatile opens. Weighted 1.15× in trends, 0.7× in range.

### `vwap_reversion` — VWAP Mean Reversion
In non-trending tape (efficiency ratio ≤ 0.30), fade stretches ≥ 2σ from session VWAP
when RSI(2) confirms exhaustion. Skips when VWAP is too close to pay for the trip.
- **Stop**: 1.5×ATR beyond entry. **Target**: the VWAP itself.
- **Params**: `z_entry`, `z_window`, `er_max`, `rsi2_oversold/overbought`,
  `atr_stop_mult`, `min_target_atr`.
- **Best regime**: range (1.2×); faded to 0.7× in trends.

### `momentum` — EMA Momentum Cross
Fresh EMA9/EMA20 cross aligned with VWAP side and a rising MACD histogram, RSI kept out
of exhaustion. Exits early when the cross reverses (plus the always-on bracket).
- **Stop**: 1.5×ATR. **Target**: 1.8R.
- **Params**: `rsi_min/max`, `vol_z_min`, `atr_stop_mult`, `reward_risk`.
- **Best regime**: trend_up (1.2×).

### `rsi2_reversion` — RSI(2) Dip Buy
Connors-style panic-dip buy adapted intraday: RSI(2) ≤ 5 while price holds above a
rising EMA50. Long-only by design. Exits on RSI(2) normalization (≥ 70) or the bracket.
- **Stop**: 1.5×ATR. **Target**: 1.5R.
- **Params**: `rsi2_entry`, `rsi2_exit`, `ema_slope_lookback`, `atr_stop_mult`, `reward_risk`.
- **Best regime**: trend_up / range; faded hard (0.5×) in downtrends.

## The AI signal pipeline

Every candidate entry passes three gates (`strategies/pipeline.py`):

1. **ML gate** — the signal model scores p(favorable move) *for that direction*
   (longs need high p_up, shorts high p_down). Signals under
   `ml_confidence_threshold` (default 0.55, runtime-tunable) are dropped. Until you
   train a model, a labeled momentum heuristic provides the score.
2. **Regime weighting** — `score = confidence × regime_weight × ml_probability`.
   Weights encode which conditions each strategy was built for (table above);
   scores below 0.35 are dropped.
3. **Conflict resolution** — one entry per symbol per bar: highest composite score wins.

**Exit signals always bypass the pipeline** — nothing may block risk reduction.

## Risk rules (applied after the pipeline)

| Rule | Default | Effect |
| --- | --- | --- |
| Risk per trade | 0.5% of equity | qty = risk$ / stop distance (ATR-synthesized stop if a strategy omitted one) |
| Max position | 20% of equity | notional cap per position |
| Max open positions | 4 | across all symbols |
| Max daily loss | 2% | flatten + halt until next session |
| Max drawdown | 10% | flatten + **kill switch** (manual reset) |
| Loss streak | 3 losses | 30-minute entry cooldown |
| Session windows | 30 / 5 min | no entries near close; force-flatten before close |
| PDT guard | equity < $25k | blocks the 4th same-day round trip on live accounts |
| Liquidity | $1M/min, $5 | average dollar-volume and minimum price filters |

## Training the model

UI: **AI Insights → Train**, or `POST /api/ai/train {"symbols": null, "days": 30}`.
Fetches 1-minute history (provider-dependent depth; Alpaca recommended for real data),
builds the feature/label set (label: forward return over `ML_HORIZON_BARS` beats a
0.10% cost threshold), trains gradient boosting with a chronological holdout, reports
AUC/accuracy vs base rate, and hot-swaps the live gate. Retrain regularly; markets drift.
