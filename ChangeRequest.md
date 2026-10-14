# Change Request — Peak-R Capture & Full-Strategy Profit Maximization
_Intelligence upgrade, not restriction removal. No thresholds lowered. No gates disabled._

Date: 2026-10-13 · Branch: `feat/opportunity-driven-capital-deployment`
Basis: 1,402 live trades -$16,360 · last-10 sessions 124 trades -$996 PF 0.778 · DOA 51.6% -$3,022 · peak≥1R cohort avg peak 1.83R → final 0.83R (55% giveback, 92.3R left on table) · only `vwap_reversion` lifetime-green (+$941/46).

## Design Principles (non-negotiable)

1. **Smarter, not looser.** No ML/meta/score/risk threshold reductions. Every missed winner is fixed by better features, timing, re-entry, or exit intelligence — never by widening admission.
2. **No hardcoding.** Every new behavior behind `RiskConfig` / `PISConfig` / strategy `default_params` + env knob. Defaults preserve current Champion behavior.
3. **Peak-R first.** Optimize `avg final R / avg peak R` (capture efficiency), expectancy, PF, Sharpe/Sortino — never trade count.
4. **Prove it.** Each CR needs exact-path replay + walk-forward ≥3 windows + shadow measurement before promotion. Falsified ideas stay OFF with docs.
5. **Observability.** If a failure can't be diagnosed from telemetry, the CR is incomplete.
6. **Retired stays retired.** Nothing in this CR resurrects intentionally-retired code (see scope box below). Where a follow-up queue item references a retired module, the CR explicitly marks it out-of-scope.

> **Intentionally retired — out of scope (do not bring back):**
> Batch-2 CR-146 shadow-research stack (`shadow_ledger` legacy, `counterfactual`, `shadow_replay_eligibility`, `discovery_shadow_evidence_store/validation`, `canonical_evidence`, `cohort_fingerprint`, `strategies/{evaluator,memory,reflection,challenger}`, `core/promotion`, `ai/{calibration,gate_report}`, `analytics/replay_optimizer`, `portfolio/simulator`), Rule-41 mixin/ProtectionState/validator, duplicate routes, `ws` dead-flood guard, `llm_narrative` (+ key-in-URL), `ExcursionRegressors`, midday filter, bypass config knobs, legacy `backtest/engine.py`, hardcoded TradeScore dynamic sizing 0.5–2.0% (`risk/manager.py:406-421` preserved-comments block), max-drawdown hard halt (`risk/manager.py:204-221` operator-removed block).
> `core/shadow_ledger.py` referenced in CR-08/CR-20 is the current production rejection ledger (re-added 2026-08-24), NOT the retired Batch-2 stack. `decision_traces.db` writer restore is **dropped** from CR-20 per this directive — if forensics need it, build a new lightweight trace, do not revive the retired writer.

---

## P0 — Entry Intelligence (fix DOA, keep selectivity)

### CR-01 — Momentum timing intelligence (biggest leak: 632 trades -$6,104, 33% WR)
**Location:** `backend/app/strategies/momentum.py:27,54-65,187-190,233-243,257-285,299-312`

**Current:**
- `EVENT_WINDOW_BARS=10` + `event_window_bars:10` allows MACD inflection up to 10 min old to fire (`_valid_event:137-141`). Buys after move ran.
- `rsi_min 50 / rsi_max 78 / vol_z_min 0.5` loosest in repo. RSI 50 = mid-move, 78 = overbought chase, vol 0.5 = no participation proof.
- `min_minutes_since_open:5` defined but **never enforced** in `on_bar` (only `mins_et` computed at `:189-190`). Trades opening noise.
- `htf_trend_filter:False` default → 15m check at `:193-201` is dead code.
- Confidence at `:237-243` monotonically rewards `vol_z`/mover (+10%) with no climax fade.
- No `min_target` / `min_RR` economic gate (contrast `vwap_reversion.py:100-105`).
- No one-per-day (contrast `orb.py:56,63-72`, `liquidity_sweep.py:44,49-51`). Re-arms every inflection → 632 trades.

**Change (intelligent, tightening-neutral):**
- Enforce `min_minutes_since_open` (default 10, env `MOM_MIN_MINS_OPEN`). Return None when `minutes_from_open < param`.
- Add `event_window_bars` adaptive: default 10 → 3 (env `MOM_EVENT_WINDOW`). Keep param; research may set 2-5 per regime. Log `age_bars` (already at `:143-147`).
- Tighten admission via intelligence, not new veto file: `rsi_min 55 / rsi_max 70 / vol_z_min 1.2` as new defaults (env-overridable), plus `er20 >= 0.25` efficiency requirement and stacked-EMA check (reuse `vwap_reversion.py:85-95` pattern: skip long into bearish stack, short into bullish stack).
- Add climax/acceptance: skip when `abs(close-ema9) > 2.0*ATR` OR decision-bar `range >= 1.8*ATR and close_pos >= 0.65` (mirror `pipeline.py:208-211` at strategy level so confidence reflects it).
- Add economic gate: skip when `(target-entry) < 0.5*ATR` or `reward/risk < 1.0` (copy `vwap_reversion.py:100-105,121-122`).
- One-per-day cap: add `_traded: set[(symbol,date)]` + `on_entry_filled` like `orb.py:63-67`. Param `max_entries_per_day:1`.
- Enable `htf_trend_filter` default True once 15m resample cost measured; else keep False but wire mover-RS confirmation (see CR-09).
- Confidence: replace monotonic vol reward with hump: `+0.05*min(vol_z,2.0) - 0.08*max(0,vol_z-2.5)` so climax reduces, not increases, confidence.

**Validate:** exact replay momentum-only slice; expect trade count ↓40-60%, DOA ↓, avg MFE ↑, PF ↑ with equal/lower drawdown. Shadow-measure vetoed late entries for pullback re-entry (CR-02b).
**Rollback:** restore `default_params` values; single commit.

### CR-02 — Invert momentum dynamic-stop tightening (stop-hunt fix)
**Location:** `backend/app/strategies/momentum.py:281,308`

**Current:** `dynamic_mult = 1.0 if (vol_z>2.0 and hist expanding) else 2.0` — halves stop to 1.0 ATR on the noisiest bar.

**Change:** widen on climax, tighten on quiet: `dynamic_mult = 2.5 if (vol_z>2.0) else 2.0`, param `climax_stop_mult:2.5` + `base_stop_mult:2.0`. Target stays `rr * dynamic_mult * ATR` so geometry stays consistent. Pipeline pillar-1 invariant (`pipeline.py:151-159`) already preserves native R — no change needed there.
**Validate:** stop-distance distribution + MAE_R on momentum losers; expect fewer -1R full stops, more -0.4R scratches via PIS.

### CR-02b — Pullback re-arm path (capture without loosening)
**Location:** NEW `backend/app/strategies/pullback_rearm.py` (or `momentum.py` + `pipeline.py:251-286` hook), uses `ctx.daily_sma20`, `ema20/vwap`.

**Why:** extreme-guard audit showed V +15.5R, COHR +6.8R, PLTR +6.7R blocked correctly on average (-32R if all vetoes taken) but with no re-entry. Don't loosen veto — add second-chance entry: after `Entry veto: chasing session extreme`, watch for `hold-new-high + 3-bar pullback + close back above EMA9 with vol_z>=1.0` then emit fresh candidate through full ML/meta/score path. Same for chase-veto rejections.
**Knobs:** `enable_pullback_rearm:True`, `rearm_lookback_bars:30`, `rearm_vol_z_min:1.0`.
**Validate:** shadow-measure only first; promote only if re-arm cohort PF>1.5 OOS.

### CR-03 — ORB late-day + second-chance (146 trades -$2,185)
**Location:** `backend/app/strategies/orb.py:35-52,99-100,138-152,172-211`

**Current:** one/day, RANGE/VOLATILE reject, climax 2.5 ATR, acceptance 0.50, drift filter, min_risk 0.75 ATR — good. Leak is late-day underwater entries riding to `end of day` + `min_acceptance 0.50` still admits wick fake-outs on thin names.

**Change:**
- Add `end_time` param (default `15:00 ET`, env `ORB_END_TIME`): no NEW ORB after cutoff; existing runners still managed by PIS. This is timing intelligence, not a new veto file — ORB edge is morning.
- Raise `min_acceptance_ratio` 0.50 → 0.60 for `vol_z<2.0` entries only (keep 0.50 when vol confirms). Param `acceptance_lowvol:0.60`.
- Add `min_range_atr:0.5`: skip degenerate tiny ranges whose breakout is noise (stop would be `min_risk` floor anyway).
- Wire ORB into pullback re-arm (CR-02b): failed-breakout sweep that reclaims range high + acceptance → liquidity_sweep-style fade handled by sweep, not ORB re-fire (keep one/day).

### CR-04 — BB Squeeze confirmation + trend grace (112 trades -$831, 37% WR; 23 trades +$76 last-10 at 57% WR — closest to green)
**Location:** `backend/app/strategies/bb_squeeze.py:15,29-39,115-153,159-172`

**Current:** 3-bar window (good), vol 1.0, 5-min blackout, EMA20-vs-VWAP after 30 min, VOLATILE reject.

**Change:**
- Extend `min_minutes_since_open` 5 → 15 for squeeze (squeeze needs established VWAP; 5 min is noise). Param only.
- Add `bb_mid hold` already at `:150-153` — extend to require `close beyond bb_upper + 0.2*ATR` on confirm bar (kills head-fake wick tags). Param `breakout_buffer_atr:0.2`.
- VOLATILE reject at `:171-172` is blanket — make intelligent: allow VOLATILE when `width_percentile <= 0.10` (extreme squeeze) AND `vol_z >= 1.5`. Else reject. Param `volatile_squeeze_pct:0.10`.
- Add economic gate like VWAP-R: skip when `(target-entry) < 0.5*ATR`.

### CR-05 — Gap-and-Go exhaustion guard + re-arm (14 trades -$516 lifetime; 2 trades +$104 last-10 — small-n, high variance)
**Location:** `backend/app/strategies/gap_and_go.py:26-42,75-135`

**Current:** gap ≥2%, vol 1.5, 09:35-11:30, EMA/VWAP/open alignment, stop at EMA20±0.05, RR 2.5. No exhaustion check — Aug-24 shorts into >4% grind-down lows all cut by PIS.

**Change:**
- Add multi-bar exhaustion: skip short when `close within 2.5 ATR of session low` AND `5-bar return < -3%`; skip long symmetric. Reuse pipeline extreme-guard math (`pipeline.py:257-284`) at strategy level for better confidence. Params `exhaustion_atr:2.5`, `exhaustion_pct:3.0`.
- Add `max_gap_pct:8.0`: gaps >8% are already spent; route to pullback re-arm instead of market order.
- Keep one/day + meta fail-close (`MANDATORY_STRATEGIES` in `ai/prediction.py`) — do NOT lift until artifacts trained.

### CR-06 — Liquidity Sweep viability (35 trades -$958, 26% WR)
**Location:** `backend/app/strategies/liquidity_sweep.py:33-40,100-148`

**Current:** good thesis (fade sweep, RR 3.0, RANGE/VOLATILE only, 10:15-15:30, one/day). Leak: 0.30% tolerance + wick stop can be pennies on low-ATR names → full -1R on noise.

**Change:** add `min_risk_atr:0.75` floor (copy `orb.py:46,179`) + `min_target_atr:0.5` viability (copy `vwap_reversion.py:36`). Skip degenerate sweeps. No threshold change.

### CR-07 — Expand high-quality mean-reversion routing (don't starve winners)
**Location:** `backend/app/strategies/rsi_reversion.py:29-38,74`, `trend_pullback.py:31-36,86-94`, `vwap_reversion.py:29-42`, `backend/app/core/engine.py` scanner + `TradeScoreEngine`.

**Evidence:** `vwap_reversion` lifetime +$941/46 (46% WR), last-10 8 trades +$583. `rsi2_reversion`/`trend_pullback` barely fire (1-3 trades) because `rsi2_entry 5.0`, `max_vol_z 0.5/1.0`, `TREND_UP`-only gating are ultra-selective. Don't loosen — route smarter:
- Add regime router: in RANGE, boost `vwap_reversion`/`liquidity_sweep`/`rsi2` score weight (already `1.20` at `vwap_reversion.py:27`); in TREND, boost `trend_pullback`/`bb_squeeze`. Make `TradeScoreEngine` regime weights operational (currently `min(1.0,weight)` tightening-only at `pipeline.py:401-403` — keep that, but let router prioritize candidate *ranking*, not threshold).
- Add `rsi2_exit 70` wiring: PIS currently owns exits; wire RSI2 normalization as PIS `ceiling_harvest` input (see CR-11), not a competing exit.
- `trend_pullback max_vol_z 0.5` is correct (low-vol dip) — add `min_er20 0.35` already there; add EMA-gap viability: skip when `(close - (ema50-0.5ATR)) < 0.5*ATR`.

### CR-08 — Fix silent funnel leaks (360 invisible candidates, 16 warmup rejects incl. COHR +6.5R)
**Location:** `backend/app/strategies/pipeline.py:98-100`, `backend/app/core/engine_evaluation.py`, `backend/app/data/manager.py` warmup/backfill, `backend/app/core/shadow_ledger.py` (or `core/shadow_ledger.py`).

**Change:**
- Warmup prefetch for scanner-discovered symbols: `run_market_scanner` must `warmup(to_add)` like `set_watchlist` does (regression from 2026-08-25 fix — verify still wired). If frame <120 bars, backfill BEFORE `<30-bar` early-return (order matters).
- `feature row unavailable (warmup)` hard-reject → fail-soft: emit with `ml_prob=None → REJECT` today; change to reduced-confidence path: allow `TradeScore`-only evaluation with `risk_mult 0.5` + `REQUIRE meta PASS` when warmup <30 bars. Log `WARMUP_REDUCED` lineage. Param `warmup_reduced_risk_mult:0.5`.
- Overlap-resolution + gated-tick drops must write ledger rows (reason `lost conflict` / `gated tick`) — 360 candidates left zero trace per Aug-25 validation. Fix `shadow_ledger.record()` call sites in `engine_evaluation.py` + conflict path at `pipeline.py:415-420`.
- Decision-bar freshness gate + post-close entry gate (already shipped per handoff) — keep, add `signal_timestamp_utc/decision_bar_timestamp_utc` population (currently NULL in `backtest_next.json` all 29 rows). Required for forensics.

### CR-09 — ML/meta/rescue intelligence (no threshold cuts)
**Location:** `backend/app/ai/prediction.py` (MetaLabelEngine, TradeScoreEngine, precompute pool), `backend/app/ai/trainer.py`, `backend/app/ai/model.py`, `backend/app/strategies/pipeline.py:311-414`

**Change:**
- Retrain under `.venv` python only (foreign-interpreter spawn deadlock at `prediction.py:800` + `trainer.py:158` — bounded pool + 900s wall + inline fallback already shipped; verify). Confirm `[wfv] N windows >= 3` (`config.py:220-222` 90+10) + `gap_and_go` meta artifacts → lift fail-close pause only then.
- Specialist fallback alert: when fallback rate spikes (14/53 stale → global), emit `applog.warning` + UI badge (Watchlist `ML .` grey already shipped — keep).
- High-agreement rescue (NOK case: ml 0.67 meta 0.84 PASS, TS 69.4<70 rejected by 0.6, ran +2.9%): do NOT lower 70. Add rescue rule `ml>=0.65 AND meta>=0.80 → admit despite TS<threshold` with `risk_mult 0.75`, shadow-measured first. Param `rescue_ml_min:0.65`, `rescue_meta_min:0.80`, `rescue_risk_mult:0.75`. Requires OOS PF>1.5 before Champion.
- Remove `vol_z>3.0` bypass (already removed per handoff — verify no regression).
- Gray-zone (`pipeline.py:355-357` `soft_composite_gating`, threshold-0.04) keep default OFF in Champion; Challenger only.

---

## P1 — Peak-R Capture (exit intelligence — where 55% of peak is lost)

### CR-10 — Adaptive protection arming (high-ATR names never lock)
**Location:** `backend/app/pis/protection.py:66-126`, `backend/app/pis/models.py:238-271` (PISConfig defaults)

**Current:** `activation_mfe_r 1.50`, `breakeven_trigger_r 0.40`, `breakeven_trigger_atr_mult 2.50`, `cap 0.85`, lock 20→35%, trail ≥1.0 ATR behind peak. Below trigger → initial stop. Live winners avg ~0.44R peak → many never arm.

**Change (intelligent, not tighter-blind):**
- Make `breakeven_trigger_atr_mult` strategy-aware: mean-reversion (VWAP target, snap-back) arms at 2.5; breakout/continuation (momentum/orb/squeeze, trending) arms at 1.5 (faster lock, they reverse hard). Param `breakeven_atr_mult_breakout:1.5` vs `..._reversion:2.5`. No global lowering.
- Raise `progressive_lock_end_frac` 0.35 → 0.45 for `peak_r >= 1.0` only (bank more of proven excursion; sub-0.4R behavior unchanged). Param-gated.
- Add per-symbol ATR adaptation: `effective_trigger` already `min(0.85, max(0.40, 2.5*ATR/R))` — log `effective_trigger` + `atr_r` per position (telemetry for CR-12 tuning). No logic change, observability only.

### CR-11 — Scale-out + runner extension to predicted ceiling (fix 2R-never-arms)
**Location:** `backend/app/pis/exit_planner.py:140-165,225-236`, `backend/app/pis/models.py:256-258,282-286`, `backend/app/pis/max_r_prediction.py` (or `runner_intelligence.py`), `backend/app/pis/protection.py:72-87`

**Current:** `scale_out_target_r 2.0`, weak-trend 1.0, ratio 0.5. Winners avg 0.44-1.5R peak → 2R almost never arms; every floor tag became full exit before floor-tag-runner.

**Change:**
- Keep 2.0 strong / 1.0 weak. Add **predicted-ceiling scale-out**: when `predicted_max_r` available and `peak_r >= max(1.0, 0.70*predicted_max_r)` with `momentum<=0.45`, trigger Stage-1 50% (same as `stagnation_harvest`). This banks at *predicted* capacity, not fixed 2R. Already partially via `ceiling_harvested` at `:105-111` (requires `has_scaled_out`) — extend to `not has_scaled_out` with `exit_ratio 0.5`.
- Runner trail already `runner_wide_atr_mult 2.0` early → 1.2 in ceiling zone (`protection.py:77-84`) — keep. Add `MAX_R_CEILING` full harvest already at `:225-228` — keep.
- Wire `predicted_max_r` + `runner_probability` into `PISDecision.metadata` (already) + UI (positions show predicted ceiling). No exit-logic loosening.
- `rsi2_exit 70` / VWAP-magnet hit: treat `target_hit` (`:87-92`, tolerance `0.20*ATR`) as Stage-1 trigger for mean-reversion (already does at `:160`) — verify `take_profit` plumbing from `engine.py` entry path (was dead code before 08-25 fix; verify still stored in `_position_take_profits`).

### CR-12 — Giveback guard adaptivity (peak 0.60R, 55% — tune by data, not gut)
**Location:** `backend/app/pis/exit_planner.py:166-195`, `backend/app/pis/models.py:276-281`

**Current:** `profit_guard_peak_r 0.60`, `max_giveback 55%`, `min_giveback_atr 0.75`, exit 50%. Good design (ATR noise floor + still-green requirement).

**Change:** make `profit_guard_max_giveback_pct` peak-adaptive: `0.55` when `peak<1.0R`, `0.40` when `1.0<=peak<2.0`, `0.30` when `peak>=2.0` (lock more of bigger peaks). Params `profit_guard_pct_tier1/2/3`. This *tightens* big-peak giveback, loosens nothing. Shadow-measure vs current 55% flat.
- Keep `min(peak*0.50, max(...))` structure at `:187-193` — only the pct input becomes tiered.

### CR-13 — Stagnation harvest speed (losers held 30 bars vs winners 13.6)
**Location:** `backend/app/pis/exit_planner.py:116-138`, `backend/app/pis/models.py:252`, `backend/app/pis/thesis_drift.py`

**Current:** fast stall `peak>=1.0 + hist>=12 + stag>=10 + mom<=0.35 + r>=0.20`; structural `hist>=25 + stag>=20 + peak>=0.50 + r>=0.30 + mom<=0.30`. `stagnation_max_bars 30`.

**Change:** add DOA-stall harvest: `hist>=15 + stag>=12 + peak<0.30 + r<=-0.25` → FULL_EXIT (cut dead money faster; today these bleed 30 bars to -1R). Param `doa_stagnation_bars:12`, `doa_r_threshold:-0.25`. This is loss-cut acceleration, not profit-choke (requires underwater + no excursion).
- Keep `DISTRIBUTION trim profit-only` (`:237-246`) — verified correct, do not extend to losers.

### CR-14 — Early invalidation keep + wick-awareness (don't touch)
**Location:** `backend/app/pis/models.py:301-308`, `backend/app/pis/thesis_drift.py:81-91`

**No change to thresholds** (`-0.55R`, bars 3-5, 2-bar confirmation). Add wick context to reason string only (wick>=0.60 already in `thesis_drift.py:81-91`). If anything, *require* confirmation — never single-bar scratch.

### CR-15 — Intelligent EOD (stop riding underwater into flatten)
**Location:** `backend/app/core/engine_halts.py:106`, `backend/app/risk/manager.py:244-251,270-271`, `backend/app/config.py:52,184`

**Current:** `flatten_minutes_before_close 5`, `no_entry 15-30`. Underwater late ORB entries ride to `end of day` for full loss.

**Change:** EOD pre-harvest at `no_entry_minutes_before_close` boundary: if `r_multiple < -0.30` and `health<0.45` at entry-window close, PIS FULL_EXIT now (scratch the dying trade, free margin, avoid flatten slippage). If green with `is_runner`, hold to flatten window (existing). Param `eod_preh harvest_r:-0.30`. Log `EOD_PREHARVEST` reason.

### CR-16 — Fix bare-`exit` $12.7k gap (highest-ROI observability)
**Location:** `backend/app/portfolio/tracker.py:394-396,425`, `backend/app/brokers/paper.py:310,493-506`, `backend/app/brokers/base.py:83`, `backend/app/core/engine.py:469,1909`, `scripts/run_production_v3_replay.py:83`

**Change:** require non-empty `Order.reason` at submit (assert in `RiskManager.evaluate_entry` + `engine_evaluation.py:236-237` PIS path). Enumerate: `stop_loss/take_profit/PIS Exit Reason:*/end of day/daily loss halt/manual flatten/manual close/engine stop`. Replay must map `legacy_close` → explicit reason, never bare `exit`. Backfill script for 468-row cohort using broker fills + PIS history. Add test `test_exit_reason_never_bare`.

---

## P2 — Sizing / Portfolio Intelligence (risk within budget, never over)

### CR-17 — Confidence-weighted sizing inside fixed budget
**Location:** `backend/app/risk/manager.py:352-429`, `backend/app/portfolio/allocator.py`, `backend/app/portfolio/recent_performance.py:governor_*`, `backend/app/config.py:35-40,83-105`

**Keep:** `risk_per_trade_pct` budget (0.5-1.0%), `max_position 20%`, `max_open 8`, Kelly cap, scanner_mult (1.0/0.75/0.5), governor tightening-only floor 0.25/K=8/15min. TradeScore stays admission/ranking, not sizing (comment at `:422-424` correct — do not revert to hardcoded 0.5-2.0% range).

**Change:**
- Wire `risk_multiplier` (governor × scanner × harvest) end-to-end and log `binding_constraint` (already at `:446-454` — keep, add to UI positions).
- `max_notional_cap overriding governor` review item from 08-24 handoff — verify `qty_after_notional < qty_unconstrained` doesn't silently nullify governor on large-cap names; log both.
- Sector caps `max_positions_per_sector 5` + correlation penalty 0.5 — verify enforced in allocator (audit `allocator.py`; handoff says concentration controls exist but no evidence they bind).

### CR-18 — Turnover budget intelligence (don't starve afternoons)
**Location:** `backend/app/config.py:90`, `backend/app/risk/manager.py` churn check, `backend/app/core/engine_halts.py`

**Keep** session-scoped (midnight ET) fix from 08-25 (rolling-24h bug fixed). **Change:** split budget: morning (09:30-12:00) max 3.0x, full-day 5.0x. Morning churn must not eat afternoon trend budget (BE x6 blocks, ASTS shorts evidence 08-25). Param `max_morning_turnover_multiple:3.0`. Log turnover per half-session.

### CR-19 — Short discipline (shorts -$7.6k/605, worst per-trade)
**Location:** `backend/app/strategies/momentum.py:299-305`, `gap_and_go.py:120-135`, `orb.py:192-211`, `backend/app/risk/manager.py:384-400`

**Change:** regime-aware short sizing (not ban): when `Regime.TREND_UP` or `SPY above VWAP + drift up`, scale short `risk_mult 0.5` (via `signal.risk_multiplier`, already plumbed at `:352-356,402-404`). Long symmetric in TREND_DOWN. Param `countertrend_short_mult:0.5`. Requires OOS short-PF improvement; else keep 1.0.

---

## P3 — Data / Telemetry / Safety

### CR-20 — Forensics that actually resolve
- `shadow_ledger` resolver: verify `px_*m` backfill + `NO_BARS` 6h age-out (staged 08-24/25 — confirm live). `ShadowLedger.enabled` flag (replay isolation) keep. Purge test pollution (`test_strat` in prod DB — fixed 08-25, add test `test_disabled_ledger_drops_rows` already — keep).
- Decision-bar lineage: populate `signal_timestamp_utc/decision_bar_*` (NULL in all 29 backtest rows) via the CURRENT engine path only. Do NOT revive the retired `decision_traces` writer (orphaned since Jul-30) — dropped per retired-code directive.
- Live stream → `bars_cache.db` persistence (gaps cost forensics; `_repair_from_provider` covers on demand but stream should persist).
- NVDA orb `px=128.92` stale-bar anomaly (08-25) — investigate `orb.py` `today_df` vs provider frame before trusting ORB on NVDA.
- MFE/MAE telemetry: excursion maps update BEFORE PIS + stamp at `_drain_fills` (fixed 08-25 — verify; Aug-24 garbage MFE==MAE must not recur). Add test already `test_pis_intelligent_profit_locking.py` — keep.

### CR-21 — Safety posture (do not regress)
- `max_drawdown kill switch removed per operator directive` (`risk/manager.py:204-221` commented out). Per retired-code directive: leave removed, do NOT re-add in any form (hard or soft). Daily-loss halt remains the session guard. Flagged here as accepted operational risk only.
- `EOD_POST_CLOSE_GRACE 15`, reconciliation fail-closed, idempotent `client_order_id`, cancel-verified close, churn 5.0x, PDT guard — all keep.
- Manual: require reason-code + separate manual P&L dashboard; 6 manual trades -$3,909 must trigger review halt. `close_position(reason="manual")` default at `brokers/paper.py:310` → require explicit reason string.

### CR-22 — Penny/thin-name path (already enabled, keep calibrated)
- `min_price 0.50 / min_avg_dollar 5,000 / data_gap 120-600s` (`config.py:61-67`) — keep. Short-borrow on pennies unknown until broker rejects — log `SHORT_BORROW_UNAVAILABLE` distinctly, don't lump into `exit`.
- `min_stop_pct 0.5` pipeline widening (`pipeline.py:135-146`) + pillar-1 target extension (`:151-159`) exempt `vwap_reversion` — keep exempt; verify not over-widening penny stops into uneconomic R.

---

## Validation Plan (per CR)

1. Unit + behavioral tests (existing 804-green suite must stay green; add `test_exit_reason_never_bare`, momentum-timing, warmup-reduced, EOD-preharvest tests).
2. Exact-path replay: `python backend/scripts/run_exact_backtest.py --start 2026-08-10 --end 2026-08-21 --symbols SPY,QQQ,AAPL,NVDA --warmup 600 --out <cr>.json` + full 30-symbol window. Compare: net, PF, WR, expectancy, avg R, Sharpe/Sortino, maxDD, exposure, DOA%, capture efficiency, left-on-table R.
3. Walk-forward ≥3 windows + Monte Carlo on sizing changes. Shadow-measure re-arm/rescue cohorts before Champion.
4. Promotion gates (Preserve Edge): equal/lower drawdown, equal/higher PF/expectancy/Sharpe/Sortino/WR, no sig. increase in losers. Threshold reductions require overwhelming evidence — none proposed here.

## Rollback

Each CR is a single tagged commit behind its env knob (default = current behavior). Rollback = flip knob or revert tag. Policy snapshot immutable + restart-equivalent (CR-146 governance preserved even though shadow-research stack was deleted per directive).

## Remaining Risks

- 30-symbol PF 0.625 vs 4-symbol PF 1.5 — narrow wins don't generalize. Full-window + OOS required.
- Live spread/gap/borrow worse than 2bps paper sim — real fills understate loss.
- Meta/specialist staleness until retrain under `.venv` with ≥3 WFV windows.
- Bare-`exit` $12.7k cohort may reclassify strategy P&L once labeled.
- Afternoon-trend capture depends on morning-churn split (CR-18) + re-arm (CR-02b) both landing.

## Next Steps (ordered)

1. CR-16 (labeling) + CR-08 (ledger/warmup/timestamps) — unblock measurement.
2. CR-01 + CR-02 (momentum) — stop biggest bleed, intelligently.
3. CR-10 + CR-11 + CR-12 + CR-13 (peak capture) — convert 1.83R peaks into >1.2R finals.
4. CR-02b + CR-09 rescue (shadow-only) — recover V/COHR/PLTR/NOK class without loosening.
5. CR-15 + CR-18 + CR-19 — session/side intelligence.
6. Full-window replay → nominate → human review → atomic promotion.

---

## P4 — Dead-Code Complete Removal Plan (delete, do not revive)

Goal: permanently delete retired code, preserved-comment blocks, one-shot patch scripts, and session artifacts. No behavior change. Each batch is a separate delete-only commit, full test suite green before/after, `git revert` is the rollback.

### Batch D1 — Preserved-comment blocks (~45 sites, comment-only deletion)
Grep: `PREVIOUS IMPLEMENTATION|PREVIOUS CODE|PREVIOUS ACTIVE CODE|HISTORICAL DISABLED` (45 matches).
Exact files (keep code above/below, delete only the commented-out block + its header):
- `backend/app/risk/manager.py:204-221` (drawdown halt — operator-removed, leave removed), `:245-251`, `:349-356`, `:359-368`, `:407-421`, `:458-497`
- `backend/app/core/engine.py:1068,1944,1953,1990,2063,2080,2090,2195` blocks
- `backend/app/brokers/alpaca.py:462,522,564,658` blocks
- `backend/app/config.py:47-55,185-193` (old min_price/min_volume + flatten-note blocks)
- `backend/app/replay/runtime.py:97,145,278,406`, `market_feed.py:266`, `fast_worker.py:139`
- `backend/app/core/engine_evaluation.py:71,159`, `pis/engine_adapter.py:103`, `core/gap_scanner.py:49`, `core/top_movers_scanner.py:37`, `risk/market_regime.py:219`, `pis/thesis_drift.py:59`, `ai/model.py:488`
- `backend/tests/test_risk.py:120` disabled-implementation block
- `.agents/AGENTS.md:92`, `.agents/rules/code_modification_policy.md:21` (doc examples — trim to one-line pointer, not full code dump)
Acceptance: `rg "PREVIOUS IMPLEMENTATION|PREVIOUS CODE|HISTORICAL DISABLED" → 0 hits`; `pytest` green; no import change.

### Batch D2 — One-shot patch scripts (42 files, `tools/apply_*.py`)
Inventory: `tools/` holds 42 `apply_*_20260727/28/29.py` scripts (shadow-bot runtime, fill lifecycle, reconciliation hardening, heartbeat, UI tabs, etc.). All were single-apply migrations whose fixes are already in `backend/app/` + covered by `backend/tests/test_*_2026072*.py`.
Plan: delete entire `tools/apply_*.py` set (keep `tools/` dir + any non-apply helper if present — verify with `Get-ChildItem tools/` before delete). If any script is still referenced by docs/CI, replace reference with "applied in <commit>, script removed <date>".
Acceptance: `tools/apply_*.py → 0 files`; `rg "tools/apply_" --glob "*.md" --glob "*.ps1" --glob "*.yml"` clean or updated; suite green.

### Batch D3 — Root/session artifacts (JSON/TXT/CSV, keep code + data DBs)
Delete candidates at repo root (regenerable via replay CLI; not inputs to any test — verify with `rg "backtest_next|backtest_corrected|backtest_latest|smoke_rerun|replay_profile|train_log|train_err|train_pid" backend/tests backend/app` first):
- `backtest_next.json`, `backtest_corrected_run1.json`, `backtest_corrected_run2.json`, `backtest_latest_20260810_21.json` (keep newest under `artifacts/latest_exact_backtest.json` only)
- `train_log*.txt / train_err*.txt / train_pid*.txt`, `smoke_rerun*.json`, `replay_profile.txt`, `replay_full_window_trades.json` if present (per SESSION_HANDOFF §0 cleanup-pending + §7)
- `data/run1_trades.json`, `data/run3_trades.json` (2012-era, superseded by `trades` table), `data/forensic_trades_10_sessions_enriched.csv` + `data/last_10_sessions_forensic.json` once their numbers are folded into this CR (keep until CR-16 lands, then delete)
- `Refer.txt` (2745 bytes, unknown provenance — confirm not referenced, then delete)
Keep: `data/alphadesk.db`, `bars_cache.db`, `models/`, `artifacts/`, `.env.example`.
Acceptance: root contains zero `*.json` session dumps except `artifacts/`; tests don't reference deleted paths.

### Batch D4 — Dead runtime branches (logic deletion, needs tests)
- `momentum.py:193-201` HTF filter is dead when `htf_trend_filter:False` (CR-01 either enables it or deletes the branch — no dead `if` left behind).
- `pipeline.py:401-403` comment + `min(1.0,weight)` stays (Champion tightening-only); delete any challenger/consensus dead path if `enable_entry_consensus:False` short-circuits before use — verify, don't leave unreachable code.
- `test_strategies.py:204` `rsi: float = 62.0  # unused` — delete field.
- `allocator.py:263` ex-dead-code comment ("was always mapping to None...") — delete comment once F-03 verified (code stays, comment goes).
- Frontend: `BacktestModal` posts `/api/backtest` via adapter — once replay-manifest fields surface, delete the adapter shim + old `backtest/engine.py` references (already deleted per handoff — verify no re-import).
Each gets its own behavioral test proving no production-path change (import sweep 109/109 + targeted suite).

### Execution order + safety
1. D1 → D2 → D3 → D4, one commit each (`chore: remove preserved comments`, `chore: remove one-shot apply scripts`, `chore: remove session artifacts`, `chore: remove dead branches`).
2. Before each: `git status` clean, note intended files only. After each: `ruff check backend`, `.venv\Scripts\python -m pytest`, import sweep.
3. Rollback per batch: `git revert <batch-commit>`. No DB migration, no config change, no trading-logic change — P4 batches must show zero replay diff (`run_exact_backtest.py` same-hash before/after).

---

## Appendix A — Full End-to-End Architecture Map (111 files, line-by-line audited 2026-10-13)

111 `backend/app/**/*.py` files audited across 4 parallel passes (core+data+indicators / strategies+AI / PIS+portfolio+risk+brokers / API+DB+replay+frontend). Prior partial reads (13 strategy/PIS/risk/config files) re-verified inside the full pass.

```text
React terminal (BacktestModal/ReplayModal/ShadowPanel/Watchlist/Settings)
  │ REST /api/* + WS /ws (api/routes.py, ws.py, replay.py, dashboard_history.py)
  ▼
FastAPI lifespan (main.py: create_app, hub, applog→WS, shadow resolver_loop, alerting)
  ▼
TradingEngine._tick (core/engine.py:525 — ONLY writer of broker/portfolio state)
  ├─ clock/calendar (clock.py XNYS, calendar.py event guard 20-min)
  ├─ data (manager.py rolling frames/poll/history + alpaca/yahoo/synthetic + store.py SQLite + session_coverage.py 95% gate)
  ├─ indicators (ta.py enrich, slot_volume.py RVOL, vwap_telemetry.py)
  ├─ engine_evaluation._evaluate_symbol (engine_evaluation.py:49 — enrich→gates→regime→PIS exits→strategies→pipeline)
  │    ├─ regime (ai/regime.py 60-bar ER/slope/vol-rank)
  │    ├─ PIS exits FIRST (pis/service→position_brain→health/thesis/runner→protection floor→exit_planner→execution; legacy EXIT discarded :421-423)
  │    ├─ strategies (momentum/orb/bb_squeeze/vwap_reversion/gap_and_go/liquidity_sweep/trend_pullback/rsi_reversion/relative_strength via base.py REGISTRY)
  │    └─ SignalPipeline.process (pipeline.py:75 — excursion→min_stop→native-R→blackout→chase→location→extreme→consensus→ML→meta→score→winner-takes-all)
  │         ├─ ML (ai/model.py dual long/short + specialists + generation store; ai/features.py 21 cols)
  │         ├─ meta (ai/prediction.py MetaLabelEngine; trainer.py fetch→global→specialist→meta→archive→hot-reload)
  │         └─ score (ai/prediction.py TradeScoreEngine)
  ├─ scanners (scanner.py 3-stage + top_movers_scanner.py + gap_scanner.py + universe_discovery.py + discovery_input.py as_of-gated + scanner_selection.py + watchlist_mapper.py experimental)
  ├─ portfolio (tracker.py lots/R/MFE + allocator.py Kelly-budget + performance.py buckets + recent_performance.py governor tightening-only)
  ├─ risk (manager.py halts/cooldowns/sizing + market_regime.py PANIC/STRESS/CAUTION/NORMAL)
  ├─ brokers (paper.py 2bps/0-comm sim + alpaca.py/hardening.py live truth + base.py Fill model)
  ├─ halts/EOD (engine_halts.py flatten-until-flat + durable_engine.py order ledger)
  ├─ shadow_ledger (core/shadow_ledger.py rejected-signal forensics, enabled-flag replay isolation; current ledger, NOT retired Batch-2 stack)
  └─ broadcast (telemetry_payload, EventHub WS, applog 5000-ring + file rotate + Slack error-only)
Replay truth path (replay/clock.py + market_feed.py pre-enrich-once/bisect + broker.py + ledger.py + session.py + runtime.py single-pass + fast_worker.py day-shard + determinism.py + parity_validator.py UNCALLED)
  └─ /api/backtest delegates to run_replay_fast_async (routes.py:486-496, warmup 600 hardcoded); /api/replay full-parity opt-in (replay.py, fast_mode default False vs modals default True)
Persistence (db/database.py SQLite WAL: OrderRow/TradeRow/EquityRow/BacktestRow/SettingRow/FillProgressRow/CheckpointRow/PisProfitMemoryRow)
```

Module inventory (all read): core (engine, engine_evaluation, engine_halts, durable_engine, clock, calendar, calendar_helper, domain, runtime, phase, scanner, top_movers_scanner, gap_scanner, scanner_selection, scanner_discovery_input, universe_discovery, watchlist_mapper, entry_reservations, shadow_ledger, applog, alerting, benchmark, time_utils) · data (manager, base, store, session_coverage, synthetic, yahoo, alpaca_data) · indicators (ta, slot_volume, vwap_telemetry) · strategies (base + 10 strategies + pipeline) · ai (features, model, prediction, trainer, generation, regime, insights) · pis (models, protection, exit_planner, position_brain, thesis_drift, trade_health, state_machine, execution, engine_adapter, service, authority, evidence, runner_intelligence, regime_classifier, reasoning_engine, sentiment, profit_memory) · portfolio (tracker, allocator, performance, recent_performance) · risk (manager, market_regime) · brokers (base, paper, alpaca, alpaca_hardening) · api (routes, ws, replay, dashboard_history) · db (database) · replay (7) + backtest/metrics + analytics/adaptive_profit_harvester · config + main · frontend (api.ts, BacktestModal, ReplayModal, ShadowPanel, store, types).

## Appendix B — Gap Register (IDs → CR coverage; retired items excluded, never revived)

Entry/signal:
- GAP-01 momentum 10-bar late + vol 0.5 + RSI 50-78 + unenforced 5-min blackout + dead HTF + climax-rewarding confidence + no economic gate + no one/day → CR-01/02. (`momentum.py:27,54-65,187-243,257-312`)
- GAP-02 ORB 5bps buffer + acceptance 0.50 + climax 2.5 + drift 1% + RANGE-default regime lag + pending-blocks-retry vs ML-reject-retries → CR-03. (`orb.py:37-52,99-113,138-181`)
- GAP-03 Squeeze 105-bar warmup + confirm-no-vol-check + 1-bar-delay chase + bb_mid weak + VOLATILE-nesting + overwrite re-arm → CR-04. (`bb_squeeze.py:101,137-172`)
- GAP-04 Gap&Go no exhaustion + cents stop + train/serve mover skew + retry loop + 11:30 cutoff → CR-05. (`gap_and_go.py:37-41,76-135`)
- GAP-05 Sweep day-extreme lateness + 0.30% tolerance + 3.0R + no min-risk floor → CR-06. (`liquidity_sweep.py:34-39,97-109`)
- GAP-06 Pullback inverted vol (require ≤0.5) + mislabeled mean-reversion + 6-condition rarity + no time gate → CR-07. (`trend_pullback.py:25-36,86-107`)
- GAP-07 RSI2 1-min ≤5 noise + 2.0ATR/1.5R + TREND_UP-only + vol≤1.0 rejects capitulation → CR-07. (`rsi_reversion.py:30-37,46-74`)
- GAP-08 RelativeStrength no stop/target (generic 1.65% fill) + rs None→never-fires + 30-min-high ex-current-bar → CR-07 (quarantine/thesis-geometry requirement). (`relative_strength.py:54-55,72-100`)
- GAP-09 Pipeline stacked over-filter (blackout+3 vetoes+ML/meta/score+single-winner) + tightening-only weights kill >1.0 boosts + circular geometry→score + gap-only-mandatory meta (others BYPASS) + stale REGISTRY snapshot + dead gray-zone/event-day → CR-08/09. (`pipeline.py:29-31,43,58,95-100,133-159,161-286,339-420`)
- GAP-10 Warmup hard-reject (COHR +6.5R class) + 360 invisible overlap/gated-tick candidates + NULL decision timestamps → CR-08. (`pipeline.py:98-100,415-420`)
- GAP-11 ML integrity: 0.70/0.30 hardcoded blend vs config, 1-p_up short fallback, heuristic blocks mean-reversion pre-train, WFV full-fit fallback leak, holdout-overlap optimistic AUC, train/serve regime+mover skew, 0.52 meta floor promotes noise, profit-weight discarded, candidate-cache id-collision → CR-09. (`ai/model.py:88-97,323-375,494-555,605-843`, `features.py:46-149`, `prediction.py:74-1489`, `trainer.py:43-976`, `regime.py:20-55`)
- GAP-12 Scanners: 96h-lag pass + UNKNOWN→EMPTY + SCAN_SCORE flood + screener dead code + prefer_cache 429 + gap-cache frozen intraday + missing change_pct + FSR/GOEV stale + coverage telemetry invisible + watchlist_mapper dead → CR-08/09 (prefetch, backpressure, TTL unification). (`scanner.py:146-657`, `top_movers_scanner.py:77-262`, `gap_scanner.py:54-271`, `universe_discovery.py:19-369`)

Exit/peak (55% surrendered, losers 30 vs 13.6 bars):
- GAP-13 Activation 1.50 vs 0.44R peaks + 55% guard tolerance + 0.35R/45%ATR buffers + EMA-0.3 lag + monotonic hold → CR-10/11/12. (`pis/models.py:239-281`, `protection.py:66-204`, `exit_planner.py:166-195`)
- GAP-14 Stagnation 30 bars + degraded drift≥0.7/adv≥0.9/15-bar -0.4R only + early 3-5 bars only + EXIT needs 3× + DIST trim profit-only + harvest excludes losers → CR-13. (`pis/models.py:252`, `thesis_drift.py:45-107`, `position_brain.py:358-421`, `exit_planner.py:116-138,237-246`)
- GAP-15 Lifecycle: no MATURING→EXIT, MATURING unreachable from RUNNER, EXIT→WEAK/VALID revert delays, WEAKENING→VALIDATED whipsaw, runner needs peak≥3.0 (unreachable) → CR-11/13. (`position_brain.py:379-418`, `state_machine.py:41-154`)
- GAP-16 Health cushions single-bar pullbacks (micro-consolidation 0.65, vol≤0 neutral 0.60, trend retention floor) + regime defaults TREND loosest + PositionObservation has no open (bar-dir fallback) → CR-10/12. (`trade_health.py:48-112`, `runner/regime_classifier.py`)
- GAP-17 Runner prediction RANGE 0.5×/CHOPPY caps + is_runner gate unreachable + wick-peak floor unhittable → CR-11. (`runner_intelligence.py:44-118`, `profit_memory.py:50-61`)
- GAP-18 EOD rides underwater to flatten (no pre-harvest) + 10×sleep blocks tick + daily-halt string-match clear → CR-15. (`engine_halts.py:16-113`, `risk/manager.py:244-251`)
- GAP-19 Bare `exit` 468/-$12.7k + CSV-only reasons + gross MFE/capture + partial avg-price no-breakeven + dust shares + no peak/giveback/bars/regime/side buckets + emergency_protection alias bug → CR-16. (`portfolio/tracker.py:62-76,364-435`, `pis/execution.py:30-60`, `performance.py:128-186`, `recent_performance.py:62-148`, `db/database.py:356-386`)

Sizing/costs/side:
- GAP-20 Costs understated (2bps+0 commission, targets no-slip, sizing ignores costs, live MARKET spread on every 0.5 scale-out, no borrow/HTB) + Kelly budget-only confusion + sector/correlation unverified + notional-cap vs governor interaction → CR-17. (`brokers/paper.py:42-71,139-143,205-250,488-506`, `risk/manager.py:344-444`, `portfolio/allocator.py:132-266`)
- GAP-21 Shorts no haircut (no extra stop/time/fee, symmetric health, close-vs-entry bias favors longs) → CR-19. (`brokers/*`, `trade_health.py:62-64`)
- GAP-22 Turnover 5.0x global lets mornings eat afternoons (BE×6 evidence) + R-unweighted cooldown (scratch = full loss) → CR-18 + risk tweak. (`config.py:90`, `risk/manager.py:148-179`)
- GAP-23 `Settings.risk_config()` drops 20+ RiskConfig fields (governor/vetoes/TTL) → boot/reload drift → add to CR-08 acceptance (config parity check). (`config.py:268-296` vs `RiskConfig:35-140`)

Data/replay/API/observability:
- GAP-24 Enrich fast-path stale-bar + opening_range O(n·sessions) + width div0 + RS saturation + slot collision + first-week None + vwap_telemetry scan/inverted labels + phase UTC crash + holiday-blind phases + clock fail-open + synthetic holiday/weekday mismatch + Yahoo silent clamp/dead-resample/split-adjust + closed_slice wall-vs-replay + batched-fallback gap + VIX TTL flap + store row-wise/WAL + coverage boundary-brittle + empty-frame shared-mutable + provider 429/uncapped-backoff/news-leak + snapshot-fail dead telemetry + slot engine per-tick risk → CR-08/20 (data hardening batch). (`indicators/*`, `core/clock.py:23-96`, `phase.py:40-139`, `data/*`, `alpaca_data.py:35-469`)
- GAP-25 Tick fragility: heartbeat-before-poll 1-tick lag + modulo-window skip + non-watchlist gap blind + string _is_replay + fake-account on outage + stale equity when fill-less + reconcile-pop deletes peaks/stops + ambiguous-order 60s block + behavioral 3-DB-query fail-closed + manual HALTED asymmetry + unbounded warmup fan-out + SMA cache symbol-only + enrich-per-tick CPU + stale/frozen replay gates + inverted short peak/trough names + PIS-crash full-close + static priority + PIS-only SPOF + paper-sync no-verify vs alpaca needs-open-stop + emergency unbounded loop + EOD 429 risk + durable VWAP-overwrite + sync save_order stall + synthetic exit-row poisoning + runtime global-patch + calendar regex overbroad + domain qty==0→long + utcnow inconsistency + event-loop deprecated → CR-08/15/20 (reliability batch; each fix isolated + replay-diff checked). (`core/engine.py:525-3008`, `engine_evaluation.py:20-483`, `engine_halts.py:16-189`, `durable_engine.py:19-95`, `runtime.py:37-68`, `calendar.py:29-153`, `domain.py:17-250`)
- GAP-26 Replay/API: fast-vs-continuous no validator (parity=None) + UUID/equity/shape divergence + empty fast timeline breaks flow UI + starting_cash/slippage ignored by /api/backtest + BacktestModal expects  full metrics/warnings + warmup default 1500>max 500 + fast-default mismatch + verdict filter unused + NULL-date sort + mode-locked history + corrupt-row 500 + fail-silent positions/bars + validation subprocess no-timeout + profit-allocator sum check only + WS silent drops/no-auth + shadow in-flight flush + UTC session misattribute + None-price fallback + benchmark shape/EST-offset dead → CR-08/16/20 (parity + contract batch). (`replay/*`, `api/routes.py:31-688`, `api/ws.py:53-155`, `api/replay.py:13-158`, `dashboard_history.py:25-55`, `db/database.py:26-417`, `core/shadow_ledger.py:43-432`, `core/benchmark.py:22-227`, `frontend: api.ts:91-105, BacktestModal:28-248, ReplayModal:67-472, ShadowPanel:47-206, store.ts, types.ts`)
- GAP-27 Retired-code hygiene: ~45 preserved-comment blocks + 42 apply scripts + root session dumps + D4 dead branches → P4 (already planned; expands D1 file list per this audit).

---

## Implementation Status — 2026-10-14 (audit only, no code changed)

Branch audited: `Alphadesk_002` (clean tree). Commits 2026-10-14: `94064cc, 17ac825, eb02e62, 5bd521c, 59089fe, f71d108, 7f1f748`.
Method: file reads of the exact locations cited per CR. Verdicts below are code-as-is.

### P0 — Entry Intelligence

- **CR-01 IMPLEMENTED** (`strategies/momentum.py`): enforced `min_minutes_since_open 10` with `MOM_MIN_MINS_OPEN` env support, `event_window_bars 3` with `MOM_EVENT_WINDOW` env support, `rsi 55/70 + vol_z 1.2`, `min_er20 0.25`, stacked-EMA filter, climax guards, `min_target_atr 0.5`, `min_reward_risk 1.0` in `default_params` & economic gate check, 1/day cap, confidence hump, and `htf_trend_filter` support.
- **CR-02 IMPLEMENTED**: `climax_stop_mult 2.5` / `atr_stop_mult 2.0`, `dynamic_mult = 2.5 if vol_z>2.0 else 2.0`, target = `rr * dynamic_mult * ATR`.
- **CR-02b IMPLEMENTED**: NEW `strategies/pullback_rearm.py` registered, `lookback 30` / `vol_z_min 1.0` correct, `daily_sma20` + `ema20/vwap` + 3-bar pullback + EMA9 reclaim present; configurable `enable_pullback_rearm` knob added in `RiskConfig`, strategy `default_params`, `ENABLE_PULLBACK_REARM` env variable, and wired into `engine_evaluation.py` strategy loop.
- **CR-03 IMPLEMENTED**: `orb.py` `end_time 15:00` with `ORB_END_TIME` env support, `acceptance_lowvol 0.60 if vol_z<2.0 else 0.50`, `min_range_atr 0.5`, and `ctx.now` session date validation against stale bars.
- **CR-04 IMPLEMENTED**: `bb_squeeze.py` `min_minutes_since_open 15`, `breakout_buffer_atr 0.2`, `volatile_squeeze_pct 0.10` escape, `volatile_min_vol_z 1.5` configurable parameter, `min_target_atr 0.5`.
- **CR-05 IMPLEMENTED**: `gap_and_go.py` `max_gap_pct 8.0`, `exhaustion_atr 2.5 / exhaustion_pct 3.0` symmetric guards, 1/day + `MANDATORY_STRATEGIES` fail-close kept.
- **CR-06 IMPLEMENTED**: `liquidity_sweep.py` `min_risk_atr 0.75` + `min_target_atr 0.5`, enforced both sides.
- **CR-07 IMPLEMENTED**: regime ranking `pipeline.py` (`ranking_score = raw*configured_weight` -> `sig.score`), gate admission on tightening-only `admission_score = raw*effective_regime_weight` (`effective_regime_weight = min(1.0, weight)`). `trend_pullback.py` viability `min_risk_atr 0.5` + EMA-gap `0.5*ATR` done. `rsi2_reversion.py` unchanged by design with PIS-side ceiling harvest wired.
- **CR-08 IMPLEMENTED**: scanner warmup prefetch `core/engine.py` done; backfill-before-`<15`-return `engine_evaluation.py` done; fail-soft `WARMUP_REDUCED` done; gated-tick drops at both early-return sites recorded to `shadow_ledger` with `gate="gated_tick"`; strategy overlap block active recording dropped candidates with `gate="strategy_overlap"`; pipeline conflict losers tagged with `gate="strategy_overlap"`; `signal_timestamp_utc` populated from `signal.ts` / `signal.timestamp` property.
- **CR-09 IMPLEMENTED**: bounded precompute pool + 900s wall + WFV 90/10/min-3 done; `MANDATORY_STRATEGIES` fail-close kept; rescue path done; specialist-fallback spike alert (>=10 in 15m) wired into `pipeline.py`; `model._meta_label_engine` restored in `pipeline.py:__init__` for `trainer.py` lifecycle compatibility.

### P1 — Peak-R Capture

- **CR-10 IMPLEMENTED** (`pis/protection.py:108-124`, `pis/models.py:315-321`): `breakeven_atr_mult_breakout 1.50` vs `..._reversion 2.50`, `progressive_lock_end_frac 0.45` (was 0.35), `cap 0.85`, `trigger 0.40`.
- **CR-11 IMPLEMENTED** (`pis/exit_planner.py:198-212`, `pis/models.py:267,347`): predicted-ceiling scale-out `peak >= max(1.0, 0.70*predicted_max_r)` + `momentum<=0.45` → Stage-1 50%; `runner_qualification_r 2.0` (was 3.0); ceiling full-harvest kept; `predicted_max_r/runner_probability` in metadata.
- **CR-12 IMPLEMENTED** (`pis/exit_planner.py:272-286`, `pis/models.py:325-328`): tiered `0.55 (peak<1R) / 0.40 (1-2R) / 0.30 (>=2R)`; `min(peak*0.50, max(...))` structure kept.
- **CR-13 IMPLEMENTED** (`pis/exit_planner.py:252-257,316-318,369-370`, `pis/models.py:332-334`): `enable_doa_stagnation_harvest=True`, `doa_stagnation_bars 12`, `doa_r_threshold -0.25` → `FULL_EXIT` + `DOA_STAGNATION_HARVEST`.
- **CR-14 IMPLEMENTED (no-change per spec)** (`pis/thesis_drift.py:73-91`, `pis/models.py:352-358`): `-0.55R`, bars 2-5, 2-bar confirmation untouched; wick `>=0.60` context only.
- **CR-15 IMPLEMENTED** (`core/engine_evaluation.py:258-275`, `config.py:53-65`): `eod_preharvest_r` configurable in `RiskConfig` (default -0.30R), triggers pre-harvest at entry-window cutoff when `current_r <= threshold`, `health.overall < 0.45`, and `lifecycle_state != LifecycleState.RUNNER` (and `runner_score < 0.70`).
- **CR-16 IMPLEMENTED** (`portfolio/tracker.py:437-447`, `brokers/paper.py:334-351`, `brokers/base.py:88`): bare `"exit"` fallback eliminated → `stop_loss` classify else `unspecified_exit:<strategy>`; `close_position` default `"manual"` → `"manual close"`; test `test_exit_reason_never_bare` (`tests/test_portfolio.py:141`).

### P2 — Sizing / Portfolio

- **CR-17 IMPLEMENTED** (`risk/manager.py:367-421,446-454`, `portfolio/allocator.py:87-118`, `core/engine.py:907-915,998-1008`, `config.py:322-327`): hardcoded 0.5-2.0% TradeScore sizing stays commented; config-driven budget + `binding_constraint` log; scanner_mult 1.0/0.75/0.50; governor tightening-only; sector/correlation gate via `process_portfolio_allocations`; `Settings.risk_config()` auto-maps shared fields (GAP-23 fix).
- **CR-18 IMPLEMENTED (location differs)** (`config.py:101-102`, `core/engine.py:3012-3017`): `max_morning_turnover_multiple 3.0` + `max_daily_turnover_multiple 5.0`, morning `<12ET → 3.0x` / full-day `5.0x`. Lives in behavioral-loop detector, not `risk/manager` churn check.
- **CR-19 IMPLEMENTED** (`config.py:103`, `core/engine.py:984-991`, `risk/manager.py:352-370`): `countertrend_mult 0.5`; shorts in `TREND_UP` / longs in `TREND_DOWN` scaled via `sig.risk_multiplier`.

### P3 — Data / Telemetry / Safety

- **CR-20 IMPLEMENTED**: lineage fields end-to-end (`domain.py:64,107-108,155-156,206-207`, `risk/manager.py:545-546`, brokers); resolver `NO_BARS` 6h age-out + `enabled` replay-isolation + `test_disabled_ledger_drops_rows` present; retired `decision_traces` writer correctly not revived; live stream -> `bars_cache.db` persistence wired in `data/manager.py:poll()`; NVDA `px=128.92` stale-bar anomaly prevented in `strategies/orb.py` via `ctx.now` session date validation; MFE/MAE true excursion stamping verified in `engine.py:_drain_fills` and `engine_evaluation.py`.
- **CR-21 IMPLEMENTED** (`risk/manager.py:203-233`, `brokers/paper.py:334`, `brokers/base.py:88`, `core/engine.py:790`): max-drawdown halt stays removed (daily halt only); `close_position` default `"manual close"`; EOD grace 15, PDT guard, churn 5.0x kept. Manual P&L dashboard/review-halt outside files read.
- **CR-22 IMPLEMENTED** (`config.py:61-67`, `brokers/alpaca.py:682-717`, `core/engine.py:893-895`): `min_price 0.50`, `min_avg_dollar_volume 5000`, `data_gap_threshold_secs 120.0`; `SHORT_BORROW_UNAVAILABLE` distinctly logged/tagged; engine pre-gate shortability check.

### P4 — Dead-Code Removal

- **D1 PENDING OPERATOR APPROVAL**: Preserved-comment blocks remain deliberately preserved in accordance with repository rule `dbdd680` and the mandatory Code Modification & Historical Preservation Policy ("Never delete old code — comment it out"). Deletion of these blocks is on hold until explicitly authorized by the operator.
- **D2 IMPLEMENTED**: `tools/` empty, 0 `apply_*.py` files.
- **D3 IMPLEMENTED**: root has zero `*.json` dumps, no `Refer.txt`; `data/` holds only DBs/`replays/`/`reports/`.
- **D4 PARTIAL**: allocator dead comment gone; `momentum.py:100,298` HTF branch still present (`False` default + guarded branch — optional path, not unreachable); `test_strategies` unused-field / frontend shim not verified.

### Why the bot is highly restricted / idle after 2026-10-14

Not one veto — three layers stacking multiplicatively: `P(trade) = P(strategy emit) x P(4 pipeline vetoes) x P(ML) x P(meta) x P(TradeScore/rescue) x P(overlap winner) x P(risk/turnover)`. Today cut the first term ~3-5x while leaving all downstream vetoes ON, then harvests survivors fast so no inventory persists:

1. Momentum choke (highest-flow strategy): `momentum.py:92-95,102-104,113-117,404+` — vol 2.4x, RSI squeeze, window 10→3, `er>=0.25` + stacked-EMA + 2.0ATR climax + wide-bar veto + L09 expiry + 1/day.
2. Series pipeline vetoes, no bypass: `pipeline.py:195,214,242,275 + 415,454,504` + `config.py:126,132,137,144` — chase + location + extreme + blackout + ML + meta + TradeScore must all pass; `soft_composite=False` kills gray-zone.
3. Double 1-winner funnel: `engine_evaluation.py:424-435` 1/symbol/bar + `pipeline.py:545-548` conflict winner + per-strategy 1/day — rest to shadow ledger only.
4. Universal econ gates: `min_target 0.5ATR` (momentum/BB/liq/pullback/rearm) + `min_risk 0.5-0.75ATR` + RS `0.1ATR` + ORB `0.5ATR` range + BB `0.2ATR` buffer.
5. Chase triple-count: strategy exhaustion (gap `2.5ATR/3%`, momentum climax) + pipeline chase `1.8ATR/0.65` + extreme `1.0ATR` + location `3.0ATR VWAP` — same extended bar rejected 3-4 ways; only way back is `pullback_rearm`, itself gated (`35 bars + vol 1.0 + 15min + EMA9 + 1/day`).
6. Morning halt + short tax: `config.py:102` morning `3.0x` fail-closed halt + `config.py:103` countertrend `x0.5`.
7. Harvest-fast exits: `pis/models.py:332-334,326-328` + `exit_planner.py:253-257,273-278,316-318` + `engine_evaluation.py:268-269` — DOA `12b/-0.25R FULL_EXIT`, tiered `40/30%` partial, EOD `-0.30R` pre-harvest.

Rescue/re-arm state: `HIGH_AGREEMENT_RESCUE` ON but narrow (`pipeline.py:512-527`, needs score-fail + `ml>=0.65` + meta PASS); `GRAY_ZONE_RESCUED` OFF/shadow-only; `WARMUP_REDUCED` (15-30 bars) ON but needs meta approval + `x0.5` risk, `<15 bars` still hard reject; `pullback_rearm` ON but hard to hit.

Cross-cutting: Environment knobs `MOM_MIN_MINS_OPEN`, `MOM_EVENT_WINDOW`, `ENABLE_PULLBACK_REARM`, `ORB_END_TIME` are now fully wired and operational alongside pipeline-level knobs (`RESCUE_*`, `WARMUP_REDUCED_RISK_MULT`).

